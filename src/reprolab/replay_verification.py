"""Verify recorded outcomes from two runs over the same frozen inputs."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .environment import compare_environments
from .project_history import _checked_report, _match_reference, report_reference
from .runtime_environment import compare_variable_records


_COMPLETE = {'regression_passed', 'regression_detected', 'baseline_failure', 'improved'}
_LABELS = {'verified': '逐项结果一致', 'different': '逐项结果有变化', 'incomplete': '结果未能完整核对',
           'inputs_changed': '固定输入不同', 'same_run': '两份报告对应同一次运行',
           'unavailable': '暂无法核对重放'}
_CONDITION_LABELS = {'reprolab': 'ReproLab 版本', 'timeout': '每版时限', 'platform': '系统及架构',
                     'toolchain': '工具链版本', 'distributions': 'Python 发行包',
                     'runtime_variables': '运行变量', 'locale_timezone': '区域设置与时区', 'offline': '离线安装模式', 'network_isolation': '系统网络隔离'}
_INPUT_LABELS = {'runner': '执行器', 'sources': '两版源码', 'permissions': '普通文件权限',
                 'selection': '测试清单及顺序', 'shared_tests': '共享测试文件', 'scope': '发现范围或项目目录',
                 'pytest_order': 'pytest 顺序对照设置', 'version_order': 'pytest 版本先后对照',
                 'fixture_control': '基线夹具对照', 'clock_control': 'Python 固定时钟', 'snapshot_scope': '源码冻结范围', 'test_source_control': '基线测试文件对照', 'input_control': '固定基线业务输入', 'recorded_inputs': '已记录业务输入'}
_OUTCOMES = {'passed': '通过', 'failed': '失败', 'skipped': '跳过', 'not_run': '未执行',
             'inconclusive': '受阻'}


@contextmanager
def open_replay_evidence(source, *, control=None):
    """Accept a report path or import a ZIP into a short-lived directory."""
    source = Path(source).expanduser()
    if source.is_file() and source.suffix.casefold() == '.zip':
        from .project_sessions import unpack_project_report

        with TemporaryDirectory(prefix='reprolab-replay-evidence-') as temporary:
            child = control.child(progress=control.progress) if control else None
            imported = unpack_project_report(source, Path(temporary) / 'report', control=child)
            yield Path(imported['report_dir'])
    else:
        yield source


def _check(source, replay):
    return None if source is None or replay is None else source == replay


def _toolchain(report, runner):
    keys = {'pytest': ('python_version', 'pytest_version'), 'go': ('go_version',),
            'vitest': ('node_version', 'npm_version')}[runner]
    versions = {key: (report.get('runtime') or {}).get(key) for key in keys}
    if runner == 'vitest':
        versions['vitest'] = report.get('vitest_versions')
    return versions if all(versions.values()) else None


def _platform(report):
    environment = report.get('environment') or {}
    platform = [environment.get(key) for key in ('platform', 'machine')]
    return platform if all(platform) else None


def _conditions(source, replay, runner):
    conditions = {
        'reprolab': _check(source.get('reprolab_version'), replay.get('reprolab_version')),
        'timeout': _check(source.get('timeout'), replay.get('timeout')),
        'platform': _check(_platform(source), _platform(replay)),
        'toolchain': _check(_toolchain(source, runner), _toolchain(replay, runner)),
    }
    if source.get('network_isolation') or replay.get('network_isolation'):
        conditions['network_isolation'] = source.get('network_isolation') == replay.get('network_isolation')
    if runner == 'vitest':
        conditions['offline'] = _check(source.get('offline'), replay.get('offline'))
    if runner != 'pytest':
        return conditions, None
    environment = compare_environments(source.get('environment'), replay.get('environment'))
    conditions['distributions'] = (None if environment['status'] == 'unknown' else
                                  not any(environment[key] for key in ('added', 'removed', 'changed')))
    conditions['runtime_variables'] = compare_variable_records(source, replay)
    sides = ('before', 'after')
    left = tuple(source.get(side, {}).get('runtime_context') for side in sides)
    right = tuple(replay.get(side, {}).get('runtime_context') for side in sides)
    if any(left) or any(right):
        conditions['locale_timezone'] = left == right if all((*left, *right)) else None
    return conditions, environment


def _scope(report, runner):
    if runner == 'go':
        return report['go_package']
    if runner == 'pytest':
        return (report.get('collection') or {}).get('collection_path', '')
    from .vitest_project import _recorded_install

    return report.get('vitest_project_dir', '.'), _recorded_install(report)


def _pair(source, replay, identity, *, compare_failures=False, compare_fingerprints=False):
    def outcomes(entry):
        if entry is None:
            return None
        return {side: entry[side]['status'] for side in ('before', 'after')}

    old, new = outcomes(source), outcomes(replay)
    complete = (source is not None and replay is not None
                and source['status'] in _COMPLETE and replay['status'] in _COMPLETE
                and all(status in ('passed', 'failed') for status in (*old.values(), *new.values())))
    status = ('matched' if old == new else 'different') if complete else 'incomplete'
    row = {**identity, 'source': old, 'replay': new, 'status': status}
    if compare_failures and status == 'matched':
        from .project_comparison import _failure_shape

        changes = {}
        for side in ('before', 'after'):
            if old[side] == 'failed':
                before, after = _failure_shape(source[side]), _failure_shape(replay[side])
                if before is not None and after is not None and before != after:
                    changes[side] = {'source': before, 'replay': after}
        if changes:
            row.update(status='different', reason='failure_shape_changed', failure_changes=changes)
    if compare_fingerprints and status == 'matched':
        from .project_failure_waivers import recorded_failure_fingerprint

        fingerprints = {}
        for side in ('before', 'after'):
            if old[side] != 'failed':
                continue
            left = recorded_failure_fingerprint(source[side])
            right = recorded_failure_fingerprint(replay[side])
            if left and right and left != right:
                fingerprints[side] = {'source': left, 'replay': right}
        if fingerprints:
            row.update(status='different', reason=row.get('reason', 'recorded_failure_changed'),
                       failure_fingerprint_changes=fingerprints)
    return row


def _entries(source, replay, runner):
    left = {entry['test_id']: entry for entry in source['entries']}
    right = {entry['test_id']: entry for entry in replay['entries']}
    version = min(source.get('review_summary_version', 1), replay.get('review_summary_version', 1))
    compare_failures = runner != 'go' and version >= 2
    compare_fingerprints = runner != 'go' and version >= 3
    for identity in source['selected_tests']:
        old, new = left[identity], right[identity]
        if runner != 'vitest':
            yield _pair(old, new, {'test_id': identity}, compare_failures=compare_failures, compare_fingerprints=compare_fingerprints)
            if runner == 'go' and min(source.get('go_report_version', 1), replay.get('go_report_version', 1)) >= 2:
                original = {case['test_id']: case for case in old.get('case_entries', ())}
                repeated = {case['test_id']: case for case in new.get('case_entries', ())}
                for name in dict.fromkeys([*original, *repeated]):
                    yield _pair(original.get(name), repeated.get(name), {'test_id': name})
            continue
        old_cases = old.get('case_entries') or []
        new_cases = new.get('case_entries') or []
        old_project = old['before'].get('project')
        new_project = new['before'].get('project')
        if not old_cases or not new_cases or old_project != new_project:
            row = _pair(old, new, {'test_id': identity})
            yield {**row, 'status': 'incomplete', 'reason': 'vitest_case_inventory_unavailable'}
            continue
        by_name = {case['test_id']: case for case in new_cases}
        original = {case['test_id']: case for case in old_cases}
        for name in dict.fromkeys([*original, *by_name]):
            yield _pair(original.get(name), by_name.get(name),
                        {'file': identity, 'test_id': name, 'project': old_project}, compare_failures=compare_failures, compare_fingerprints=compare_fingerprints)


def _summary(result):
    counts = result['counts']
    label = _LABELS[result['status']]
    if counts:
        label += (f' · 一致 {counts["matched"]} 项 · 变化 {counts["different"]} 项'
                  f' · 未能核对 {counts["incomplete"]} 项')
    if any(value is False for value in result['conditions'].values()):
        label += ' · 运行条件有差异'
    elif any(value is None for value in result['conditions'].values()):
        label += ' · 部分运行条件未知'
    return label


def _captured_inputs(report):
    samples = {}
    for entry in report['entries']:
        for side in ('before', 'after'):
            outcome = entry.get(side, {})
            if outcome.get('input_digests') or outcome.get('input_error'):
                samples[side, entry['test_id']] = (outcome.get('input_digests'), outcome.get('input_error'))
    return samples


def compare_replay_reports(source, replay):
    """Compare the inputs and outcomes of two already checked reports."""
    runner = source.get('runner', 'pytest')
    replay_runner = replay.get('runner', 'pytest')
    if any(value not in ('pytest', 'go', 'vitest') for value in (runner, replay_runner)):
        raise ValueError('此报告的执行器不支持重放核对。')
    inputs = {
        'runner': runner == replay_runner,
        'sources': _check(source.get('source_sha256'), replay.get('source_sha256')),
        'permissions': _check(source.get('source_mode_sha256'), replay.get('source_mode_sha256')),
        'selection': source['selected_tests'] == replay['selected_tests'],
        'shared_tests': _check(source.get('shared_tests'), replay.get('shared_tests')),
        'scope': runner == replay_runner and _scope(source, runner) == _scope(replay, runner),
    }
    if source.get('order_control') or replay.get('order_control'):
        inputs['pytest_order'] = source.get('order_control') == replay.get('order_control')
    if source.get('version_order') or replay.get('version_order'):
        inputs['version_order'] = source.get('version_order') == replay.get('version_order')
    if source.get('snapshot_scope') or replay.get('snapshot_scope'):
        inputs['snapshot_scope'] = source.get('snapshot_scope') == replay.get('snapshot_scope')
    if source.get('clock_control') or replay.get('clock_control'):
        inputs['clock_control'] = source.get('clock_control') == replay.get('clock_control')
    if source.get('test_source_control') or replay.get('test_source_control'):
        inputs['test_source_control'] = source.get('test_source_control') == replay.get('test_source_control')
    if source.get('fixture_control') or replay.get('fixture_control'):
        inputs['fixture_control'] = source.get('fixture_control') == replay.get('fixture_control')
    if source.get('input_control') or replay.get('input_control'):
        inputs['input_control'] = source.get('input_control') == replay.get('input_control')
    if runner == replay_runner == 'pytest':
        left, right = _captured_inputs(source), _captured_inputs(replay)
        if left or right:
            inputs['recorded_inputs'] = left == right
    result = {'kind': 'project_replay_verification', 'schema_version': 1, 'runner': runner,
              'inputs': inputs, 'entries': [], 'counts': {}, 'conditions': {}}
    if min(source.get('review_summary_version', 1), replay.get('review_summary_version', 1)) >= 3:
        result['recorded_failure_identity'] = True
    for label, report in (('source', source), ('replay', replay)):
        ref = report_reference(report)
        result[label] = {key: ref[key] for key in ('run_id', 'report_sha256')} | {'verdict': report['status']}
    if runner == replay_runner:
        conditions, environment = _conditions(source, replay, runner)
        result['conditions'] = conditions
        if environment is not None:
            result['environment_comparison'] = environment
    if result['source']['run_id'] == result['replay']['run_id']:
        status = 'same_run'
    elif False in inputs.values():
        status = 'inputs_changed'
    else:
        complete = all(report['status'] != 'cancelled'
                       and all(report[side]['status'] == 'completed' for side in ('before', 'after'))
                       for report in (source, replay))
        entries = list(_entries(source, replay, runner))
        if not complete:
            entries = [{**entry, 'status': 'incomplete', 'reason': 'run_incomplete'} for entry in entries]
        counts = Counter(entry['status'] for entry in entries)
        result['entries'] = entries
        result['counts'] = {key: counts[key] for key in ('matched', 'different', 'incomplete')}
        if None in inputs.values() or not complete:
            status = 'incomplete'
        elif counts['different']:
            status = 'different'
        elif not entries or counts['incomplete']:
            status = 'incomplete'
        else:
            status = 'verified'
    result['status'] = status
    result['summary'] = _summary(result)
    return result


@contextmanager
def checked_replay_pair(source, replay, *, control=None):
    with open_replay_evidence(source, control=control) as original:
        with open_replay_evidence(replay, control=control) as rerun:
            yield _checked_report(original, control=control), _checked_report(rerun, control=control)


def verify_replay(source, replay, *, control=None):
    """Check both reports and compare recorded outcomes without executing code."""
    with checked_replay_pair(source, replay, control=control) as (original, rerun):
        return compare_replay_reports(original, rerun)


def verify_new_replay(source, replay, *, control=None):
    """Bind a new report to the source checked before execution."""
    try:
        original = _checked_report(source['report_dir'], control=control)
        _match_reference(original, report_reference(source))
        return compare_replay_reports(original, _checked_report(replay['report_dir'], control=control))
    except (OSError, ValueError) as exc:
        return {'kind': 'project_replay_verification', 'schema_version': 1,
                'status': 'unavailable', 'summary': _LABELS['unavailable'], 'issue': str(exc)}


def replay_verification_exit_code(result):
    return {'verified': 0, 'different': 1}.get(result['status'], 2)


def replay_verification_text(result):
    lines = [result['summary']]
    if issue := result.get('issue'):
        return '\n\n'.join([*lines, issue])
    for label, key in (('来源运行', 'source'), ('重放运行', 'replay')):
        lines.append(f'{label} · {result[key]["run_id"]}')
    for checks, labels in ((result['inputs'], _INPUT_LABELS), (result['conditions'], _CONDITION_LABELS)):
        for key, same in checks.items():
            if same is not True:
                lines.append(f'{labels[key]} · {"有差异" if same is False else "待核对"}')
    attention = [row for row in result['entries'] if row['status'] != 'matched']
    for row in attention[:30]:
        name = (row['file'] + ' · ' if 'file' in row else '') + row['test_id']
        def pair_text(key):
            pair = row[key]
            return '未取得记录' if pair is None else ' / '.join(_OUTCOMES.get(pair[side], '待核对')
                                                              for side in ('before', 'after'))
        lines.extend(['', name, f'来源 · {pair_text("source")}　重放 · {pair_text("replay")}'])
        if row.get('reason') in ('failure_shape_changed', 'recorded_failure_changed'):
            lines.append('已记录的异常类型、阶段或消息发生变化。')
    if len(attention) > 30:
        lines.extend(['', f'另有 {len(attention) - 30} 项，完整清单见 CLI 核对结果。'])
    return '\n'.join(lines)
