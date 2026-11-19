"""Describe recorded pytest phases independently of lifecycle and comparison verdicts."""
from collections import Counter


PHASES = ('setup', 'call', 'teardown')
OUTCOMES = ('passed', 'failed', 'skipped')
PHASE_BLOCKERS = {
    'execution_unverified': 'Execution evidence is incomplete',
    'fixed_inputs_unverified': 'Frozen source or shared test evidence is missing',
    'pytest_input_changed': 'Recorded business inputs differ',
    'pytest_parameter_changed': 'Recorded parameters differ or cannot be verified',
    'pytest_fixture_changed': 'The test uses a changed fixture configuration',
    'pytest_resource_error': 'The test has a recorded resource error',
}


def recorded_phases(outcome):
    results = {}
    for phase in PHASES:
        found = [event for event in outcome.get('phases') or ()
                 if isinstance(event, dict) and event.get('phase') == phase]
        value = found[0].get('outcome') if len(found) == 1 else None
        if value not in OUTCOMES:
            results[phase] = 'not_observed'
        else:
            results[phase] = 'xfail_marked' if phase == 'call' and found[0].get('xfail') else value
    return results


def complete_failure_with_cleanup(entry):
    """Recognize only a complete clean baseline and call+teardown failure on the candidate."""
    before, after = (entry.get(side) or {} for side in ('before', 'after'))
    if (entry.get('status') != 'inconclusive' or before.get('status') != 'passed'
            or after.get('status') != 'inconclusive'):
        return False
    for result, outcomes in ((before, ('passed', 'passed', 'passed')), (after, ('passed', 'failed', 'failed'))):
        phases = result.get('phases') or ()
        if (any(not isinstance(event, dict) for event in phases)
                or tuple(event.get('phase') for event in phases) != PHASES
                or tuple(event.get('outcome') for event in phases) != outcomes
                or any(event.get('xfail') is not False for event in phases)):
            return False
        if result is after and any(not isinstance(event.get('exception'), dict)
                                   or not isinstance(event['exception'].get('type'), str)
                                   or not event['exception']['type'] for event in phases[1:]):
            return False
    return True


def phase_observations(report, decisions, *, include_test_ids=False, limit=20):
    calls = {side: Counter() for side in ('before', 'after')}
    lifecycle = {side: Counter() for side in calls}
    details = []
    for entry in report.get('entries', ()):
        pair = {}
        for side in calls:
            phases = recorded_phases(entry.get(side) or {})
            pair[side] = phases
            calls[side][phases['call']] += 1
            for phase in ('setup', 'teardown'):
                if phases[phase] in ('failed', 'skipped'):
                    lifecycle[side][f'{phase}_{phases[phase]}'] += 1
        if include_test_ids and (any(p['call'] not in ('passed', 'failed') for p in pair.values())
                                 or any(p[phase] != 'passed' for p in pair.values() for phase in ('setup', 'teardown'))):
            identity = entry['test_id']
            details.append({'test_id': identity, **pair, **({'comparison': decisions[identity]} if identity in decisions else {})})
    summary = {'calls': {key: dict(value) for key, value in calls.items()},
               'lifecycle_issues': {key: dict(value) for key, value in lifecycle.items()},
               'retained_call_regressions': sum(row['status'] == 'confirmed' for row in decisions.values()),
               'blocked_call_regressions': sum(row['status'] == 'blocked' for row in decisions.values())}
    if details:
        summary['details'] = details[:limit]
        summary['omitted'] = max(0, len(details) - limit)
    return summary


def phase_pair_text(entry):
    labels = {'passed': '通过', 'failed': '失败', 'skipped': '跳过',
              'xfail_marked': '有 xfail 标记', 'not_observed': '无观察'}
    sides = [recorded_phases(entry.get(side) or {}) for side in ('before', 'after')]
    return [f'{label} · 基线 {labels[sides[0][phase]]} → 候选 {labels[sides[1][phase]]}'
            for phase, label in (('setup', '准备（setup）'), ('call', '主体（call）'), ('teardown', '清理（teardown）'))]


def phase_overview_text(phases):
    names = {'passed': '通过', 'failed': '失败', 'skipped': '跳过',
             'xfail_marked': '有 xfail 标记', 'not_observed': '无记录'}
    lifecycle_names = {'setup_failed': '准备失败', 'setup_skipped': '准备跳过',
                       'teardown_failed': '清理失败', 'teardown_skipped': '清理跳过'}
    lines = []
    for side, title in (('before', '基线'), ('after', '候选')):
        calls = '，'.join(f'{names[key]} {count}' for key, count in phases['calls'][side].items())
        lifecycle = '，'.join(f'{lifecycle_names[key]} {count}' for key, count in phases['lifecycle_issues'][side].items())
        lines.append(f'{title}主体（call） · {calls or "无记录"}' + (f' · {lifecycle}' if lifecycle else ''))
    return lines
