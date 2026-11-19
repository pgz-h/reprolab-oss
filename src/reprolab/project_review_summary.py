"""Share checked project comparisons and replay results."""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from .acceptance_plan import GATE_POLICIES, _unique_fields, baseline_pin
from .project_archive import archive_preflight_text
from .project_comparison import (
    ASSERTION_REVIEW_REASONS, comparison_blocked, comparison_scope, comparison_verdict, comparison_view,
    pytest_assertion_comparisons, pytest_order_difference, pytest_parameter_changes,
)
from .project_fixture import test_uses_changed_conftest
from .project_input_control import input_changes, protected_input_check
from .project_history import _checked_report, _match_reference, report_reference
from .project_pr_context import comparison_type
from .project_pytest_phases import PHASE_BLOCKERS, phase_observations
from .project_run_scope import public_run_scope, pytest_run_scope
from .project_sessions import saved_report_collection
from .replay_verification import open_replay_evidence
from .workspace import WORKTREE


DETAIL_LIMIT = 20
_HISTORICAL_FORMAT = 'cb56b04'
_PRE_SCOPE_FORMAT = 'pre_scope'
_PRE_PHASE_FORMAT = 'pre_phase'
OUTCOMES = {
    'regression_detected': 'New failure',
    'inconclusive': 'Blocked',
    'baseline_failure': 'Existing failure',
    'improved': 'Improved',
    'regression_passed': 'Passed on both versions',
}
ASSERTION_BASES = {
    'site_changed': 'Different recorded assertion sites · needs review',
    'site_ambiguous': 'Repeated assertion sites cannot be aligned · needs review',
    'legacy_site_unverified': 'Legacy assertion fingerprint lacks occurrence evidence · needs review',
    'legacy_fingerprint_changed': 'Legacy evidence has differing message fingerprints · needs review',
    'site_matched': 'Matching recorded assertion sites',
    'legacy_site_unique': 'Legacy fingerprint matches a unique assertion in the frozen shared test',
    'site_unavailable': 'Assertion site unavailable · phase and type only',
    'legacy_fingerprint_matched': 'Legacy evidence · matching message fingerprints, assertion site unknown',
    'legacy_phase_and_type': 'Legacy evidence · phase and type only',
}
VERDICTS = {
    'regression_detected': 'New failures detected',
    'attention': 'Needs attention',
    'regression_passed': 'Selected scope passed',
    'cancelled': 'Comparison cancelled',
}
RUNNERS = {'pytest': 'Python / pytest', 'go': 'Go', 'vitest': 'Vitest'}
COMPARISONS = {
    'direct': 'Direct version comparison',
    'pr_head': 'PR change review · common ancestor to PR head',
    'pr_merge': 'PR merge acceptance · target commit to verified merge result',
}
RUN_STATUS = {
    'completed': 'Completed',
    'cancelled': 'Cancelled',
    'collection_failed': 'Collection failed',
    'dependencies_failed': 'Dependency setup failed',
    'not_run': 'Not run',
    'timed_out': 'Timed out',
    'worker_error': 'Incomplete execution record',
}
REPLAY_STATUS = {
    'verified': 'Selected outcomes reproduced',
    'different': 'Selected outcomes changed',
    'incomplete': 'Replay verification incomplete',
    'inputs_changed': 'Frozen inputs differ',
    'same_run': 'Both reports describe the same run',
}
REPLAY_RESULTS = {'matched': 'Matched', 'different': 'Changed', 'incomplete': 'Incomplete'}
REPLAY_INPUTS = {
    'runner': 'Runner', 'sources': 'Source snapshots', 'permissions': 'File permissions',
    'selection': 'Test selection and order', 'shared_tests': 'Shared tests', 'scope': 'Test scope',
    'test_source_control': 'Baseline test files', 'input_control': 'Fixed baseline business inputs', 'recorded_inputs': 'Recorded business inputs',
    'pytest_order': 'Pytest order control', 'version_order': 'Pytest version order experiment', 'fixture_control': 'Baseline fixture replay',
}
REPLAY_CONDITIONS = {
    'reprolab': 'ReproLab version', 'timeout': 'Per-version timeout', 'platform': 'OS and architecture',
    'toolchain': 'Toolchain versions', 'distributions': 'Python packages',
    'runtime_variables': 'Runtime variables', 'locale_timezone': 'Locale and time zone', 'offline': 'Offline install mode', 'network_isolation': 'OS network isolation',
}


def _gate(report, policy, counts, case_counts, runs, scope):
    if comparison_blocked(report, scope):
        return {'policy': policy, 'status': 'blocked', 'exit_code': 2, 'reason': scope['reason']}
    verdict = report['status']
    if verdict == 'regression_detected':
        return {'policy': policy, 'status': 'failed', 'exit_code': 1, 'reason': 'new_failures'}
    if verdict == 'regression_passed':
        return {'policy': policy, 'status': 'passed', 'exit_code': 0, 'reason': 'selected_scope_passed'}
    existing_only = (
        verdict == 'attention'
        and counts.get('baseline_failure')
        and not counts.get('inconclusive')
        and all(status == 'completed' for status in runs.values())
        and (report.get('runner') != 'vitest' or (
            case_counts and case_counts.get('baseline_failure') and not case_counts.get('inconclusive')
        ))
    )
    if policy == 'no-new-failures' and existing_only:
        return {'policy': policy, 'status': 'passed', 'exit_code': 0, 'reason': 'existing_failures_allowed'}
    reason = 'existing_failures' if existing_only else 'blocked_or_incomplete'
    return {'policy': policy, 'status': 'blocked', 'exit_code': 2, 'reason': reason}


def _required_improvements(report, targets, vitest_cases, scope, *, reviewed_inputs=()):
    runner = report.get('runner', 'pytest')
    if (runner == 'vitest' and targets) or (runner != 'vitest' and vitest_cases):
        raise ValueError('pytest 和 Go 使用 --require-improvement；Vitest 使用 --require-vitest-case FILE CASE。')
    identities = [(None, target) for target in targets] + list(map(tuple, vitest_cases))
    if len(set(identities)) != len(identities):
        raise ValueError('同一个测试不能重复列为必须改善。')
    if runner == 'vitest':
        selected = {(entry['test_id'], case['test_id']): case
                    for entry in report['entries'] for case in entry.get('case_entries', ())}
    else:
        selected = {(None, entry['test_id']): entry for entry in report['entries']}
    completed = report['status'] != 'cancelled' and all(
        report[side]['status'] == 'completed' for side in ('before', 'after'))
    changed_inputs = {row['test_id'] for row in input_changes(report)
                      if row['status'] == 'improved' and row['test_id'] not in reviewed_inputs}
    changed_parameters = {row['test_id'] for row in pytest_parameter_changes(report) if row['status'] == 'improved'}
    results = []
    for file, target in identities:
        record = selected.get((file, target))
        outcome = record['status'] if record else None
        reason = scope['reason'] if scope else None
        if reason in ('pytest_input_changed', 'pytest_fixture_changed', 'pytest_parameter_changed'):
            reason = None  # These findings apply only to the affected targets.
        if report.get('version_order'):
            reason = 'pytest_version_order'
        elif report.get('test_source_control'):
            reason = 'pytest_test_source_control'
        elif report.get('input_control'):
            reason = 'pytest_input_control'
        elif target in changed_parameters:
            reason = 'pytest_parameter_changed'
        elif target in changed_inputs:
            reason = 'pytest_input_changed'
        elif report.get('fixture_control'):
            reason = 'pytest_fixture_control'
        elif outcome == 'improved' and report.get('fixture_changes') and test_uses_changed_conftest(
                target, report['fixture_changes']):
            reason = 'pytest_fixture_changed'
        blocked_by_scope = reason in ('excluded_changes_only', 'pytest_import_origin', 'pytest_order_control', 'pytest_version_order',
                                      'pytest_fixture_control', 'pytest_fixture_changed', 'pytest_input_control', 'pytest_input_changed', 'pytest_parameter_changed', 'pytest_test_source_control') or (
            reason == 'same_snapshot_divergence' and outcome in ('improved', 'regression_detected'))
        if blocked_by_scope or not completed or outcome in (None, 'inconclusive'):
            status = 'blocked'
        elif outcome == 'improved':
            status = 'passed'
        else:
            status = 'failed'
        row = {'test_id': target, 'status': status, 'outcome': outcome}
        if file is not None:
            row['file'] = file
        if blocked_by_scope:
            row['reason'] = reason
        results.append(row)
    return results


def _required_passes(report, plan, scope):
    if plan.runner == 'vitest':
        entries = {(entry['test_id'], case['test_id']): case
                   for entry in report['entries'] for case in entry.get('case_entries', ())}
    else:
        entries = {(None, entry['test_id']): entry for entry in report['entries']}
    goals = plan.required_passes if plan.runner == 'vitest' else ((None, target) for target in plan.required_passes)
    blocked = (report['status'] == 'cancelled' or report['after']['status'] != 'completed'
               or report.get('order_control') or report.get('version_order') or report.get('fixture_control')
               or report.get('input_control')
               or comparison_blocked(report, scope))
    rows = []
    for file, identity in goals:
        entry = entries.get((file, identity))
        outcome = entry['after']['status'] if entry else None
        comparable = entry and entry['status'] != 'inconclusive'
        if comparable and outcome == 'failed' and report['after']['status'] == 'completed' and report['status'] != 'cancelled':
            status = 'failed'
        else:
            status = 'passed' if comparable and outcome == 'passed' and not blocked else 'blocked'
        rows.append({'test_id': identity, 'status': status, 'outcome': outcome,
                     **({'file': file} if file is not None else {})})
    return rows


def _approved_scope(report, approved):
    selected = report['selected_tests']
    required, actual = set(approved), set(selected)
    missing = [item for item in approved if item not in actual]
    return {'status': 'blocked' if missing else 'passed', 'approved_count': len(approved),
            'selected_count': len(selected), 'missing_tests': missing,
            'added_tests': [item for item in selected if item not in required],
            'common_order_changed': ([item for item in approved if item in actual]
                                     != [item for item in selected if item in required])}


def _required_receipt(source, receipt, digest, *, control):
    from .replay_receipt import check_replay_receipt

    try:
        checked = check_replay_receipt(source, receipt, control=control)
    except (OSError, ValueError):
        return {'status': 'blocked', 'receipt_status': 'unavailable',
                'issues': [{'part': 'receipt', 'reason': 'unavailable'}]}
    proof = checked['replay_verification']
    if proof is not None and proof['source']['report_sha256'] != digest:
        return {'status': 'blocked', 'receipt_status': 'unavailable',
                'issues': [{'part': 'source', 'reason': 'changed'}]}
    result = {'status': 'blocked', 'receipt_status': checked['status'],
              'issues': [{key: issue[key] for key in ('part', 'reason')} for issue in checked['issues']]}
    if proof is not None:
        result.update(verification_status=proof['status'], counts=proof['counts'], conditions=proof['conditions'],
                      replay_report_sha256=proof['replay']['report_sha256'])
        if checked['status'] == 'consistent':
            result['status'] = {'verified': 'passed', 'different': 'failed'}.get(proof['status'], 'blocked')
    return result


def _required_stability(source, summary, expected, minimum, *, control):
    from .stability_history import check_saved_stability

    try:
        checked = check_saved_stability(source, summary, expected_report=expected, control=control)
    except (OSError, ValueError):
        return {'status': 'blocked', 'verification_status': 'unavailable', 'minimum_rounds': minimum}
    status = 'blocked'
    if checked['completed_rounds'] >= minimum:
        status = {'verified': 'passed', 'different': 'failed'}.get(checked['status'], 'blocked')
    return {'status': status, 'verification_status': checked['status'],
            'minimum_rounds': minimum, 'requested_rounds': checked['requested_rounds'],
            'completed_rounds': checked['completed_rounds'], 'counts': checked['counts'],
            'unverified_rounds': [issue['round'] for issue in checked['history_issues']],
            'round_report_sha256': [item['verification']['replay']['report_sha256'] for item in checked['rounds']]}


def _check_baseline(report, expected):
    actual = baseline_pin(report)
    if actual is None:
        reason = 'missing'
    elif actual != expected:
        reason = 'different'
    else:
        reason = 'matched'
    return {'status': 'passed' if reason == 'matched' else 'blocked', 'reason': reason,
            'expected': expected.record(), 'actual': actual.record() if actual is not None else None}


def _counts(counts):
    return {status: counts[status] for status in OUTCOMES if counts.get(status)}


def _label(value):
    text = ''.join(character if character.isprintable() else ' ' for character in value)
    return text if len(text) <= 200 else text[:199] + '…'


def _identity_label(value, *, redact_parameters=False):
    digest = hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:16]
    display = value
    if redact_parameters and '::' in value:
        file, _, test = value.partition('::')
        name, separator, _ = test.partition('[')
        if separator:
            display = f'{file}::{name}[sha256:{digest}]'
    if len(display) > 200:
        return _label(display[:180]) + '…#' + digest
    return _label(display)


def _new_failure_details(report, runner):
    found = []
    for entry in report['entries']:
        if entry['status'] != 'regression_detected':
            continue
        cases = [case for case in entry.get('case_entries', ()) if case['status'] == 'regression_detected']
        for case in cases or (entry,):
            row = {'test_id': _identity_label(case['test_id'], redact_parameters=runner == 'pytest')}
            if cases and runner == 'vitest':
                row['file'] = _identity_label(entry['test_id'])
            found.append(row)
    return {'items': found[:DETAIL_LIMIT], 'omitted': max(0, len(found) - DETAIL_LIMIT)}


def _details(entries, *, include_cases=True, display=_label):
    rows = {status: [] for status in OUTCOMES if status != 'regression_passed'}
    total = 0
    for entry in entries:
        cases = (entry.get('case_entries') or []) if include_cases else []
        for row in cases or (entry,):
            status = row['status']
            if status not in rows:
                continue
            total += 1
            if len(rows[status]) < DETAIL_LIMIT:
                label = {'test_id': display(row['test_id']), 'status': status}
                if cases:
                    label['file'] = display(entry['test_id'])
                rows[status].append(label)
    items = [item for group in rows.values() for item in group][:DETAIL_LIMIT]
    return {'items': items, 'omitted': total - len(items)}


def _revision(commit, digest):
    if commit == WORKTREE:
        return {'kind': 'working_tree', 'sha256': digest}
    if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        return {'kind': 'commit', 'commit': commit}
    return {'kind': 'snapshot', 'sha256': digest}


def _as_of(value):
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError('报告评估时间须包含时区偏移。')
    return instant


def review_summary(directory, **options):
    """Summarize checked evidence and optionally enforce repair and replay requirements."""
    return _review_summaries(directory, **options)['current']


def _review_summaries(directory, *, include_test_ids=False, policy=None, expected_report=None,
                   required_improvements=(), required_vitest_cases=(), receipt=None, stability=None,
                   min_stability_rounds=None, plan=None, control=None, _reviewed_inputs=(),
                   expected_sources=None, expected_plan_sha256=None, public=False, waiver_file=None, waiver_as_of=None,
                   max_report_age=None, report_as_of=None, approval=(None, None, None), include_historical=False):
    if plan is not None:
        if policy is not None or required_improvements or required_vitest_cases or min_stability_rounds is not None:
            raise ValueError('使用验收计划时，回归门槛、测试目标和最少轮数由计划指定。')
        policy = plan.policy
        if plan.runner == 'vitest':
            required_vitest_cases = plan.targets
        else:
            required_improvements = plan.targets
        min_stability_rounds = plan.min_stability_rounds
    if stability is not None or (min_stability_rounds is not None and plan is None):
        from .replay_stability import MAX_ROUNDS, MIN_ROUNDS

        if stability is None:
            raise ValueError('--min-stability-rounds 需要同时指定 --require-stability。')
        if min_stability_rounds is None:
            min_stability_rounds = MIN_ROUNDS
        if type(min_stability_rounds) is not int or not MIN_ROUNDS <= min_stability_rounds <= MAX_ROUNDS:
            raise ValueError(f'最少稳定性轮数须为 {MIN_ROUNDS}–{MAX_ROUNDS}。')
    if expected_plan_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', expected_plan_sha256):
        raise ValueError('计划指纹须为 64 字符的 SHA-256。')
    if (waiver_file is None) != (waiver_as_of is None):
        raise ValueError('失败豁免和评估日期须同时提供。')
    if waiver_file is not None and policy != 'no-new-failures':
        raise ValueError('失败豁免须使用 no-new-failures 门禁。')
    if (max_report_age is None) != (report_as_of is None):
        raise ValueError('请同时提供报告最长时限与评估时间。')
    if max_report_age is not None and not 1 <= max_report_age <= 87600:
        raise ValueError('报告最长时限须为 1–87600 小时。')
    evaluation_time = _as_of(report_as_of) if report_as_of is not None else None
    received_approval = any(item is not None for item in approval)
    if received_approval and (plan is None or not all(approval)):
        raise ValueError('校验审查批准须同时提供验收计划、签名、可信公钥和预期身份。')
    requirements = bool(required_improvements or required_vitest_cases or receipt is not None
                        or stability is not None or plan is not None
                        or expected_sources or expected_plan_sha256 is not None or waiver_file is not None or evaluation_time is not None or received_approval)
    if requirements and policy is None:
        policy = 'strict'
    with open_replay_evidence(directory, control=control) as source:
        report = _checked_report(source, control=control)
        if expected_report is not None:
            _match_reference(report, expected_report)
        if plan is not None and report.get('runner', 'pytest') != plan.runner:
            raise ValueError(f'验收计划使用 {plan.runner}，当前报告使用 {report.get("runner", "pytest")}。')
        protected = protected_input_check(report, plan) if plan and plan.protected_inputs is not None else None
        if protected and protected['plan_bound']:
            # In this evaluation the exact plan already accounts for input substitution.
            report = {key: value for key, value in report.items() if key != 'input_control'}
        formats = ('current', _PRE_PHASE_FORMAT, _HISTORICAL_FORMAT) if include_historical else ('current',)
        shared = {}
        if protected:
            shared['protected_inputs'] = protected
        if plan is not None:
            shared['acceptance_plan_sha256'] = plan.fingerprint()
        bindings = {}
        for side, expected in (expected_sources or {}).items():
            if expected.startswith('sha256:'):
                actual = report.get('source_sha256', {}).get(side)
                actual = 'sha256:' + actual if actual else None
            else:
                actual = report.get(side + '_commit')
            bindings[side] = {'expected': expected, 'actual': actual, 'matches': expected == actual}
        if expected_plan_sha256 is not None:
            actual_plan = shared.get('acceptance_plan_sha256')
            bindings['plan'] = {'expected': expected_plan_sha256, 'actual': actual_plan,
                                'matches': expected_plan_sha256 == actual_plan}
        if evaluation_time is not None:
            actual = report.get('created_at')
            try:
                age = evaluation_time - _as_of(actual)
                fresh = timedelta() <= age <= timedelta(hours=max_report_age)
            except (TypeError, ValueError):
                fresh = False
            bindings['age'] = {'expected': f'{report_as_of} · max {max_report_age}h',
                               'actual': actual, 'matches': fresh}
        if bindings:
            shared['expected_bindings'] = bindings
        if waiver_file is not None:
            from .project_failure_waivers import check_failure_waivers

            shared['failure_waivers'] = check_failure_waivers(report, waiver_file, waiver_as_of)
        if received_approval:
            from .project_attestation import required_review_approval

            document, key, actor = approval
            shared['review_approval'] = required_review_approval(
                report, plan, document=document, key=key, actor=actor, control=control)
        if receipt is not None:
            shared['receipt'] = _required_receipt(report['report_dir'], receipt, report['_report_sha256'], control=control)
        elif plan is not None and plan.require_receipt:
            shared['receipt'] = {'status': 'blocked', 'receipt_status': 'missing', 'issues': []}
        if stability is not None:
            shared['stability'] = _required_stability(report['report_dir'], stability, report_reference(report),
                                                      min_stability_rounds, control=control)
        elif plan is not None and min_stability_rounds is not None:
            shared['stability'] = {'status': 'blocked', 'verification_status': 'missing',
                                   'minimum_rounds': min_stability_rounds}
        if plan is not None and plan.test_contract is not None:
            from .project_test_contract import check_test_contract

            shared['test_contract'] = check_test_contract(report, plan, control=control)
        results = {}
        for summary_format in formats:
            summary = _review_summary(report, include_test_ids, policy, None,
                                      reviewed_inputs=_reviewed_inputs, historical=summary_format == _HISTORICAL_FORMAT,
                                      phase_evidence=summary_format == 'current')
            if summary_format != _HISTORICAL_FORMAT and summary['runner'] == 'pytest':
                summary['run_scope'] = pytest_run_scope(report, collection=saved_report_collection(report),
                                                        include_omitted=include_test_ids and not public)
            summary.update(shared)
            if requirements:
                _apply_requirements(summary, report, required_improvements, required_vitest_cases,
                                    plan=plan, reviewed_inputs=_reviewed_inputs)
            results[summary_format] = _public_summary(summary) if public else summary
        return results


def _apply_requirements(summary, report, improvements, vitest_cases, *, plan, reviewed_inputs=()):
    comparison = summary['gate']
    checks = [comparison['status']]
    if plan is not None and plan.baseline is not None:
        summary['baseline_check'] = _check_baseline(report, plan.baseline)
        checks.append(summary['baseline_check']['status'])
    if improvements or vitest_cases:
        targets = _required_improvements(report, improvements, vitest_cases, summary.get('comparison_scope'),
                                         reviewed_inputs=reviewed_inputs)
        summary['required_improvements'] = targets
        checks.extend(row['status'] for row in targets)
    if plan is not None and plan.approved_selection is not None:
        summary['approved_scope'] = _approved_scope(report, plan.approved_selection)
        checks.append(summary['approved_scope']['status'])
    if plan is not None and plan.required_passes:
        targets = _required_passes(report, plan, summary.get('comparison_scope'))
        summary['required_passes'] = targets
        checks.extend(row['status'] for row in targets)
    checks.extend(summary[key]['status'] for key in
                  ('protected_inputs', 'test_contract', 'receipt', 'stability', 'review_approval', 'failure_waivers')
                  if key in summary)
    if any(not binding['matches'] for binding in summary.get('expected_bindings', {}).values()):
        checks.append('blocked')
    status = 'failed' if 'failed' in checks else 'blocked' if 'blocked' in checks else 'passed'
    code = {'passed': 0, 'failed': 1, 'blocked': 2}[status]
    summary['comparison_gate'] = comparison
    summary['gate'] = {'policy': comparison['policy'], 'status': status, 'exit_code': code,
                       'reason': 'acceptance_requirements'}


def _review_summary(report, include_test_ids, policy, expected_report, *, reviewed_inputs=(), historical=False, phase_evidence=True):
    if expected_report is not None:
        _match_reference(report, expected_report)
    runner = report.get('runner', 'pytest')
    if runner not in RUNNERS or report['status'] not in VERDICTS:
        raise ValueError('此项目报告的执行器或判定无法生成审查摘要。')
    runs = {side: report[side]['status'] if report[side]['status'] in RUN_STATUS else 'unknown'
            for side in ('before', 'after')}
    cases = _counts(report['case_counts']) if runner == 'vitest' and report.get('vitest_report_version', 1) >= 2 else None
    # cb56b04 compared failure phase and type only and had no assertion-site section.
    assertions = [] if historical else pytest_assertion_comparisons(report)
    scope = comparison_scope(report, reviewed_inputs=reviewed_inputs, assertion_comparisons=assertions)
    observed = report
    if phase_evidence and not historical:
        report, decisions = comparison_view(observed, scope)
    counts = _counts(report['counts'])
    result = {
        'kind': 'project_review_summary',
        'schema_version': 1,
        'report_sha256': report['_report_sha256'],
        'runner': runner,
        **({} if historical else {'comparison_type': comparison_type(report)}),
        'verdict': comparison_verdict(report, scope),
        'revisions': {side: _revision(report.get(side + '_commit'), report['source_sha256'][side])
                      for side in runs},
        'runs': runs,
        'selected_count': len(report['selected_tests']),
        'counts': counts,
    }
    if scope:
        result['comparison_scope'] = {key: value for key, value in scope.items() if key != 'failure_changes'}
        result['observed_verdict'] = observed['status']
    if report is not observed:
        result['observed_verdict'] = observed['status']
        result['observed_counts'] = _counts(observed['counts'])
    if runner == 'pytest' and phase_evidence and not historical:
        result['pytest_phases'] = phase_observations(observed, decisions, include_test_ids=include_test_ids, limit=DETAIL_LIMIT)
    if discovery := report.get('discovery_audit'):
        result['discovery_audit'] = {key: value for key, value in discovery.items() if key != 'baseline_only'}
        result['discovery_audit']['baseline_only_count'] = len(discovery['baseline_only'])
        if include_test_ids:
            result['discovery_audit']['baseline_only'] = discovery['baseline_only'][:DETAIL_LIMIT]
    if scope and (changes := scope.get('failure_changes')):
        result['failure_change_count'] = len(changes)
        if include_test_ids:
            result['failure_changes'] = changes[:DETAIL_LIMIT]
    if assertions:
        result['assertion_comparison'] = {'counts': dict(Counter(row['basis'] for row in assertions))}
        if include_test_ids:
            result['assertion_comparison']['items'] = sorted(
                assertions, key=lambda row: row['basis'] not in ASSERTION_REVIEW_REASONS)[:DETAIL_LIMIT]
    if runner == 'pytest' and include_test_ids and report.get('review_summary_version', 1) >= 2:
        from .project_failure_waivers import recorded_existing_failures

        if failures := recorded_existing_failures(report):
            result['existing_failure_fingerprints'] = failures[:DETAIL_LIMIT]
    if cases is not None:
        result['case_counts'] = cases
    if archive_preflight_text(report.get('archive_preflight')):
        result['archive_preflight'] = report['archive_preflight']
    if report.get('pr_context'):
        result['pr_context'] = report['pr_context']
    if report.get('snapshot_scope'):
        result['snapshot_scope'] = report['snapshot_scope']
    if report.get('clock_control'):
        result['clock_control'] = report['clock_control']
    if report.get('network_isolation'):
        result['network_isolation'] = report['network_isolation']
    if runner == 'go':
        omitted = {side: report[side].get('omitted_output_chars', 0) for side in ('before', 'after')}
        if any(omitted.values()):
            result['go_omitted_output_chars'] = omitted
        subtests = [case for entry in report['entries'] for case in entry.get('case_entries', ())]
        if subtests:
            result['go_subtests'] = {
                'counts': dict(Counter(case['status'] for case in subtests)),
            }
            if include_test_ids:
                result['go_subtests']['attention'] = [
                    {'test_id': case['test_id'], 'status': case['status']}
                    for case in subtests if case['status'] != 'regression_passed'
                ][:DETAIL_LIMIT]
    if runner == 'pytest':
        resources = {}
        for side in ('before', 'after'):
            record = report[side]
            observed = {key: record[key] for key in ('worker_resources', 'termination_signal') if key in record}
            if errors := record.get('resource_errors'):
                observed['resource_error_count'] = len(errors)
                if include_test_ids:
                    observed['resource_errors'] = errors[:DETAIL_LIMIT]
            if observed:
                resources[side] = observed
        if resources:
            result['pytest_resources'] = resources
        timeouts = {}
        for side in ('before', 'after'):
            version = report[side]
            if report.get('review_summary_version', 1) >= 2 and version['status'] == 'timed_out':
                progress = version.get('progress') or {}
                phase = progress.get('current_phase')
                timeouts[side] = {'last_phase': phase or 'startup_or_between_tests',
                                  'completed_tests': progress.get('completed', 0)}
                if include_test_ids and progress.get('current_test'):
                    timeouts[side]['test_id'] = _identity_label(progress['current_test'])
        if timeouts:
            result['pytest_timeouts'] = timeouts
        if parameters := pytest_parameter_changes(report):
            result['parameter_change_count'] = len(parameters)
            if include_test_ids:
                result['parameter_changes'] = parameters[:DETAIL_LIMIT]
        if changed := input_changes(report):
            result['input_change_count'] = len(changed)
            if include_test_ids:
                result['input_changes'] = changed[:DETAIL_LIMIT]
        if source := report.get('test_source_control'):
            result['test_source_control'] = source
        if metadata := report.get('input_control'):
            result['input_control'] = {'source_report_sha256': metadata['source_report_sha256'],
                                       'target_count': len(metadata['targets'])}
        if report.get('fixture_changes'):
            result['fixture_changes'] = report['fixture_changes']
        if fixture := report.get('fixture_control'):
            result['fixture_control'] = fixture
        if version_order := report.get('version_order'):
            result['version_order'] = version_order
        rules = {side: report.get(side, {}).get('pytest_rules') for side in ('before', 'after')}
        if any(rule and (rule['warning_filters'] or rule['other_addopts_ignored']) for rule in rules.values()):
            result['pytest_rules'] = rules
        if order := pytest_order_difference(report):
            result['pytest_order_difference'] = order
    project_dir = report.get('vitest_project_dir', '.')
    if runner == 'vitest' and project_dir != '.':
        result['vitest_project_dir'] = project_dir
    if runner == 'vitest' and (report.get('vitest_install') or {}).get('workspaces'):
        result['vitest_install'] = report['vitest_install']
    version = report.get('review_summary_version', 1)
    if version >= 2 and counts.get('regression_detected') and not include_test_ids:
        result['new_failure_tests'] = _new_failure_details(report, runner)
    if include_test_ids:
        display = _identity_label if version >= 2 else _label
        result['details'] = _details(report['entries'], include_cases=runner != 'go', display=display)
        if not historical:
            result['selected_tests'] = [display(identity) for identity in report['selected_tests'][:DETAIL_LIMIT]]
    if policy is not None:
        if policy not in GATE_POLICIES:
            raise ValueError('未知的项目回归门槛。')
        result['gate'] = _gate(report, policy, counts, cases, runs, scope)
    return result


def _code(text):
    fence = '`' * (1 + max((len(match) for match in re.findall(r'`+', text)), default=0))
    return f'{fence} {text} {fence}'


def _revision_text(revision):
    if revision['kind'] == 'commit':
        return _code(revision['commit'])
    kind = 'Working-tree snapshot' if revision['kind'] == 'working_tree' else 'Snapshot'
    return f'{kind} · {_code(revision["sha256"])}'


def _public_summary(summary):
    fields = ('runner', 'comparison_type', 'verdict', 'selected_count', 'counts', 'case_counts', 'report_sha256',
              'acceptance_plan_sha256', 'gate')
    result = {'kind': 'project_review_public_summary', 'schema_version': 1,
              **{key: summary[key] for key in fields if key in summary}}
    if assertions := summary.get('assertion_comparison'):
        result['assertion_comparison'] = {'counts': assertions['counts']}
    if scope := summary.get('run_scope'):
        result['run_scope'] = public_run_scope(scope)
    if phases := summary.get('pytest_phases'):
        result['pytest_phases'] = {key: phases[key] for key in
                                   ('calls', 'lifecycle_issues', 'retained_call_regressions', 'blocked_call_regressions')}
    return result


def _assertion_markdown(summary):
    if not (assertions := summary.get('assertion_comparison')):
        return []
    counts = assertions['counts']
    lines = ['', '**Tests failing with AssertionError on both versions**', '']
    lines.extend(f'- {label} · {counts[basis]}' for basis, label in ASSERTION_BASES.items() if basis in counts)
    lines.extend(f'- {_code(_identity_label(row["test_id"]))} · {ASSERTION_BASES[row["basis"]]}'
                 for row in assertions.get('items', ()))
    return lines


def _run_scope_markdown(scope):
    discovery, recommendations = scope['discovery'], scope['recommendations']

    def number(value, accuracy):
        return 'unknown' if value is None else f'at least {value}' if accuracy == 'at_least' else str(value)

    status = {'complete': 'complete', 'partial': 'partial', 'truncated': 'truncated',
              'partial_truncated': 'partial and truncated', 'unknown': 'completeness unknown'}[discovery['status']]
    line = ('- Discovery · unknown' if discovery['count'] is None
            else f'- Discovery · {number(discovery["count"], discovery["accuracy"])} tests · {status}')
    if path := discovery.get('path'):
        line += ' · ' + _code(_label(path))
    lines = [line]
    if recommendations['status'] == 'recorded':
        lines.append(f'- Recommendations · {recommendations["count"]} recorded, '
                     f'{recommendations["selected_count"]} selected, {recommendations["omitted_count"]} omitted'
                     + (' · outcomes of omitted tests unknown' if recommendations['omitted_count'] else ''))
    else:
        lines.append('- Recommendations · unknown')
    for side, label in (('before', 'Baseline execution'), ('after', 'Candidate execution')):
        run = scope['execution'][side]
        started, finished = (number(run[key], run['accuracy']) for key in ('started', 'finished'))
        line = f'- {label} · {RUN_STATUS.get(run["status"], "Unknown")} · started {started}, finished records {finished}'
        if run['not_started']:
            line += f', {run["not_started"]} did not start'
        lines.append(line)
    if omitted := recommendations.get('omitted_tests'):
        lines.extend(['', '**Omitted recommendations**', ''])
        lines.extend(f'- {_code(_identity_label(item["test_id"]))} · {_code(_label(item["reason"]))}'
                     for item in omitted)
        if remaining := recommendations['omitted_count'] - len(omitted):
            lines.append(f'- {remaining} additional omitted recommendations in the saved discovery record.')
    return lines


def _phase_markdown(phases):
    lines = ['', '**Pytest test body and lifecycle observations**', '']
    labels = {'passed': 'passed', 'failed': 'failed', 'skipped': 'skipped',
              'xfail_marked': 'marked xfail', 'not_observed': 'no call recorded'}
    for side, title in (('before', 'Baseline'), ('after', 'Candidate')):
        body = phases['calls'][side]
        lifecycle = phases['lifecycle_issues'][side]
        line = f'- {title} call · ' + ', '.join(f'{label} {body[key]}' for key, label in labels.items() if key in body)
        if lifecycle:
            line += ' · ' + ', '.join(f'{name.replace("_", " ")} {count}' for name, count in lifecycle.items())
        lines.append(line)
    if retained := phases['retained_call_regressions']:
        lines.append(f'- {retained} new call failure(s) retained with a teardown failure on the same test.')
    if blocked := phases['blocked_call_regressions']:
        lines.append(f'- {blocked} call pass-to-fail observation(s) with teardown failure require review.')
    for row in phases.get('details', ()):
        phases_text = (' / '.join(f'{name} {outcome.replace("_", " ")}' for name, outcome in row[side].items())
                       for side in ('before', 'after'))
        left, right = phases_text
        lines.append(f'- {_code(_identity_label(row["test_id"]))} · baseline {left} → candidate {right}')
        if decision := row.get('comparison'):
            if decision['status'] == 'blocked':
                reason = decision['reason']
                lines.append(f'  · Needs review · {PHASE_BLOCKERS.get(reason, reason.replace("_", " "))}')
    if phases.get('omitted'):
        lines.append(f'- {phases["omitted"]} additional phase detail(s) in the full report.')
    return lines


def _render_public_markdown(summary, *, historical=False):
    lines = ['### ReproLab public comparison summary', '']
    if not historical:
        lines.append(COMPARISONS[summary.get('comparison_type', 'direct')])
    lines.extend([f'**{VERDICTS[summary["verdict"]]}** · {RUNNERS[summary["runner"]]}',
                  f'Selected scope · {summary["selected_count"]}'])
    gate_line = None
    if gate := summary.get('gate'):
        gate_line = f'**CI gate** · {gate["status"].title()} · {_code(gate["reason"])}'
    if not historical and gate_line:
        lines.extend(['', gate_line])
    if scope := summary.get('run_scope'):
        lines.extend(['', *_run_scope_markdown(scope)])
    lines.extend(['', '| Outcome | Count |', '| --- | ---: |'])
    lines.extend(f'| {name} | {summary["counts"].get(key, 0)} |' for key, name in OUTCOMES.items())
    lines.extend(_assertion_markdown(summary))
    if phases := summary.get('pytest_phases'):
        lines.extend(_phase_markdown(phases))
    if historical and gate_line:
        lines.extend(['', gate_line])
    if plan := summary.get('acceptance_plan_sha256'):
        lines.extend(['', f'Acceptance plan SHA-256 · {_code(plan)}'])
    lines.extend(['', f'Report SHA-256 · {_code(summary["report_sha256"])}',
                  '', 'This aggregate summary can be checked by a recipient who also has the original evidence.'])
    return '\n'.join(lines) + '\n'


def render_review_markdown(summary, *, historical=False):
    if summary.get('kind') == 'project_review_public_summary':
        return _render_public_markdown(summary, historical=historical)
    project_dir = summary.get('vitest_project_dir')
    install = summary.get('vitest_install')
    verdict = VERDICTS[summary['verdict']]
    heading = verdict if historical else COMPARISONS[summary.get('comparison_type', 'direct')]
    lines = ['### ReproLab comparison', '', f'**{heading}** · {RUNNERS[summary["runner"]]}', '']
    gate_line = None
    if gate := summary.get('gate'):
        suffix = ' · Existing failures allowed by policy' if gate['reason'] == 'existing_failures_allowed' else ''
        gate_line = f'**CI gate** · {gate["status"].title()} ({_code(gate["policy"])}){suffix}'
    if not historical:
        if gate_line:
            lines.extend([gate_line, ''])
        lines.append(f'**{verdict}**')
    lines.extend([
        f'- Baseline · {_revision_text(summary["revisions"]["before"])} · '
        + RUN_STATUS.get(summary['runs']['before'], 'Unknown'),
        f'- Candidate · {_revision_text(summary["revisions"]["after"])} · '
        + RUN_STATUS.get(summary['runs']['after'], 'Unknown'),
        f'- Scope · {summary["selected_count"]} selected '
        + ('test files' if summary['runner'] == 'vitest' else 'tests')
        + (f' in {_code(project_dir)}' if project_dir else '')
        + (f' · npm workspace root {_code(install["root"])}' if install else ''),
    ])
    if selected := summary.get('selected_tests'):
        lines.append('- Selected · ' + ', '.join(_code(name) for name in selected)
                     + (' …' if len(selected) < summary['selected_count'] else ''))
    if run_scope := summary.get('run_scope'):
        lines.extend(_run_scope_markdown(run_scope))
    lines.extend([
        '',
        '| Outcome | ' + ('Files | Cases |' if 'case_counts' in summary else 'Count |'),
        '| --- | ---: |' + (' ---: |' if 'case_counts' in summary else ''),
    ])
    labels = OUTCOMES
    if summary.get('comparison_scope', {}).get('divergent_outcomes'):
        labels = {**OUTCOMES, 'improved': 'Observed fail-to-pass', 'regression_detected': 'Observed pass-to-fail'}
    for status, title in labels.items():
        cells = [title, str(summary['counts'].get(status, 0))]
        if 'case_counts' in summary:
            cells.append(str(summary['case_counts'].get(status, 0)))
        lines.append('| ' + ' | '.join(cells) + ' |')
    if phases := summary.get('pytest_phases'):
        lines.extend(_phase_markdown(phases))
    if failures := summary.get('new_failure_tests'):
        observed = summary.get('comparison_scope', {}).get('divergent_outcomes')
        lines.extend(['', '**Observed pass-to-fail tests**' if observed else '**Newly failing tests**', ''])
        for row in failures['items']:
            lines.append('- ' + ' · '.join(_code(row[key]) for key in ('file', 'test_id') if key in row))
        if failures['omitted']:
            lines.append(f'- {failures["omitted"]} additional result(s) in the execution report.')
    if scope := summary.get('comparison_scope'):
        if changes := scope['divergent_outcomes']:
            lines.extend(['', '**Identical frozen snapshots produced different outcomes.** '
                          f'Observed fail-to-pass: {changes.get("improved", 0)}. '
                          f'Observed pass-to-fail: {changes.get("regression_detected", 0)}. '
                          'Check runtime conditions and external state before attributing the change.'])
        if excluded := scope['excluded_tracked_changes']:
            lines.extend(['', f'**Tracked paths excluded from the comparison: {excluded}.** '
                          + ('The frozen snapshots are identical.' if scope['same_snapshot'] else
                             'The results cover the changes included in the snapshots.')])
        if imports := scope.get('pytest_import_issues'):
            lines.extend(['', f'**Pytest import provenance needs review.** {len(imports)} module(s) '
                          'changed from project source to an external origin or did not meet a snapshot requirement.'])
        if order_mode := scope.get('order_mode'):
            label = 'baseline observed order' if order_mode == 'baseline' else 'original requested order'
            lines.extend(['', f'**Pytest order control.** Both versions were configured to use the {label}.'])
    if omitted := summary.get('go_omitted_output_chars'):
        lines.extend(['', f'**Go output excerpts** · {omitted["before"]} baseline and {omitted["after"]} candidate'
                      ' output characters omitted from the structured excerpts. Full logs remain in the evidence.'])
    if subtests := summary.get('go_subtests'):
        counts = subtests['counts']
        lines.extend(['', '**Go subtests** · ' + ' · '.join(
            f'{title} {counts[status]}' for status, title in OUTCOMES.items() if status in counts)])
        for item in subtests.get('attention', ()):
            lines.append(f'- {_code(_label(item["test_id"]))} · {OUTCOMES[item["status"]]}')
    if audit := summary.get('discovery_audit'):
        lines.extend(['', f'**Pytest discovery** · {audit["status"]} · baseline {audit["before_count"]}'
                      f' · candidate {audit["after_count"]} · baseline only {audit["baseline_only_count"]}'])
        if audit['status'] != 'complete':
            lines.append('A complete inventory comparison is unavailable.')
        for item in audit.get('baseline_only', ()):
            reason = {'source_file_absent': 'baseline file absent', 'not_discovered': 'absent from discovery',
                      'possible_rename': 'possible rename'}[item['reason']]
            lines.append(f'- Baseline only · {_code(_label(item["test_id"]))} · {reason}')
            for match in item['possible_matches']:
                lines.append(f'  - Candidate · {_code(_label(match))}')
    if changed := summary.get('failure_change_count'):
        lines.extend(['', f'**Failure type or phase changed** · {changed} test(s)'])
        for change in summary.get('failure_changes', ()):
            name = ' · '.join(_code(_label(change[key])) for key in ('file', 'test_id') if key in change)
            before, after = (' + '.join(f'{row["phase"]} / {row["type"]}' for row in change[side])
                             for side in ('before', 'after'))
            lines.append(f'- {name} · {_code(_label(before))} → {_code(_label(after))}')
    lines.extend(_assertion_markdown(summary))
    if resources := summary.get('pytest_resources'):
        lines.extend(['', '**Pytest worker resources**', ''])
        for side, record in resources.items():
            fields = []
            if measurements := record.get('worker_resources'):
                fields.append(f'worker-only peak RSS {measurements["peak_rss_bytes"]} bytes')
                cpu = measurements['user_cpu_seconds'] + measurements['system_cpu_seconds']
                fields.append(f'worker CPU {cpu:.3f} s')
            if signal := record.get('termination_signal'):
                fields.append(f'terminated by signal {signal} · cause unknown')
            if count := record.get('resource_error_count'):
                fields.append(f'{count} recorded resource error(s)')
            lines.append(f'- {side.title()} · ' + ' · '.join(fields))
    if timeouts := summary.get('pytest_timeouts'):
        lines.extend(['', '**Pytest timeouts**', ''])
        for side, item in timeouts.items():
            phase = item['last_phase'].replace('_', ' ')
            name = f' · {_code(item["test_id"])}' if item.get('test_id') else ''
            lines.append(f'- {side.title()} · last observed {phase} · completed {item["completed_tests"]}{name}')
    if count := summary.get('parameter_change_count'):
        lines.extend(['', f'**Pytest parametrization changed** · {count} test(s)', ''])
        for item in summary.get('parameter_changes', ()):
            names = ', '.join(_code(_label(name)) for name in item['names'])
            lines.append(f'- {_code(_label(item["test_id"]))} · {names}')
    if count := summary.get('input_change_count'):
        lines.extend(['', f'**Recorded business inputs changed** · {count} test(s)', ''])
        for item in summary.get('input_changes', ()):
            names = ', '.join(_code(_label(name)) for name in item['names'])
            lines.append(f'- {_code(_label(item["test_id"]))} · {names}')
    if context := summary.get('pr_context'):
        lines.extend(['', f'**GitHub PR context** · {_code(context["mode"])}',
                      f'Event SHA-256 · {_code(context["event_sha256"])}'])
    if scope := summary.get('snapshot_scope'):
        lines.extend(['', '**Source snapshot limited to selected paths**', ''])
        lines.extend('- ' + _code(_label(path)) for path in scope[:DETAIL_LIMIT])
    if clock := summary.get('clock_control'):
        lines.extend(['', f'**Fixed Python clock** · {_code(clock["instant"])}'])
        for key in ('timezone', 'locale'):
            if clock[key] is not None:
                lines.append(f'- {key.title()} · {_code(clock[key])}')
    if isolation := summary.get('network_isolation'):
        if isolation['mechanism'] == 'windows_container_none':
            lines.extend(['', '**OS network isolation** · Windows container with no external network interface'])
        elif isolation['mechanism'] == 'linux_network_namespace':
            lines.extend(['', '**OS network isolation** · distinct Linux namespace with loopback only'])
        else:
            lines.extend(['', '**OS network isolation** · TCP bind and connect probes denied',
                          f'Policy SHA-256 · {_code(isolation["profile_sha256"])}'])
    if source := summary.get('test_source_control'):
        lines.extend(['', '**Baseline test files executed on both versions**',
                      f'Source report SHA-256 · {_code(source["source_report_sha256"])}'])
    if controlled := summary.get('input_control'):
        lines.extend(['', f'**Fixed baseline business inputs** · {controlled["target_count"]} test(s)',
                      f'Source report SHA-256 · {_code(controlled["source_report_sha256"])}'])
    if fixtures := summary.get('fixture_changes'):
        lines.extend(['', '**Pytest fixture files changed** · '
                      + ', '.join(_code(_label(item['path'])) for item in fixtures[:20])])
    if control := summary.get('fixture_control'):
        lines.extend(['', '**Baseline fixture replay**',
                      f'Source report SHA-256 · {_code(control["source_report_sha256"])}'])
        for item in control['files']:
            lines.append(f'- {_code(_label(item["path"]))} · baseline SHA-256 {_code(item["before_sha256"])}')
    if version_order := summary.get('version_order'):
        positions = ' → '.join('A (baseline)' if side == 'before' else 'B (candidate)'
                               for side in version_order['started_versions']) or 'none'
        lines.extend(['', f'**Pytest version order experiment** · planned {version_order["group"].upper()}'
                      f' · started {positions}',
                      f'Source report SHA-256 · {_code(version_order["source_report_sha256"])}'])
    if archive := summary.get('archive_preflight'):
        lines.extend(['', '**Evidence ZIP path preflight.** '
                      f'{archive["blocked_count"]} blocking path(s), {archive["portability_count"]} portability issue(s). '
                      'See the report for the affected paths.'])
    if rules := summary.get('pytest_rules'):
        lines.extend(['', '**Pytest project options**'])
        for side, rule in rules.items():
            if rule:
                omitted = 'other addopts ignored' if rule['other_addopts_ignored'] else 'no other addopts ignored'
                lines.append(f'- {side.title()} · {rule["warning_filters"]} warning filter(s) used · {omitted}')
    if order := summary.get('pytest_order_difference'):
        lines.extend(['', f'**Pytest execution order differs.** {order["positions_changed"]} of '
                      f'{order["common_tests"]} common tests changed position. See the execution report for the sequences.'])
    if historical and gate_line:
        lines.extend(['', gate_line])
    if plan_digest := summary.get('acceptance_plan_sha256'):
        lines.extend(['', f'Acceptance plan SHA-256 · {_code(plan_digest)}'])
    if comparison := summary.get('comparison_gate'):
        lines.extend(['', f'- Comparison policy · {comparison["status"].title()}'
                      f' · {_code(comparison["reason"])}'])
    if baseline := summary.get('baseline_check'):
        label = {'matched': 'Frozen baseline matches', 'different': 'Frozen baseline differs',
                 'missing': 'Baseline fingerprints unavailable'}[baseline['reason']]
        lines.append(f'- Pinned baseline · {baseline["status"].title()} · {label}')
        for field, title in (('source_sha256', 'Source'), ('source_mode_sha256', 'File modes')):
            expected = _code(baseline['expected'][field])
            actual = baseline['actual']
            line = f'  · {title} SHA-256 · {expected}'
            if actual is not None and actual[field] != baseline['expected'][field]:
                line += f' · observed {_code(actual[field])}'
            lines.append(line)
    if targets := summary.get('required_improvements'):
        for row in targets:
            name = ' · '.join(_code(_label(row[key])) for key in ('file', 'test_id') if key in row)
            outcome = OUTCOMES.get(row['outcome'], 'Missing comparable result')
            if row.get('reason') == 'same_snapshot_divergence':
                outcome = ('Observed fail-to-pass' if row['outcome'] == 'improved' else 'Observed pass-to-fail')
            elif row.get('reason') == 'excluded_changes_only':
                outcome = 'Tracked changes were excluded from the snapshots'
            elif row.get('reason') == 'pytest_import_origin':
                outcome = 'Observed import origins need review'
            elif row.get('reason') in ('pytest_input_control', 'pytest_input_changed', 'pytest_parameter_changed'):
                outcome = 'Recorded business inputs require review'
            elif row.get('reason') in ('pytest_fixture_control', 'pytest_fixture_changed'):
                outcome = 'Fixture inputs require review'
            elif row.get('reason') == 'pytest_version_order':
                outcome = 'Outcome observed in a version order experiment'
            elif row.get('reason') == 'pytest_order_control':
                outcome = 'Outcome observed under fixed-order control'
            lines.append(f'- Required improvement · {name} · {row["status"].title()} · {outcome}')
    if approved := summary.get('approved_scope'):
        lines.append(f'- Approved regression scope · {approved["status"].title()} · approved {approved["approved_count"]}'
                     f' · selected {approved["selected_count"]} · missing {len(approved["missing_tests"])}'
                     f' · added {len(approved["added_tests"])}')
        for key, title in (('missing_tests', 'Missing'), ('added_tests', 'Added')):
            lines.extend(f'  - {title} · {_code(_label(identity))}' for identity in approved[key][:20])
        if approved['common_order_changed']:
            lines.append('  - The order of common tests changed.')
    for row in summary.get('required_passes', ()):
        name = ' · '.join(_code(_label(row[key])) for key in ('file', 'test_id') if key in row)
        lines.append(f'- Required continued pass · {name} · {row["status"].title()}'
                     f' · candidate {_code(row["outcome"] or "missing")}')
    if approval := summary.get('review_approval'):
        details = f' · {_code(_label(approval["actor"]))}' if approval.get('actor') else ''
        lines.append(f'- Signed review decision · {approval["status"].title()}'
                     f' · {_code(approval["reason"])}{details}')
    if waived := summary.get('failure_waivers'):
        lines.append(f'- Failure waivers · {waived["status"].title()} · evaluated {_code(waived["as_of"])}')
        lines.append(f'  - Waiver file SHA-256 · {_code(waived["waivers_sha256"])}')
        for row in waived['entries'][:DETAIL_LIMIT]:
            lines.append(f'  - {_code(_label(row["test_id"]))} · {row["reason"].replace("_", " ")}')
    for name, binding in summary.get('expected_bindings', {}).items():
        label = {'before': 'Expected baseline', 'after': 'Expected candidate', 'plan': 'Approved plan', 'age': 'Report age'}[name]
        lines.append(f'- {label} · {"Matched" if binding["matches"] else "Blocked"}'
                     f' · expected {_code(binding["expected"])} · observed {_code(binding["actual"] or "missing")}')
    if protected := summary.get('protected_inputs'):
        count = len(protected['entries'])
        status = protected['status'].title()
        binding = 'plan matched' if protected['plan_bound'] else 'plan binding missing or different'
        lines.extend([f'- Protected failure inputs · {status} · {count} test(s) · {binding}',
                      f'  - Original report SHA-256 · {_code(protected["source_report_sha256"])}',
                      f'  - Input control report SHA-256 · {_code(protected["control_report_sha256"])}'])
        for row in protected['entries'][:DETAIL_LIMIT]:
            lines.append(f'  - {_code(_label(row["test_id"]))} · {row["status"].title()}'
                         f' · candidate {_code(row["candidate"] or "missing")}')
    if contract := summary.get('test_contract'):
        lines.append(f'- Test contract · {contract["status"].title()} · {contract["file_count"]} pinned file(s)')
        lines.append(f'  - Approved report SHA-256 · {_code(contract["source_report_sha256"])}')
        for change in contract['changes'][:DETAIL_LIMIT]:
            lines.append(f'  - {_code(_label(change["path"]))} · {change["reason"]}')
    if receipt := summary.get('receipt'):
        verification = receipt.get('verification_status')
        label = REPLAY_STATUS.get(verification, 'Replay evidence unavailable')
        material = receipt['receipt_status'].title()
        lines.append(f'- Required replay receipt · {receipt["status"].title()} · {material} · {label}')
        if counts := receipt.get('counts'):
            lines.append('  · ' + ' · '.join(f'{name} {counts[key]}' for key, name in REPLAY_RESULTS.items()))
        if digest := receipt.get('replay_report_sha256'):
            lines.append(f'  · Replay report SHA-256 · {_code(digest)}')
        conditions = receipt.get('conditions', {})
        for value, title in ((False, 'Different conditions'), (None, 'Unknown conditions')):
            labels = [name for key, name in REPLAY_CONDITIONS.items()
                      if key in conditions and conditions[key] is value]
            if labels:
                lines.append(f'  · {title} · ' + ', '.join(labels))
        for issue in receipt['issues']:
            lines.append(f'  · {_code(issue["part"])} · {issue["reason"].title()}')
    stability = summary.get('stability')
    if stability and stability['verification_status'] in ('missing', 'unavailable'):
        label = ('No evidence supplied' if stability['verification_status'] == 'missing'
                 else 'Evidence could not be read or verified')
        lines.append(f'- Required replay stability · Blocked · {label} · minimum {stability["minimum_rounds"]}')
    elif stability:
        label = {'verified': 'Every round reproduced', 'different': 'Outcomes varied or consistently differed',
                 'incomplete': 'Verification incomplete', 'cancelled': 'Run cancelled'}[stability['verification_status']]
        lines.append(f'- Required replay stability · {stability["status"].title()} · {label}'
                     f' · {stability["completed_rounds"]}/{stability["requested_rounds"]} checked rounds'
                     f' · minimum {stability["minimum_rounds"]}')
        counts = stability['counts']
        lines.append(f'  · Matched {counts["matched"]} · Consistently different {counts["consistently_different"]}'
                     f' · Variable {counts["variable"]} · Incomplete {counts["incomplete"]}')
        if unverified := stability['unverified_rounds']:
            lines.append('  · Could not verify rounds ' + ', '.join(map(str, unverified)))
    if details := summary.get('details'):
        lines.append('')
        for item in details['items']:
            file = _code(item['file']) + ' · ' if 'file' in item else ''
            lines.append(f'- **{labels[item["status"]]}** · {file}{_code(item["test_id"])}')
        if details['omitted']:
            lines.append(f'- {details["omitted"]} additional results in the full report.')
    lines.extend(['', 'The comparison covers the selected scope. '
                   'An improvement records a fail-to-pass change.',
                   '', f'Report SHA-256 · {_code(summary["report_sha256"])}'])
    return '\n'.join(lines) + '\n'


def _matching_markdown(location, candidates):
    path = Path(location).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError('请选择普通 Markdown 摘要文件。')
    # Accept CRLF while bounding reads to the largest equivalent representation.
    limit = max(len(expected) + expected.count(b'\n') for expected in candidates.values())
    with path.open('rb') as stream:
        received = stream.read(limit + 1)
    if len(received) > limit:
        return None
    received = received.replace(b'\r\n', b'\n')
    return next((name for name, expected in candidates.items() if received == expected), None)

def markdown_matches(markdown, location):
    """Compare a bounded read with Markdown generated from checked evidence."""
    return _matching_markdown(location, {'current': markdown.encode('utf-8')}) == 'current'


def check_review_summary(directory, location, *, facts=None, **options):
    """Check every supplied artifact against a complete supported reconstruction."""
    summaries = _review_summaries(directory, **options, include_historical=True)
    current = summaries['current']
    previous = summaries[_PRE_PHASE_FORMAT]
    if 'run_scope' in previous:
        summaries[_PRE_SCOPE_FORMAT] = {key: value for key, value in previous.items() if key != 'run_scope'}
    markdown = {name: render_review_markdown(summary, historical=name == _HISTORICAL_FORMAT).encode('utf-8')
                for name, summary in summaries.items()}
    markdown_format = _matching_markdown(location, markdown)
    result = {'kind': 'project_review_summary_check', 'schema_version': 1,
              'status': 'consistent' if markdown_format else 'different',
              'report_sha256': current['report_sha256'],
              'expected_markdown_sha256': hashlib.sha256(markdown['current']).hexdigest()}
    if markdown_format:
        result['markdown_format'] = markdown_format
        result['matched_markdown_sha256'] = hashlib.sha256(markdown[markdown_format]).hexdigest()
    if markdown_format == _HISTORICAL_FORMAT and 'gate' in summaries[_HISTORICAL_FORMAT]:
        result['historical_gate'] = summaries[_HISTORICAL_FORMAT]['gate']
    if markdown_format in (_PRE_PHASE_FORMAT, _PRE_SCOPE_FORMAT) and previous.get('gate') != current.get('gate'):
        result['historical_gate'] = previous['gate']
    if facts is not None:
        path = Path(facts).expanduser()
        limit = 16 * 1024 * 1024
        if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
            raise ValueError('请选择不超过 16 MiB 的普通摘要 JSON。')
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
        try:
            if len(raw) > limit:
                raise ValueError('摘要 JSON 超过 16 MiB。')
            canonical = json.dumps(json.loads(raw, object_pairs_hook=_unique_fields), sort_keys=True, ensure_ascii=True)
        except (ValueError, RecursionError, UnicodeError) as exc:
            raise ValueError('收到的摘要 JSON 无效。') from exc
        result['markdown_status'] = result['status']
        facts_format = next((name for name, summary in summaries.items()
                             if canonical == json.dumps(summary, sort_keys=True, ensure_ascii=True)), None)
        result['facts_status'] = 'consistent' if facts_format else 'different'
        if facts_format:
            result['facts_format'] = facts_format
        if not facts_format or facts_format != markdown_format:
            result['status'] = 'different'
        if markdown_format and facts_format and markdown_format != facts_format:
            result['reason'] = 'summary_format_mismatch'
    for field in ('acceptance_plan_sha256', 'gate'):
        if field in current:
            result[field] = current[field]
    return result


def _replay_pair(pair):
    if pair is None:
        return 'Missing record'
    return f'{pair["before"].replace("_", " ").capitalize()} → {pair["after"].replace("_", " ")}'


def render_replay_markdown(verification, *, include_test_ids=False):
    """Render a shareable receipt from checked evidence without raw environment metadata."""
    status = verification['status']
    lines = ['### ReproLab replay verification', '', f'**{REPLAY_STATUS[status]}** · {RUNNERS[verification["runner"]]}', '']
    if counts := verification['counts']:
        lines.extend(['| Selected outcomes | Count |', '| --- | ---: |'])
        lines.extend(f'| {label} | {counts[key]} |' for key, label in REPLAY_RESULTS.items())
        lines.append('')
    for field, labels, title in (
        ('inputs', REPLAY_INPUTS, 'Frozen inputs'), ('conditions', REPLAY_CONDITIONS, 'Recorded conditions'),
    ):
        checks = verification[field]
        if not checks:
            continue
        changed = [labels[key] for key in labels if key in checks and checks[key] is False]
        unknown = [labels[key] for key in labels if key in checks and checks[key] is None]
        if changed:
            lines.append(f'- {title} with differences · ' + ', '.join(changed))
        if unknown:
            lines.append(f'- {title} with unknown values · ' + ', '.join(unknown))
        if not changed and not unknown:
            lines.append(f'- {title} · All checks matched')
    lines.append('')
    failure_changes = sum(row.get('reason') in ('failure_shape_changed', 'recorded_failure_changed')
                          for row in verification.get('entries', ()))
    if failure_changes:
        caption = 'Recorded failure evidence' if verification.get('recorded_failure_identity') else 'Failure type or phase'
        lines.extend([f'{caption} changed for {failure_changes} test(s).', ''])
    if include_test_ids and (entries := verification['entries']):
        lines.append('Selected tests (baseline → candidate)')
        lines.append('')
        ordered = sorted(entries, key=lambda row: row['status'] == 'matched')
        for row in ordered[:DETAIL_LIMIT]:
            name = ' · '.join(_code(_label(row[key])) for key in ('file', 'project', 'test_id') if row.get(key))
            lines.append(f'- **{REPLAY_RESULTS[row["status"]]}** · {name} · '
                         f'Source {_replay_pair(row["source"])} · Replay {_replay_pair(row["replay"])}')
        if len(entries) > DETAIL_LIMIT:
            lines.append(f'- {len(entries) - DETAIL_LIMIT} additional results in the full verification.')
        lines.append('')
    for label, key in (('Source', 'source'), ('Replay', 'replay')):
        record = verification[key]
        lines.append(f'- {label} report SHA-256 · {_code(record["report_sha256"])}'
                     f' · {VERDICTS[record["verdict"]]}')
    lines.extend(['', 'Verification covers recorded outcomes in the selected scope.'])
    return '\n'.join(lines) + '\n'
