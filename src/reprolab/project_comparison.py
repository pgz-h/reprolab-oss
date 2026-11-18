"""Keep test observations separate from conclusions about a frozen comparison."""
from .case import valid_source_mode_hashes
from .project_fixture import test_uses_changed_conftest
from .project_input_control import input_changes


DIVERGENT_OUTCOME_LABELS = {'improved': '观察到失败转通过', 'regression_detected': '观察到通过转失败'}

_SCOPE_MESSAGES = {
    'same_snapshot_divergence': '两版冻结源码和文件权限相同，测试结果却发生变化。请核对运行条件与外部状态。',
    'excluded_changes_only': 'Git 改动被快照规则排除，两版冻结内容相同。本次测试未覆盖这些改动。',
    'excluded_changes': '部分 Git 改动被快照规则排除，本次测试只覆盖纳入快照的内容。',
    'pytest_import_origin': 'pytest 发现项目模块的导入来源漂移，或没有观察到要求的快照导入。',
    'pytest_test_source_control': '本报告使用基线测试文件对照，适用于诊断。',
    'pytest_parameter_changed': '同一测试 ID 的 pytest 参数发生变化，须复核输入后再确认改善。',
    'pytest_input_changed': '失败转通过时已记录的业务输入发生变化，请用基线样本核对候选。',
    'pytest_input_control': '本报告记录固定业务输入。请核对诊断结果或按来源验收计划核对。',
    'pytest_fixture_changed': '失败转通过期间相关 conftest.py 发生变化，需核对候选是否仍接受原输入。',
    'pytest_fixture_control': '候选使用从基线保存的夹具文件做对照。',
    'pytest_discovery_scope': '两版测试发现不完整或候选发现清单缺少基线测试，请核对保护范围。',
    'failure_shape_changed': '同一测试在两版都记录了失败，但异常类型或失败阶段变化。',
    'pytest_version_order': '版本先后对照仅提供诊断观察。请同时核对原始报告和另一组运行。',
    'pytest_order_control': '两版使用固定顺序做对照。请结合原报告判断执行顺序对结果的影响。',
}


def pytest_import_issues(report):
    if report.get('runner', 'pytest') != 'pytest':
        return []
    before, after = (report.get(side, {}).get('module_origins') for side in ('before', 'after'))
    required = (report.get('pytest_profile') or {}).get('snapshot_modules', ())
    if before is None or after is None:
        return [
            {'module': name, 'reason': 'required_snapshot_import', 'before': (before or {}).get(name, []),
             'after': (after or {}).get(name, [])}
            for name in required
        ]
    issues = []
    for name in sorted(set(before) | set(after) | set(required)):
        old, new = before.get(name, []), after.get(name, [])
        old_local = any(origin.startswith('snapshot:') for origin in old)
        new_external = any(origin in ('installed-package', 'external', 'built-in', 'frozen') for origin in new)
        required_missing = name in required and any(
            not origins or any(not origin.startswith('snapshot:') for origin in origins) for origins in (old, new))
        if old_local and new_external or required_missing:
            reason = 'snapshot_to_external' if old_local and new_external else 'required_snapshot_import'
            issues.append({'module': name, 'reason': reason, 'before': old, 'after': new})
    return issues


def pytest_order_difference(report):
    before, after = (report.get(side, {}).get('execution_order') for side in ('before', 'after'))
    if not before or not after:
        return None
    common = set(before) & set(after)
    left = [name for name in before if name in common]
    right = [name for name in after if name in common]
    changed = sum(old != new for old, new in zip(left, right))
    return {'common_tests': len(common), 'positions_changed': changed} if changed else None


def _failure_shape(outcome):
    if outcome.get('input_error'):
        return None
    phases = outcome.get('phases') or []
    if phases:
        failures = [{'phase': phase['phase'], 'type': (phase.get('exception') or {}).get('type')}
                    for phase in phases if phase['outcome'] == 'failed' and not phase.get('xfail')]
    else:
        failures = [{'phase': 'case', 'type': (outcome.get('exception') or {}).get('type')}] \
            if outcome.get('status') == 'failed' else []
    return failures if failures and all(item['type'] for item in failures) else None


def failure_changes(report):
    if report.get('runner', 'pytest') == 'go' or any(
            report.get(side, {}).get('status') != 'completed' for side in ('before', 'after')):
        return []
    changes = []
    for entry in report.get('entries', ()):
        for test in (entry.get('case_entries', ()) if report.get('runner') == 'vitest' else (entry,)):
            before, after = (_failure_shape(test.get(side, {})) for side in ('before', 'after'))
            if before and after and before != after:
                change = {'test_id': test['test_id'], 'before': before, 'after': after}
                if test is not entry:
                    change['file'] = entry['test_id']
                changes.append(change)
    return changes


def pytest_parameter_changes(report):
    if report.get('runner', 'pytest') != 'pytest' or report.get('status') == 'cancelled':
        return []
    changes = []
    for entry in report.get('entries', ()):
        left, right = (entry.get(side, {}).get('parameter_digests', {}) for side in ('before', 'after'))
        names = sorted(name for name in left.keys() & right.keys()
                       if left[name] is not None and right[name] is not None and left[name] != right[name])
        names.extend(sorted(left.keys() ^ right.keys()) if left and right else ())
        if names:
            changes.append({'test_id': entry['test_id'], 'status': entry['status'], 'names': names})
    return changes


def comparison_scope(report, *, reviewed_inputs=()):
    """Return scope findings without changing the recorded test outcomes."""
    same = all(valid_source_mode_hashes(hashes) and hashes['before'] == hashes['after']
               for hashes in (report.get('source_sha256'), report.get('source_mode_sha256')))
    counts = report.get('case_counts', report.get('counts', {}))
    changes = {key: counts[key] for key in ('improved', 'regression_detected') if counts.get(key)}
    excluded = report.get('excluded_tracked_changes') or 0
    imports = pytest_import_issues(report)
    failures = failure_changes(report)
    discovery = report.get('discovery_audit')
    if same and excluded:
        reason = 'excluded_changes_only'
    elif same and changes:
        reason = 'same_snapshot_divergence'
    elif imports:
        reason = 'pytest_import_origin'
    elif report.get('version_order'):
        reason = 'pytest_version_order'
    elif report.get('test_source_control'):
        reason = 'pytest_test_source_control'
    elif report.get('input_control'):
        reason = 'pytest_input_control'
    elif report.get('fixture_control'):
        reason = 'pytest_fixture_control'
    elif report.get('order_control'):
        reason = 'pytest_order_control'
    elif discovery and (discovery['status'] != 'complete' or discovery['baseline_only']):
        reason = 'pytest_discovery_scope'
    elif any(row['status'] == 'improved' for row in pytest_parameter_changes(report)):
        reason = 'pytest_parameter_changed'
    elif any(row['status'] == 'improved' and row['test_id'] not in reviewed_inputs for row in input_changes(report)):
        reason = 'pytest_input_changed'
    elif report.get('fixture_changes') and any(
            entry['status'] == 'improved' and test_uses_changed_conftest(entry['test_id'], report['fixture_changes'])
            for entry in report['entries']):
        reason = 'pytest_fixture_changed'
    elif failures:
        reason = 'failure_shape_changed'
    elif excluded:
        reason = 'excluded_changes'
    else:
        return None
    return {'reason': reason, 'same_snapshot': same, 'excluded_tracked_changes': excluded,
            'divergent_outcomes': changes if same else {}, **({'pytest_import_issues': imports} if imports else {}),
            **({'failure_changes': failures} if failures else {}),
            **({'order_mode': report['order_control']['mode']} if report.get('order_control') else {})}


def comparison_blocked(report, scope):
    if not scope or report.get('status') == 'cancelled':
        return False
    reason = scope['reason']
    return reason in ('excluded_changes_only', 'same_snapshot_divergence') or (
        reason in ('pytest_import_origin', 'pytest_order_control', 'pytest_version_order',
                   'pytest_discovery_scope', 'failure_shape_changed', 'pytest_fixture_changed', 'pytest_fixture_control',
                   'pytest_input_changed', 'pytest_input_control', 'pytest_parameter_changed', 'pytest_test_source_control') and report.get('status') != 'regression_detected')


def comparison_verdict(report, scope):
    status = report.get('status', 'attention')
    if comparison_blocked(report, scope):
        return 'attention'
    return status


def comparison_scope_message(scope):
    message = _SCOPE_MESSAGES[scope['reason']]
    if count := scope['excluded_tracked_changes']:
        message += f' · {count} 个已跟踪路径的改动未纳入快照'
    if scope['reason'] == 'excluded_changes_only' and scope['divergent_outcomes']:
        message += '。同快照的测试结果也发生了变化。'
    if imports := scope.get('pytest_import_issues'):
        names = '、'.join(item['module'] for item in imports[:5])
        message += f' · 导入来源需核对 {len(imports)} 项（{names}）'
    return message
