"""Verify the old/new selected-test comparison using linked report evidence."""
import re

from .runner import RunControl


def validate_test_source_control(record):
    if record is not None and (not isinstance(record, dict)
                               or record.keys() != {'source_report_sha256', 'tests_from'}
                               or record['tests_from'] != 'before'
                               or not isinstance(record['source_report_sha256'], str)
                               or not re.fullmatch(r'[0-9a-f]{64}', record['source_report_sha256'])):
        raise ValueError('测试来源对照记录无效。')


def compare_test_sources(original, controlled):
    from .replay_verification import compare_replay_reports

    metadata = controlled.get('test_source_control')
    if metadata != {'source_report_sha256': original['_report_sha256'], 'tests_from': 'before'}:
        raise ValueError('测试来源对照与原报告指纹不符。')
    proof = compare_replay_reports(original, controlled)
    inputs = proof['inputs']
    for key in ('shared_tests', 'test_source_control', 'recorded_inputs'):
        inputs.pop(key, None)
    if not all(value is True for value in inputs.values()):
        raise ValueError('测试来源对照的固定实现、选择或已声明控制与原报告不一致。')

    rows = []
    old_entries = {entry['test_id']: entry for entry in controlled['entries']}
    for entry in original['entries']:
        identity = entry['test_id']
        rows.append({'test_id': identity,
                     'candidate_tests': {side: entry[side]['status'] for side in ('before', 'after')},
                     'baseline_tests': {side: old_entries[identity][side]['status'] for side in ('before', 'after')}})
    complete = all(value in ('passed', 'failed') for row in rows
                   for field in ('baseline_tests', 'candidate_tests') for value in row[field].values())
    completed_runs = all(report['status'] != 'cancelled' and all(report[side]['status'] == 'completed'
                         for side in ('before', 'after')) for report in (original, controlled))
    return {'kind': 'project_test_source_check', 'schema_version': 1,
            'status': 'completed' if complete and completed_runs and False not in proof['conditions'].values() else 'incomplete',
            'source_report_sha256': original['_report_sha256'], 'result_report_sha256': controlled['_report_sha256'],
            'entries': rows, 'conditions': proof['conditions'],
            'notice': '两组仅切换所选测试文件。每版的实现、配置及辅助文件照原比较运行；外部状态可能仍有变化。'}


def run_test_source_control(location, output, *, python_executable=None, timeout=None, confirmed=False, control=None):
    from .project_history import _checked_report, report_reference
    from .project_sessions import replay_project_report
    from .replay_verification import open_replay_evidence

    control = control or RunControl()
    with open_replay_evidence(location, control=control) as source:
        original = _checked_report(source, control=control)
        run = replay_project_report(source, output, python_executable=python_executable, timeout=timeout,
                                    confirmed=confirmed, control=control, expected_report=report_reference(original),
                                    baseline_tests=True)
        if not confirmed:
            run['kind'] = 'project_test_source_preview'
            if source != location:
                del run['report_dir']
                run['source_archive'] = str(location.absolute())
            return run, 0
        diagnosis = compare_test_sources(original, _checked_report(run['report_dir']))
    return {**run, 'test_source_diagnosis': diagnosis}, _exit_code(original, run, diagnosis)


def check_test_source_control(location, result, *, control):
    from .replay_verification import checked_replay_pair

    with checked_replay_pair(location, result, control=control) as (original, controlled):
        diagnosis = compare_test_sources(original, controlled)
        return diagnosis, _exit_code(original, controlled, diagnosis)


def _exit_code(original, controlled, diagnosis):
    if 'regression_detected' in (original['status'], controlled['status']):
        return 1
    return 0 if diagnosis['status'] == 'completed' else 2
