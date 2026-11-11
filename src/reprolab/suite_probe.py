"""Observe one frozen plan check on the old version to inform human review."""
import math
from pathlib import Path
from .pytest_profile import read_pytest_profile
import shutil

from .case import load_case, valid_source_mode_hashes
from .diagnostics import diagnose_run
from .failure_probe import _check_inputs
from .persistence import save_json
from .repair_suite import read_suite, _read, _digest, _new_id
from .runner import (RunControl, RunCancelled, run_snapshot, snapshot,
                     source_fingerprint, source_mode_fingerprint, _classify, _valid_worker_record)
from .runtime import choose_runtime


def _selected(state, check_id):
    checks = [item for item in state['plan']['checks'] if item['id'] == check_id]
    if len(checks) != 1:
        raise ValueError('请选择计划内一个明确的检查 ID。')
    check = checks[0]
    return check, load_case(Path(state['directory']) / check['case'])


def _observation(before):
    diagnosis = diagnose_run(before, label='before')
    status, suggestion, explanation = 'inconclusive', None, diagnosis['summary']
    if before['kind'] == 'passed':
        status, explanation = 'before_passed', '旧版也通过本项测试，请审阅检查目标和断言。'
    elif (before['kind'] == 'failed' and diagnosis['code'] != 'module_not_found'
          and before['failure']['type'] != 'ModuleNotFoundError'):
        failure = before['failure']
        if failure['message'].strip():
            status = 'failure_observed'
            suggestion = {'type': failure['type'], 'message_contains': failure['message'][:4000]}
            explanation = '已观察到旧版测试主体故障；请确认它对应目标问题，再审阅故障条件。'
        else:
            explanation = '旧版测试失败但异常消息为空，没有可采用的故障条件建议。'
    return {'status': status, 'suggestion': suggestion, 'explanation': explanation, 'diagnosis': diagnosis}


def _check_binding(state, check, case, record):
    if (record.get('kind') != 'suite_failure_probe' or record.get('schema_version') != 1
            or not isinstance(record.get('probe_id'), str) or not record['probe_id'].startswith('probe-')
            or record.get('suite_id') != state['suite_id'] or record.get('plan_sha256') != state['plan_sha256']
            or record.get('check_id') != check['id'] or record.get('case_sha256') != check['case_sha256']
            or record.get('test_sha256') != check['test_sha256'] or record.get('test_target') != case.test_target
            or record.get('source_sha256') != state['source_sha256']
            or ('source_mode_sha256' in state and record.get('source_mode_sha256') != state['source_mode_sha256'])
            or not isinstance(record.get('runtime'), dict)):
        raise ValueError('试运行记录与当前计划、检查或固定输入不匹配。')
    modes = record.get('source_mode_sha256')
    if (not valid_source_mode_hashes(modes)
            or any(source_mode_fingerprint(Path(state['directory']) / 'inputs' / label) != modes[label]
                   for label in ('before', 'after'))):
        raise ValueError('当前源码权限与试运行时的记录不匹配。')


def probe_suite_check(directory, check_id, output_root, *, python_executable=None, timeout=15, control=None):
    control = control or RunControl()
    control.check()
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 300:
        raise ValueError('试运行时限须为0.1至300秒。')
    state = read_suite(directory)
    check, case = _selected(state, check_id)
    parent = Path(state['directory'])
    output = Path(output_root).expanduser().resolve()
    if output.is_relative_to(parent) or output.is_relative_to(Path(state['repository']).resolve()):
        raise ValueError('试运行材料必须保存在原计划和被测仓库之外。')
    runtime = choose_runtime(python_executable or case.python)
    identity = _new_id('probe')
    folder = output / identity
    folder.mkdir(parents=True, exist_ok=False)
    record = {'schema_version': 1, 'kind': 'suite_failure_probe', 'status': 'preparing',
              'probe_id': identity, 'probe_dir': str(folder), 'suite_id': state['suite_id'],
              'plan_sha256': state['plan_sha256'], 'check_id': check_id, 'case_sha256': check['case_sha256'],
              'test_sha256': check['test_sha256'], 'test_target': case.test_target, 'runtime': runtime,
              'before': None, 'suggestion': None, 'input_integrity_notice': state.get('input_integrity_notice', ''),
              'notice': '仅观察旧版，尚未进行正式两版验收。'}
    save_json(folder / 'probe.json', record)
    try:
        inputs = folder / 'inputs'
        inputs.mkdir()
        modes = {}
        control.notify('preparing')
        for label in ('before', 'after'):
            control.check()
            source = parent / 'inputs' / label
            entries, digest = source_fingerprint(source)
            modes[label] = source_mode_fingerprint(source, entries)
            if (digest != state['source_sha256'][label]
                    or snapshot(source, inputs / label, entries) != digest
                    or source_mode_fingerprint(inputs / label) != modes[label]):
                raise ValueError('试运行准备期间源码或文件权限发生变化。')
        shutil.copyfile(case.test, inputs / 'regression.py')
        if _digest(inputs / 'regression.py') != check['test_sha256']:
            raise ValueError('试运行准备期间测试发生变化。')
        record.update(source_sha256=state['source_sha256'], source_mode_sha256=modes, status='running')
        current = read_suite(parent)
        _check_binding(current, *_selected(current, check_id), record)
        save_json(folder / 'probe.json', record)
        control.notify('before')
        control.check()
        before = run_snapshot(inputs / 'before', inputs / 'regression.py', folder, 'before', timeout,
                              test_target=case.test_target, python_executable=runtime['executable'], control=control,
                              pytest_profile=read_pytest_profile(inputs / 'after') if case.test_target else None)
        record.update(before=before, **_observation(before))
        _check_inputs(record, folder)
        current = read_suite(parent)
        _check_binding(current, *_selected(current, check_id), record)
        control.commit()
        save_json(folder / 'probe.json', record)
        return {**record, 'record_sha256': _digest(folder / 'probe.json')}
    except Exception as exc:
        record.update(status='cancelled' if isinstance(exc, RunCancelled) else 'inconclusive',
                      suggestion=None, error=str(exc), explanation='试运行未完成，没有可采用的故障条件。')
        try:
            save_json(folder / 'probe.json', record)
        except OSError:
            pass
        if isinstance(exc, RunCancelled):
            raise
        raise ValueError(f'试运行未完成，材料保存在 {folder}。{exc}') from exc


def _checked_record_hash(folder, record, digest):
    if isinstance(record.get('before'), dict):
        _check_inputs(record, folder)
    if _digest(folder / 'probe.json') != digest:
        raise ValueError('读取期间观察记录发生变化，请重新查看并确认。')
    return {**record, 'record_sha256': digest}


def read_suite_probe(directory, probe_directory):
    """Read matching local evidence; never execute a test to show its record."""
    state = read_suite(directory)
    folder = Path(probe_directory).expanduser().resolve()
    digest = _digest(folder / 'probe.json')
    record = _read(folder / 'probe.json')
    check, case = _selected(state, record.get('check_id'))
    _check_binding(state, check, case, record)
    record['probe_dir'] = str(folder)
    before = record.get('before')
    if not isinstance(before, dict):
        record.update(status='cancelled' if record.get('status') == 'cancelled' else 'inconclusive',
                      suggestion=None, explanation='试运行未完成，没有完整的旧版执行记录。')
        return _checked_record_hash(folder, record, digest)
    _check_inputs(record, folder)
    events = _read(folder / 'before.events.json')
    complete = _valid_worker_record(events)
    if before.get('timed_out'):
        kind, reason, observed, test_id = 'inconclusive', '试运行超时，无法判断。', None, None
    elif not complete:
        kind, reason, observed, test_id = 'inconclusive', '工作进程记录不完整，无法判断。', None, None
    else:
        kind, reason, observed, test_id = _classify(events, before['exit_code'])
    if (kind != before.get('kind') or observed != before.get('failure') or test_id != before.get('test_id')):
        raise ValueError('试运行故障与实际事件证据不一致。')

    # Cache fields are display conveniences, not evidence. Rebuild every field
    # used by diagnose_run from the raw events whose digest was checked above.
    before = {**before, 'reason': reason, 'worker_record_complete': complete,
              'collected': events.get('collected', 0), 'selection_guard': events.get('selection_guard'),
              'diagnostic_exceptions': events.get('diagnostic_exceptions', []),
              'phase_events': [{key: item.get(key) for key in ('phase', 'outcome', 'exception', 'xfail')}
                               for item in events.get('events', []) if isinstance(item, dict)],
              'collection_errors': events.get('collection_errors', []),
              'has_xfail': events.get('has_xfail', False), 'python_version': events.get('python_version'),
              'pytest_version': events.get('pytest_version'), 'python_executable': events.get('python_executable')}
    with (folder / 'before.log').open('rb') as stream:
        length = stream.seek(0, 2)
        stream.seek(max(0, length - 12000))
        before['log_excerpt'] = stream.read().decode('utf-8', errors='replace')
    record['before'] = before
    if record.get('status') in ('cancelled', 'preparing', 'running') or 'error' in record:
        record.update(status='cancelled' if record.get('status') == 'cancelled' else 'inconclusive',
                      suggestion=None, explanation='本次试运行已取消或未完成保存，不能采用故障条件。')
    else:
        observation = _observation(before)
        if record.get('status') != observation['status']:
            record.update(status='inconclusive', suggestion=None, explanation='观察状态与实际记录不一致，请重新试运行。')
        else:
            record.update(**observation)
    return _checked_record_hash(folder, record, digest)


def checked_probe_suggestion(directory, probe_directory, *, python_executable=None, expected_probe_sha256=None):
    """Return validated prefill data; the caller must still obtain human review."""
    state = read_suite(directory)
    # Interpreter inspection may start a separate process. Validate evidence
    # after it returns so a change during that wait cannot become prefill data.
    identity = _read(Path(probe_directory).expanduser().resolve() / 'probe.json').get('check_id')
    check, case = _selected(state, identity)
    runtime = choose_runtime(python_executable or case.python)
    record = read_suite_probe(directory, probe_directory)
    _check_binding(state, check, case, record)
    if expected_probe_sha256 is not None and record['record_sha256'] != expected_probe_sha256:
        raise ValueError('观察记录已经变化，请重新查看并明确确认。')
    if runtime != record['runtime']:
        raise ValueError('当前 Python 环境与试运行记录不一致，请重新试运行。')
    if check['role'] != 'repair' or record['status'] != 'failure_observed' or not record.get('suggestion'):
        raise ValueError('本次观察没有可采用的目标故障建议。')
    return {'check_id': check['id'], 'expected_failure': record['suggestion'], 'probe_dir': record['probe_dir'],
            'probe_id': record['probe_id'], 'probe_sha256': record['record_sha256']}
