"""Compare the known scenario of each pytest candidate without exposing captured values."""
from collections import defaultdict

from ._clock_control import compare_clock_controls
from .project_input_control import recorded_inputs


_SIDES = ('before', 'after')
_FIELDS = ('recorded_inputs', 'parameters')


def _status(states):
    return next((status for status in ('different', 'unverified', 'matched') if status in states), 'not_recorded')


def _compare_inputs(left, right, *, required=False):
    left, left_error = left
    right, right_error = right
    left, right = left or {}, right or {}
    if left_error or right_error:
        return 'unverified'
    if not (left or right):
        return 'unverified' if required else 'not_recorded'
    if any(left[name] is not None and right[name] is not None and left[name] != right[name]
           for name in left.keys() & right.keys()):
        return 'different'
    if left.keys() != right.keys() or None in left.values() or None in right.values():
        return 'unverified'
    return 'matched'


def _observations(report):
    parameters = {}
    for entry in report['entries']:
        for side in _SIDES:
            if values := entry[side].get('parameter_digests'):
                parameters[side, entry['test_id']] = (values, None)
    return {'recorded_inputs': recorded_inputs(report), 'parameters': parameters}


def _clock_evidence(report):
    if report.get('clock_control') is None:
        return 'not_recorded'
    return 'verified' if all(report[side].get('status') == 'completed'
                             and report[side].get('worker_record_complete') is True for side in _SIDES) else 'unverified'


def _compare_clocks(rows, issues):
    if len(rows) < 2:
        return 'not_comparable'
    reference = next((row for row in rows if row['clock_evidence'] == 'verified'), None)
    states = set()
    if all(row['clock_evidence'] == 'not_recorded' for row in rows):
        return 'not_recorded'
    for row in rows:
        if row['clock_evidence'] != 'verified':
            states.add('unverified')
            issue = {'name': row['name'], 'reason': 'clock_control_unverified', 'scope': 'between_candidates',
                     'message': '缺少两版已执行固定时钟的记录，无法确认与其他候选处于同一固定场景。'}
        elif not compare_clock_controls(reference['clock_control'], row['clock_control']):
            states.add('different')
            changed = [key for key in ('instant', 'timezone', 'locale')
                       if row['clock_control'][key] != reference['clock_control'][key]]
            issue = {'name': row['name'], 'reason': 'clock_control_changed', 'scope': 'between_candidates',
                     'changed_fields': changed, 'message': '候选报告采用了不同的固定时钟设置。'}
        else:
            states.add('matched')
            continue
        if reference is not None:
            issue['reference'] = reference['name']
        issues.append(issue)
    return _status(states)


def compare_candidate_scenarios(rows, eligible, reports, shared, issues):
    observations = {}
    for row in rows:
        report = reports[row['report_sha256']][0]
        if report.get('runner', 'pytest') != 'pytest':
            continue
        row['clock_control'] = report.get('clock_control')
        row['clock_evidence'] = _clock_evidence(report)
        observations[row['name']] = _observations(report)
        row['input_comparison'] = {}
    clock = _compare_clocks(eligible, issues)
    result = {'clock_control': clock}
    rerun_tests = set()
    for field in _FIELDS:
        within_states = set()
        between_states = set()
        differences = defaultdict(list)
        for row in eligible:
            name = row['name']
            records = observations[name][field]
            states = set()
            for test in row['selected_tests']:
                required = field == 'parameters' and '[' in test.split('::', 1)[-1]
                before, after = (records.get((side, test), (None, None)) for side in _SIDES)
                status = _compare_inputs(before, after, required=required)
                states.add(status)
                if status in ('different', 'unverified'):
                    differences['within_candidate', name, None, None, status].append(test)
            row['input_comparison'][field] = _status(states)
            within_states.update(states)
        if len(eligible) >= 2:
            for side in _SIDES:
                for test in sorted(shared):
                    required = field == 'parameters' and '[' in test.split('::', 1)[-1]
                    values = [(row['name'], observations[row['name']][field].get((side, test), (None, None)))
                              for row in eligible]
                    reference, expected = next(((name, value) for name, value in values if value[0] and not value[1]), values[0])
                    for name, value in values:
                        status = _compare_inputs(expected, value, required=required)
                        between_states.add(status)
                        if status in ('different', 'unverified'):
                            differences['between_candidates', name, reference, side, status].append(test)
        for (scope, name, reference, side, status), tests in differences.items():
            reason = field + ('_changed' if status == 'different' else '_unverified')
            label = '业务样本' if field == 'recorded_inputs' else 'pytest 参数'
            message = (f'记录的{label}不同，须对齐后复跑。' if status == 'different'
                       else f'{label}证据不全，须补齐同一输入后复跑。')
            issue = {'name': name, 'reason': reason, 'scope': scope, 'test_ids': tests, 'message': message}
            if reference is not None and name != reference:
                issue['reference'] = reference
            if side is not None:
                issue['side'] = side
            issues.append(issue)
            rerun_tests.update(tests)
        result[field] = {'within_candidates': _status(within_states),
                         'between_candidates': _status(between_states) if len(eligible) >= 2 and shared else 'not_comparable'}
    if clock in ('different', 'unverified'):
        rerun_tests.update(shared)
    result['tests_requiring_aligned_inputs'] = sorted(rerun_tests)
    return result
