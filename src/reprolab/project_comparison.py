"""Keep test observations separate from conclusions about a frozen comparison."""
import ast
import hashlib
from pathlib import Path

from ._assertion_sites import assertion_site_digest, assertions_by_scope
from .case import parse_test_target, valid_source_mode_hashes
from .project_failure_waivers import recorded_failure_fingerprint
from .project_fixture import test_uses_changed_conftest
from .project_input_control import input_changes
from .project_pytest_phases import complete_failure_with_cleanup


DIVERGENT_OUTCOME_LABELS = {'improved': '观察到失败转通过', 'regression_detected': '观察到通过转失败'}
ASSERTION_REVIEW_REASONS = {
    'site_changed': 'pytest_assertion_site_changed',
    'site_ambiguous': 'pytest_assertion_site_ambiguous',
    'legacy_site_unverified': 'pytest_legacy_assertion_site_unverified',
    'legacy_fingerprint_changed': 'pytest_legacy_assertion_unverified',
}

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
    'pytest_assertion_site_changed': '同一 pytest 测试在两版都失败，记录的断言位置不同，需复核。',
    'pytest_assertion_site_ambiguous': '断言重复且两版位置无法可靠对应，需复核。',
    'pytest_legacy_assertion_site_unverified': '旧证据的断言指纹缺少位置出现记录，需复核。',
    'pytest_legacy_assertion_unverified': '旧证据的失败消息指纹不同，缺少可比较的断言位置，需复核。',
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


def _assertion_site_basis(left, right):
    if any(phase.get('assertion_location_ambiguous') for phase in (left, right)):
        return 'site_ambiguous'
    old, new = (phase['assertion_site_sha256'] for phase in (left, right))
    if not old or not new:
        return 'site_unavailable'
    if old != new:
        return 'site_changed'
    old, new = (phase.get('assertion_occurrence') for phase in (left, right))
    if not old or not new:
        return 'legacy_site_unverified'
    if any(old[key] != new[key] for key in ('count', 'scope_sha256')):
        return 'site_ambiguous'
    return 'site_matched' if old['index'] == new['index'] else 'site_changed'


def _legacy_test_sites(report, test_id, cache):
    """Recover counts only when the old digest identifies a frozen shared test assertion."""
    directory = report.get('report_dir')
    if not directory:
        return {}
    path = parse_test_target(test_id)
    if path in cache:
        return cache[path]
    sites = cache[path] = {}
    expected = report.get('shared_tests', {}).get(path)
    if not expected:
        return sites
    side = 'before' if report.get('test_source_control') else 'after'
    root = (Path(directory) / 'evidence/project/inputs' / side).resolve()
    try:
        source = (root / path).resolve()
        if not source.is_relative_to(root):
            return sites
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            return sites
        for (_, name), nodes in assertions_by_scope(ast.parse(content)).items():
            for node in nodes:
                digest = assertion_site_digest(path, name, ast.dump(node.test))
                sites[digest] = sites.get(digest, 0) + 1
    except (OSError, ValueError, RuntimeError, SyntaxError):
        sites.clear()
    return sites


def pytest_assertion_comparisons(report):
    """Compare double-red call assertions without changing their observed outcomes."""
    if report.get('runner', 'pytest') != 'pytest' or any(
            report.get(side, {}).get('status') != 'completed' for side in ('before', 'after')):
        return []
    rows = []
    legacy_sites = {}
    shape = [{'phase': 'call', 'type': 'AssertionError'}]
    for entry in report.get('entries', ()):
        if entry['status'] != 'baseline_failure' or any(
                _failure_shape(entry.get(side, {})) != shape for side in ('before', 'after')):
            continue
        before, after = (entry[side] for side in ('before', 'after'))
        left, right = (next(phase for phase in outcome['phases'] if phase['phase'] == 'call')
                       for outcome in (before, after))
        if 'assertion_site_sha256' in left and 'assertion_site_sha256' in right:
            basis = _assertion_site_basis(left, right)
            if basis == 'legacy_site_unverified' and _legacy_test_sites(
                    report, entry['test_id'], legacy_sites).get(left['assertion_site_sha256']) == 1:
                basis = 'legacy_site_unique'
        else:
            old, new = (recorded_failure_fingerprint(outcome) for outcome in (before, after))
            basis = 'legacy_phase_and_type'
            if old and new:
                basis = 'legacy_fingerprint_matched' if old == new else 'legacy_fingerprint_changed'
        rows.append({'test_id': entry['test_id'], 'basis': basis})
    return rows


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


def comparison_scope(report, *, reviewed_inputs=(), assertion_comparisons=None):
    """Return scope findings without changing the recorded test outcomes."""
    same = all(valid_source_mode_hashes(hashes) and hashes['before'] == hashes['after']
               for hashes in (report.get('source_sha256'), report.get('source_mode_sha256')))
    counts = report.get('case_counts', report.get('counts', {}))
    changes = {key: counts[key] for key in ('improved', 'regression_detected') if counts.get(key)}
    excluded = report.get('excluded_tracked_changes') or 0
    imports = pytest_import_issues(report)
    failures = failure_changes(report)
    if assertion_comparisons is None:
        assertion_comparisons = pytest_assertion_comparisons(report)
    assertion_bases = {row['basis'] for row in assertion_comparisons}
    assertion_reason = next((reason for basis, reason in ASSERTION_REVIEW_REASONS.items()
                             if basis in assertion_bases), None)
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
    elif assertion_reason:
        reason = assertion_reason
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
                   'pytest_input_changed', 'pytest_input_control', 'pytest_parameter_changed', 'pytest_test_source_control',
                   *ASSERTION_REVIEW_REASONS.values()) and report.get('status') != 'regression_detected')


def comparison_verdict(report, scope):
    status = report.get('status', 'attention')
    if comparison_blocked(report, scope):
        return 'attention'
    return status


def _phase_blocker(report, scope, entry):
    versions = [report.get(side) or {} for side in ('before', 'after')]
    identity = entry['test_id']
    if (report.get('status') == 'cancelled'
            or any(run.get('status') != 'completed' or run.get('worker_record_complete') is not True
                   or (run.get('execution_order') is not None and identity not in run['execution_order'])
                   for run in versions)
            or versions[1].get('exit_code') != 1):
        return 'execution_unverified'
    if scope and scope['reason'] in (
            'same_snapshot_divergence', 'excluded_changes_only', 'pytest_import_origin', 'pytest_version_order',
            'pytest_test_source_control', 'pytest_input_control', 'pytest_fixture_control', 'pytest_order_control',
            'pytest_discovery_scope'):
        return scope['reason']
    hashes = (report.get('source_sha256'), report.get('source_mode_sha256'))
    if (not all(valid_source_mode_hashes(value) for value in hashes)
            or not (report.get('shared_tests') or {}).get(parse_test_target(identity))):
        return 'fixed_inputs_unverified'
    if all(value['before'] == value['after'] for value in hashes):
        return 'same_snapshot_divergence'
    before, after = entry['before'], entry['after']
    if (any(outcome.get('input_error') for outcome in (before, after))
            or (before.get('input_digests') or {}) != (after.get('input_digests') or {})):
        return 'pytest_input_changed'
    parameters = [outcome.get('parameter_digests') or {} for outcome in (before, after)]
    if (parameters[0] != parameters[1] or any(value is None for value in parameters[0].values())
            or ('[' in identity.split('::', 1)[-1] and not parameters[0])):
        return 'pytest_parameter_changed'
    if test_uses_changed_conftest(identity, report.get('fixture_changes') or ()):
        return 'pytest_fixture_changed'
    if any(error.get('test_id') == identity for run in versions for error in run.get('resource_errors', ())):
        return 'pytest_resource_error'
    return None


def comparison_view(report, scope):
    """Project the extra pytest verdict without changing the saved observations."""
    if report.get('runner', 'pytest') != 'pytest':
        return report, {}
    decisions = {}
    promoted = set()
    for entry in report.get('entries', ()):
        if not complete_failure_with_cleanup(entry):
            continue
        identity = entry['test_id']
        reason = _phase_blocker(report, scope, entry)
        decisions[identity] = {'status': 'blocked' if reason else 'confirmed',
                               'reason': reason or 'call_failure_with_teardown_failure'}
        if reason is None:
            promoted.add(identity)
    if not promoted:
        return report, decisions
    entries = [{**entry, 'status': 'regression_detected', 'observed_status': entry['status']}
               if entry['test_id'] in promoted else entry for entry in report['entries']]
    counts = dict(report['counts'])
    counts['regression_detected'] = counts.get('regression_detected', 0) + len(promoted)
    counts['inconclusive'] -= len(promoted)
    if not counts['inconclusive']:
        del counts['inconclusive']
    return {**report, 'entries': entries, 'counts': counts, 'status': 'regression_detected'}, decisions


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
