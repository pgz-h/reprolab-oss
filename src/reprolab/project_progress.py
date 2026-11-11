"""Read bounded, ordered worker observations without turning them into verdicts."""
import hashlib
import json
import math
from pathlib import Path

MAX_STREAM_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
OBSERVATION_LABELS = {'passed': '通过', 'failed': '失败', 'skipped': '跳过',
                      'not_run': '尚无阶段记录', 'inconclusive': '阶段未完整结束'}
PHASE_LABELS = {'collection': '收集测试', 'setup': '准备', 'call': '测试主体',
                'teardown': '清理', 'between': '切换测试', 'finished': '执行结束'}


def selection_digest(selected):
    return hashlib.sha256(json.dumps(selected, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class ProgressReader:
    """A final partial line is ignored; malformed complete records invalidate the stream."""

    def __init__(self, path, selected, *, collecting=False):
        self.path = Path(path)
        self.selected = set(selected)
        self.selection_sha256 = selection_digest(selected)
        self.mode = 'collect' if collecting else 'run'
        self.offset = self.sequence = 0
        self.header = None
        self.events = {}
        self.started = []
        self.completed = set()
        self.changed = set()
        self.current_test = None
        self.current_phase = 'collection'
        self.collected = None
        self.errors = []
        self.rejection = None
        self.exit_code = None
        self.finished = False
        self.error = ''
        self.tail_ignored = False

    def poll(self, *, final=False):
        if self.error:
            return False
        previous = self.sequence
        try:
            if not self.path.exists():
                return False
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError('进度记录不是普通文件。')
            size = self.path.stat().st_size
            if size < self.offset or size > MAX_STREAM_BYTES:
                raise ValueError('进度记录被截断或超过大小限制。')
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                while True:
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE_BYTES:
                        raise ValueError('单条进度记录超过大小限制。')
                    if not line.endswith(b'\n'):
                        self.tail_ignored = final
                        break
                    self._accept(json.loads(line))
                    self.offset += len(line)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            self.error = '进度记录无法核对：' + str(exc)[:300]
        return self.sequence != previous

    def _accept(self, record):
        if (not isinstance(record, dict) or type(record.get('sequence')) is not int
                or record['sequence'] != self.sequence + 1 or self.finished):
            raise ValueError('事件顺序不正确。')
        event = record.get('event')
        if self.header is None:
            if (event != 'session' or record.get('schema_version') != 1
                    or record.get('mode') != self.mode or record.get('selection_sha256') != self.selection_sha256
                    or any(not isinstance(record.get(key), str) for key in ('python_version', 'pytest_version'))):
                raise ValueError('进度来源与所选测试不一致。')
            self.header = record
        elif event == 'collected':
            if self.collected is not None or type(record.get('count')) is not int or record['count'] < 0:
                raise ValueError('收集记录不正确。')
            self.collected = record['count']
        elif event == 'test_start':
            identity = record.get('test_id')
            if (self.mode != 'run' or self.collected is None or not isinstance(identity, str)
                    or identity not in self.selected or identity in self.started or self.current_test is not None):
                raise ValueError('实际开始的测试不符合所选范围或顺序。')
            self.started.append(identity)
            self.current_test, self.current_phase = identity, 'between'
            self.changed.add(identity)
        elif event in ('phase_start', 'phase_result'):
            identity, phase = record.get('test_id'), record.get('phase')
            if identity != self.current_test or identity not in self.selected or phase not in ('setup', 'call', 'teardown'):
                raise ValueError('阶段与当前测试不一致。')
            prior = [item['phase'] for item in self.events.get(identity, [])]
            if event == 'phase_start':
                allowed = {'setup'} if not prior else {'call', 'teardown'} if prior == ['setup'] else {'teardown'} if prior == ['setup', 'call'] else set()
                if self.current_phase != 'between' or phase not in allowed:
                    raise ValueError('测试阶段顺序不正确。')
                self.current_phase = phase
            else:
                observation = record.get('result')
                if (self.current_phase != phase or not isinstance(observation, dict) or observation.get('phase') != phase
                        or observation.get('outcome') not in ('passed', 'failed', 'skipped')
                        or type(observation.get('xfail')) is not bool
                        or type(observation.get('duration_seconds')) not in (int, float)
                        or not math.isfinite(observation['duration_seconds']) or observation['duration_seconds'] < 0):
                    raise ValueError('测试阶段结果无效。')
                error = observation.get('exception')
                if error is not None and (not isinstance(error, dict)
                        or any(not isinstance(error.get(key), str) for key in ('type', 'message'))):
                    raise ValueError('异常记录无效。')
                self.events.setdefault(identity, []).append(observation)
                self.current_phase = 'between'
                if phase == 'teardown':
                    self.completed.add(identity)
                    self.current_test = None
            self.changed.add(identity)
        elif event == 'collection_error':
            if not isinstance(record.get('message'), str) or len(self.errors) >= 100:
                raise ValueError('收集异常记录无效。')
            self.errors.append(record['message'])
        elif event == 'selection_rejected':
            self.rejection = str(record.get('reason') or '测试范围被改变。')
        elif event == 'session_finish':
            if type(record.get('exit_code')) is not int:
                raise ValueError('执行退出码无效。')
            self.finished, self.exit_code = True, record['exit_code']
            if self.current_test is None:
                self.current_phase = 'finished'
        else:
            raise ValueError('未知的进度事件。')
        self.sequence += 1

    @property
    def valid(self):
        return self.header is not None and not self.error and self.rejection is None

    def matches(self, raw):
        return (self.valid and self.finished and not self.tail_ignored
                and self.exit_code == raw.get('exit_code') and self.events == raw.get('events')
                and self.started == raw.get('started_tests') and self.errors == raw.get('collection_errors')
                and all(self.header.get(key) == raw.get(key) for key in ('python_version', 'pytest_version', 'pytest_profile', 'runtime_environment')))

    def summary(self):
        return {'schema_version': 1, 'status': 'invalid' if self.error or self.rejection else 'recorded' if self.header else 'unavailable',
                'completed': len(self.completed), 'started': len(self.started), 'total': len(self.selected),
                'current_test': self.current_test, 'current_phase': self.current_phase, 'collected': self.collected,
                'session_finished': self.finished, 'tail_ignored': self.tail_ignored,
                'notice': self.error or self.rejection or ('末尾未写完的事件已忽略；此前完整事件保留。' if self.tail_ignored else '')}


def progress_text(payload):
    side = {'before': '基线版本', 'after': '候选版本', 'collection': '测试发现'}.get(payload.get('side'), '项目检查')
    phase = PHASE_LABELS.get(payload.get('current_phase'), '准备执行')
    count = f"已完成 {payload.get('completed', 0)}/{payload.get('total', 0)} 项" if payload.get('side') != 'collection' else '正在发现测试'
    if payload.get('side') == 'collection' and payload.get('session_finished') and payload.get('collected') is not None:
        count = f"已发现 {payload['collected']} 项"
    text = f"{side} · {count} · {phase} · 已用 {payload.get('elapsed_seconds', 0):.1f} 秒"
    quiet = payload.get('quiet_seconds', 0)
    if quiet >= 5:
        text += f' · 距最近事件 {quiet:.0f} 秒'
    if payload.get('notice'):
        text += ' · ' + str(payload['notice'])
    return text
