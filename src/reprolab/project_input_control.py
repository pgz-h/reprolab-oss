"""Replay explicitly captured pytest inputs and check their link to the original run."""
import re
from pathlib import Path

from ._input_samples import sample_digests


CONTROL_FILE = 'input-control.json'


def recorded_inputs(report):
    samples = {}
    for entry in report['entries']:
        for side in ('before', 'after'):
            outcome = entry.get(side, {})
            if outcome.get('input_digests') or outcome.get('input_error'):
                samples[side, entry['test_id']] = (outcome.get('input_digests'), outcome.get('input_error'))
    return samples


def control_metadata(record, selected):
    fields = {'source_report_sha256', 'values'}
    if (not isinstance(record, dict) or record.keys() not in (fields, fields | {'plan_sha256'})
            or any(not isinstance(record[key], str) or not re.fullmatch(r'[0-9a-f]{64}', record[key])
                   for key in record.keys() - {'values'}) or not record['values']):
        raise ValueError('业务输入对照记录无效。')
    digests = sample_digests(record['values'], selected)
    return {**{key: value for key, value in record.items() if key != 'values'},
            'targets': [{'test_id': identity, 'input_digests': digests[identity]}
                        for identity in selected if identity in digests]}


def select_failure_inputs(report, requested):
    from .project_sessions import _read

    if (report.get('runner', 'pytest') != 'pytest' or report['status'] == 'cancelled'
            or report['before']['status'] != 'completed'
            or any(report.get(key) for key in ('input_control', 'fixture_control', 'order_control', 'version_order', 'test_source_control'))):
        raise ValueError('业务输入对照需要原始 pytest 报告及已完成的基线。')
    path = Path(report['report_dir']) / 'before.events.json'
    captured = (_read(path).get('input_samples') or {}).get('values', {})
    eligible = [entry['test_id'] for entry in report['entries']
                if entry['before']['status'] == 'failed' and entry['before'].get('input_digests')]
    identities = list(dict.fromkeys(requested)) if requested else eligible
    if not identities or not set(identities) <= set(eligible):
        raise ValueError('请选择在基线中完整失败、并由 reprolab_input 保存了业务输入的测试。')
    return {'source_report_sha256': report['_report_sha256'],
            'values': {identity: captured[identity] for identity in identities}}


def input_changes(report):
    if report.get('runner', 'pytest') != 'pytest' or report.get('status') == 'cancelled':
        return []
    rows = []
    for entry in report.get('entries', ()):
        old, new = (entry.get(side, {}) for side in ('before', 'after'))
        if not all(result.get('status') in ('passed', 'failed') for result in (old, new)):
            continue
        left, right = old.get('input_digests', {}), new.get('input_digests', {})
        names = sorted(name for name in left.keys() | right.keys() if left.get(name) != right.get(name))
        if names:
            rows.append({'test_id': entry['test_id'], 'status': entry['status'], 'names': names})
    return rows


def _diagnosis(original, replay):
    from .project_comparison import pytest_import_issues
    from .project_review_summary import _review_summary
    from .replay_verification import compare_replay_reports

    metadata = replay.get('input_control')
    proof = compare_replay_reports(original, replay)
    checks = proof['inputs']
    # The explicit sample substitution is the point of the comparison.
    checks.pop('input_control', None)
    checks.pop('recorded_inputs', None)
    if not metadata or any(value is not True for value in checks.values()):
        raise ValueError('业务输入对照的固定源码、选择或运行设置与来源不一致。')
    selected = [target['test_id'] for target in metadata['targets']]
    expected = select_failure_inputs(original, selected)
    if metadata != control_metadata(expected, original['selected_tests']):
        raise ValueError('业务输入对照与来源报告或基线样本不一致。')
    originals = {entry['test_id']: entry for entry in original['entries']}
    replays = {entry['test_id']: entry for entry in replay['entries']}
    blockers = []
    if replay['status'] == 'cancelled' or any(replay[side]['status'] != 'completed' for side in ('before', 'after')):
        blockers.append('execution_incomplete')
    if pytest_import_issues(replay):
        blockers.append('import_provenance')
    rows = []
    for target in metadata['targets']:
        identity = target['test_id']
        entry = replays[identity]
        before, after = (entry[side]['status'] for side in ('before', 'after'))
        consumed = {side: entry[side].get('input_digests') == target['input_digests']
                    and not entry[side].get('input_error') for side in ('before', 'after')}
        original_entry = originals[identity]
        old_type = (original_entry['before'].get('exception') or {}).get('type')
        new_type = (entry['before'].get('exception') or {}).get('type')
        pattern = 'incomplete'
        if not blockers and all(consumed.values()) and after in ('passed', 'failed'):
            if before == 'passed':
                pattern = 'baseline_failure_not_reproduced'
            elif before == 'failed' and old_type and new_type:
                if old_type != new_type:
                    pattern = 'baseline_failure_changed'
                else:
                    pattern = 'candidate_passed' if after == 'passed' else 'candidate_failed'
        rows.append({**target, 'original_candidate': original_entry['after']['status'],
                     'before': before, 'after': after, 'inputs_consumed': consumed, 'pattern': pattern,
                     'original_failure_type': old_type, 'replayed_failure_type': new_type})
    complete = all(row['pattern'] in ('candidate_passed', 'candidate_failed') for row in rows)
    return {'kind': 'project_input_control_check', 'schema_version': 1,
            'status': 'completed' if complete else 'incomplete',
            'source_report_sha256': original['_report_sha256'], 'result_report_sha256': replay['_report_sha256'],
            'original_gate': _review_summary(original, False, 'strict', None)['gate'],
            'conditions_vs_original': proof['conditions'], 'blockers': blockers, 'entries': rows,
            'notice': '核对由测试通过 reprolab_input 请求的 JSON 样本。其他运行条件或实际输入仍须结合项目核对。'}


def inputs_for_promotion(original, replay, plan):
    from .project_review_summary import _review_summary

    if plan.runner != 'pytest' or not plan.targets or plan.protected_inputs:
        raise ValueError('从输入对照生成保护计划时，请提供原始 pytest 修复计划。')
    proof = _diagnosis(original, replay)
    targets = [row['test_id'] for row in proof['entries']]
    if not set(targets) <= set(plan.targets):
        raise ValueError('请用 --test 只对照原计划中的必验修复目标。')
    # This is the checked comparison under the original policy, with only the
    # diagnostic execution label removed. Other scope findings still apply.
    comparison = _review_summary({key: value for key, value in replay.items() if key != 'input_control'},
                                 False, plan.policy, None)['gate']
    proof['controlled_comparison_gate'] = comparison
    if comparison['status'] == 'failed' or any(row['pattern'] == 'candidate_failed' for row in proof['entries']):
        gate = {'status': 'failed', 'exit_code': 1, 'reason': 'confirmed_failure'}
    elif comparison['status'] != 'passed' or proof['status'] != 'completed':
        gate = {'status': 'blocked', 'exit_code': 2, 'reason': 'controlled_comparison_blocked'}
    elif any(value is False for value in proof['conditions_vs_original'].values()):
        gate = {'status': 'blocked', 'exit_code': 2, 'reason': 'recorded_conditions_changed'}
    else:
        gate = {'status': 'passed', 'exit_code': 0, 'reason': 'same_sample_passed'}
    proof['promotion_gate'] = gate
    if gate['exit_code']:
        return None, proof, gate['exit_code']
    saved = select_failure_inputs(original, targets)
    return {**saved, 'control_report_sha256': replay['_report_sha256']}, proof, 0


def protected_input_check(report, plan):
    from ._input_samples import sample_digests

    samples = plan.protected_inputs
    record = plan.fixed_input_record(report['selected_tests'])
    bound = record is not None and report.get('input_control') == control_metadata(record, report['selected_tests'])
    complete = (report['status'] != 'cancelled'
                and all(report[side]['status'] == 'completed' for side in ('before', 'after')))
    entries = {entry['test_id']: entry for entry in report['entries']}
    rows = []
    for identity, digests in sample_digests(samples['values'], plan.required_passes).items():
        entry = entries.get(identity, {})
        consumed = {side: entry.get(side, {}).get('input_digests') == digests
                    and not entry.get(side, {}).get('input_error') for side in ('before', 'after')}
        candidate = entry.get('after', {}).get('status')
        status = candidate if bound and complete and all(consumed.values()) and candidate in ('passed', 'failed') else 'blocked'
        rows.append({'test_id': identity, 'status': status, 'candidate': candidate,
                     'inputs_consumed': consumed, 'input_digests': digests})
    statuses = {row['status'] for row in rows}
    status = 'failed' if 'failed' in statuses else 'blocked' if 'blocked' in statuses else 'passed'
    return {'status': status, 'plan_bound': bound, 'source_report_sha256': samples['source_report_sha256'],
            'control_report_sha256': samples['control_report_sha256'], 'entries': rows}


def run_input_control(location, output, *, requested, python_executable, timeout, confirmed, control):
    from .project_history import _checked_report, report_reference
    from .project_sessions import replay_project_report
    from .replay_verification import open_replay_evidence

    with open_replay_evidence(location, control=control) as source:
        original = _checked_report(source, control=control)
        run = replay_project_report(source, output, python_executable=python_executable, timeout=timeout,
                                    confirmed=confirmed, control=control, expected_report=report_reference(original),
                                    input_tests=requested)
        if not confirmed:
            run['kind'] = 'project_input_control_preview'
            if source != location:
                del run['report_dir']
                run['source_archive'] = str(location.absolute())
            return run, 0
        result = _checked_report(run['report_dir'])
        diagnosis = _diagnosis(original, result)
    return {**run, 'input_diagnosis': diagnosis}, _exit_code(diagnosis, run)


def check_input_control(source, replay, *, control):
    from .replay_verification import checked_replay_pair

    with checked_replay_pair(source, replay, control=control) as (original, result):
        diagnosis = _diagnosis(original, result)
        return diagnosis, _exit_code(diagnosis, result)


def _exit_code(diagnosis, result):
    if result['status'] == 'regression_detected' or diagnosis['original_gate']['status'] == 'failed':
        return 1
    return 0 if diagnosis['status'] == 'completed' else 2
