"""Compare pytest version positions using two linked runs over frozen inputs."""
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
import re
from uuid import uuid4


PAIR_STATE_ENV = 'REPROLAB_PAIR_STATE_DIR'
SEQUENCES = {'ab': ('before', 'after'), 'ba': ('after', 'before')}
STATE_POLICY = {
    'environment_variable': PAIR_STATE_ENV,
    'directory': 'fresh_empty_per_group_shared_within_group',
    'external_state': 'reset_unverified',
}
STATE_NOTICE = (
    '每组开始时提供一个全新的空目录，通过 REPROLAB_PAIR_STATE_DIR 传给该组两个测试进程。'
    '仅使用此目录的项目状态受这一设置控制。其他文件、数据库、远端服务和外部缓存的初始状态及重置情况未知。'
)
_PATTERNS = {
    ('failed', 'passed', 'passed', 'failed'): 'second_position_passes',
    ('passed', 'failed', 'failed', 'passed'): 'first_position_passes',
    ('passed', 'passed', 'passed', 'passed'): 'both_pass',
    ('failed', 'failed', 'failed', 'failed'): 'both_fail',
}
_LABELS = {
    'second_position_passes': '两组均为第二个执行的版本通过',
    'first_position_passes': '两组均为第一个执行的版本通过',
    'candidate_passes_both': '候选两组通过，基线至少失败一次',
    'baseline_passes_both': '基线两组通过，候选至少失败一次',
    'both_pass': '四次均通过',
    'both_fail': '四次均失败',
    'mixed': '结果随版本及位置变化',
    'incomplete': '证据不足',
}
_OUTCOMES = {'passed': '通过', 'failed': '失败', 'skipped': '跳过', 'not_run': '未执行',
             'inconclusive': '受阻'}


def validate_version_order(record, *, execution=None):
    if record is None:
        return SEQUENCES['ab']
    keys = {'experiment_id', 'source_report_sha256', 'group', 'state_policy'}
    if execution is not None:
        keys.add('started_versions')
    if (not isinstance(record, dict) or set(record) != keys
            or not isinstance(record['experiment_id'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', record['experiment_id'])
            or not isinstance(record['source_report_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', record['source_report_sha256'])
            or record['group'] not in ('ab', 'ba') or record['state_policy'] != STATE_POLICY):
        raise ValueError('版本先后对照记录无效。')
    sequence = SEQUENCES[record['group']]
    if execution is not None:
        launched = [side for side in sequence if 'process_id' in execution[side]]
        if record['started_versions'] != launched or tuple(launched) != sequence[:len(launched)]:
            raise ValueError('版本先后对照的实际启动记录与执行证据不一致。')
    return sequence


def _open_report(stack, location, control):
    from .project_history import _checked_report
    from .replay_verification import open_replay_evidence

    return _checked_report(stack.enter_context(open_replay_evidence(location, control=control)), control=control)


def _require_original(report):
    if report.get('runner', 'pytest') != 'pytest' or report.get('version_order'):
        raise ValueError('版本先后对照需要一份原始 pytest 项目报告。')


def _diagnosis(original, reports):
    from .project_comparison import pytest_import_issues
    from .project_review_summary import _review_summary
    from .replay_verification import compare_replay_reports

    source = _review_summary(original, False, 'strict', None)
    result = {
        'kind': 'project_version_order', 'schema_version': 1, 'state_policy': STATE_POLICY,
        'notice': STATE_NOTICE, 'experiment_id': None, 'groups': {},
        'original': {key: source[key] for key in ('report_sha256', 'revisions', 'gate', 'counts')}
                    | {'source_sha256': original['source_sha256']},
    }
    blockers = []
    observations = {}
    for group, sequence in SEQUENCES.items():
        report = reports.get(group)
        if report is None:
            result['groups'][group] = None
            blockers.append(group + '_missing')
            continue
        record = report.get('version_order')
        inputs = compare_replay_reports(original, report)['inputs']
        # Old reports can omit the shared-test cache. The checked candidate snapshot still pins those files.
        shared = inputs.pop('shared_tests')
        inputs.pop('version_order', None)
        inputs.pop('recorded_inputs', None)
        if (not record or record['group'] != group
                or record['source_report_sha256'] != source['report_sha256']
                or shared is False or not all(value is True for value in inputs.values())):
            raise ValueError('版本先后对照证据与原报告或指定组不匹配：' + group)
        if result['experiment_id'] not in (None, record['experiment_id']):
            raise ValueError('版本先后对照的两组证据来自不同实验。')
        result['experiment_id'] = record['experiment_id']
        runs = {side: report[side]['status'] for side in sequence}
        result['groups'][group] = {
            'report_sha256': report['_report_sha256'], 'planned_versions': list(sequence),
            'started_versions': record['started_versions'], 'status': report['status'], 'runs': runs,
            'gate': _review_summary(report, False, 'strict', None)['gate'],
        }
        if (report['status'] == 'cancelled' or any(status != 'completed' for status in runs.values())
                or record['started_versions'] != list(sequence)):
            blockers.append(group + '_execution_incomplete')
        if pytest_import_issues(report):
            blockers.append(group + '_import_provenance')
        observations[group] = {entry['test_id']: {side: entry[side]['status'] for side in sequence}
                               for entry in report['entries']}
    if len(reports) == 2:
        ab, ba = reports['ab'], reports['ba']
        replay_proof = compare_replay_reports(ab, ba)
        conditions = replay_proof['conditions']
        if replay_proof['inputs'].get('recorded_inputs') is False:
            blockers.append('recorded_inputs_changed')
        result['conditions_between_groups'] = conditions
        if any(value is False for value in conditions.values()):
            blockers.append('run_conditions_changed')
        for side in SEQUENCES['ab']:
            first, second = ab[side], ba[side]
            if first['status'] == second['status'] == 'completed':
                orders = [version.get('execution_order') for version in (first, second)]
                if orders[0] != orders[1] or orders[0] is None:
                    blockers.append(side + '_test_order_unconfirmed')
                if first.get('module_origins') != second.get('module_origins'):
                    blockers.append(side + '_import_origins_changed')

    rows = []
    for identity in original['selected_tests']:
        cells = {group: items[identity] for group, items in observations.items()}
        values = tuple(cells.get(group, {}).get(side) for group in SEQUENCES for side in SEQUENCES['ab'])
        comparable = not blockers and all(value in ('passed', 'failed') for value in values)
        pattern = 'incomplete'
        if comparable:
            if values in _PATTERNS:
                pattern = _PATTERNS[values]
            elif values[1] == values[3] == 'passed':
                pattern = 'candidate_passes_both'
            elif values[0] == values[2] == 'passed':
                pattern = 'baseline_passes_both'
            else:
                pattern = 'mixed'
        rows.append({'test_id': identity, 'observations': cells, 'pattern': pattern})
    counts = dict(Counter(row['pattern'] for row in rows))
    return {**result, 'status': 'incomplete' if blockers or counts.get('incomplete') else 'completed',
            'blockers': blockers, 'counts': counts, 'entries': rows}


def render_version_order(result):
    from .project_review_summary import _code

    original = result['original']
    lines = ['# pytest 版本先后对照', '', STATE_NOTICE,
             '', '两组观察用于诊断。原报告继续提供原始比较和门禁判定。',
             '', f'基线 A 源码 SHA-256 · {_code(original["source_sha256"]["before"])}',
             f'候选 B 源码 SHA-256 · {_code(original["source_sha256"]["after"])}',
             f'原报告 SHA-256 · {_code(original["report_sha256"])}',
             f'原报告严格门禁 · {_code(original["gate"]["status"])} · {_code(original["gate"]["reason"])}']
    lines.append('')
    for group, title in (('ab', 'A→B'), ('ba', 'B→A')):
        record = result['groups'][group]
        if record is None:
            lines.append(f'- {title} · 缺少报告')
        else:
            labels = {'before': '基线 A', 'after': '候选 B'}
            versions = '、'.join(f'{labels[side]} {_code(status)}' for side, status in record['runs'].items())
            positions = ' → '.join(labels[side] for side in record['started_versions'])
            lines.append(f'- {title} · SHA-256 · {_code(record["report_sha256"])} · 已启动 {positions or "无"}'
                         f' · {versions} · 严格门禁 {_code(record["gate"]["status"])}')
    if result['blockers']:
        lines.extend(['', '无法作出完整对照判断 · ' + '、'.join(map(_code, result['blockers']))])
    unknown = [key for key, value in result.get('conditions_between_groups', {}).items() if value is None]
    if unknown:
        lines.extend(['', '两组间无法从保存的记录比较的运行条件 · ' + '、'.join(map(_code, unknown))])
    lines.extend(['', '| 测试 | A→B 第 1 基线 A | A→B 第 2 候选 B | B→A 第 1 候选 B | B→A 第 2 基线 A | 观察 |',
                  '| --- | --- | --- | --- | --- | --- |'])
    for row in result['entries']:
        test = ''.join(char if char.isprintable() else ' ' for char in row['test_id']).replace('|', r'\|')
        cells = [_OUTCOMES.get(row['observations'].get(group, {}).get(side), '缺少证据')
                 for group, sequence in SEQUENCES.items() for side in sequence]
        lines.append('| ' + ' | '.join([_code(test), *cells, _LABELS[row['pattern']]]) + ' |')
    return '\n'.join(lines) + '\n'


def version_order_experiment(location, output, *, python_executable=None, timeout=None, confirmed=False, control):
    from .persistence import atomic_write, save_json
    from .project_history import _checked_report, report_reference
    from .project_sessions import project_replay_settings, replay_project_report
    from .runner import RunCancelled

    with ExitStack() as stack:
        original = _open_report(stack, location, control)
        _require_original(original)
        settings = project_replay_settings(original, python_executable=python_executable, timeout=timeout)
        if not confirmed:
            return {
                'kind': 'project_version_order_preview', 'status': 'ready',
                'source_report_sha256': original['_report_sha256'], 'state_policy': STATE_POLICY,
                'selected_tests': original['selected_tests'], 'sequences': SEQUENCES,
                **settings, 'notice': STATE_NOTICE,
            }, 0
        output = Path(output).expanduser().resolve()
        if any(output.is_relative_to(Path(directory).resolve())
               for directory in (original['report_dir'], original['repository'])):
            raise ValueError('版本先后对照输出须保存在原仓库及来源报告之外。')
        experiment_id = uuid4().hex
        directory = output / ('version-order-' + experiment_id)
        directory.mkdir(parents=True)
        expected_report = report_reference(original)
        reports, errors = {}, {}
        for group in SEQUENCES:
            record = {'experiment_id': experiment_id, 'source_report_sha256': original['_report_sha256'],
                      'group': group, 'state_policy': STATE_POLICY}
            try:
                control.check()
                control.notify('version_order_group', {'group': group})
                run = replay_project_report(
                    original['report_dir'], directory / group, python_executable=settings['runtime']['executable'],
                    timeout=settings['timeout'], confirmed=True, control=control.child(progress=control.progress),
                    version_order=record, expected_report=expected_report,
                )
                reports[group] = _checked_report(run['report_dir'])
            except (RunCancelled, KeyboardInterrupt) as exc:
                errors[group] = {'type': type(exc).__name__, 'message': '用户取消了本次对照。'}
                break
            except (OSError, ValueError) as exc:
                errors[group] = {'type': type(exc).__name__, 'message': str(exc)}
                continue
            if run['status'] == 'cancelled':
                break

        result = _diagnosis(original, reports)
        atomic_write(directory / 'version-order.md', render_version_order(result), replace=False)
        result.update(directory=str(directory), reports={group: report['report_dir'] for group, report in reports.items()})
        if errors:
            result['errors'] = errors
        save_json(directory / 'version-order.json', result)
        gates = [result['original']['gate'], *(group['gate'] for group in result['groups'].values() if group)]
        if any(gate['status'] == 'failed' for gate in gates):
            return result, 1
        blocked = result['status'] == 'incomplete' or result['original']['gate']['status'] == 'blocked'
        code = 2 if blocked else 0
        return result, code


def check_version_order(location, *, ab=None, ba=None, markdown=None, control=None):
    from .project_review_summary import markdown_matches

    with ExitStack() as stack:
        original = _open_report(stack, location, control)
        _require_original(original)
        reports = {group: _open_report(stack, path, control)
                   for group, path in (('ab', ab), ('ba', ba)) if path is not None}
        result = _diagnosis(original, reports)
    if markdown is not None:
        result['markdown_status'] = 'consistent' if markdown_matches(render_version_order(result), markdown) else 'different'
    return result
