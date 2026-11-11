"""Evidence diagnosis must explain actual pytest phases without changing verdicts."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from reprolab.runtime import child_environment


def worker_run(tmp_path, text, *, target=None):
    from reprolab import _worker
    source = tmp_path / 'source'
    source.mkdir()
    test = source / 'test_teaching.py'
    test.write_text(text, encoding='utf-8')
    output = tmp_path / 'events.json'
    completed = subprocess.run(
        [sys.executable, '-I', str(Path(_worker.__file__)), str(source), str(test),
         str(output), json.dumps({'test_target': target})],
        env=child_environment(), capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode in (0, 1, 2, 4, 5), completed.stderr
    return json.loads(output.read_text(encoding='utf-8'))


@pytest.mark.parametrize(('text', 'phase', 'kind', 'message', 'name'), [
    ('import reprolab_teaching_missing_xyz\ndef test_one(): pass\n', 'collection',
     'ModuleNotFoundError', "No module named 'reprolab_teaching_missing_xyz'", 'reprolab_teaching_missing_xyz'),
    ('def test_one():\n    raise ValueError("teaching failure")\n', 'call',
     'ValueError', 'teaching failure', None),
    ('import pytest\n@pytest.fixture\ndef broken():\n    raise RuntimeError("fixture unavailable")\ndef test_one(broken): pass\n',
     'setup', 'RuntimeError', 'fixture unavailable', None),
    ('import pytest\n@pytest.fixture\ndef broken():\n    yield\n    raise RuntimeError("cleanup unavailable")\ndef test_one(broken): pass\n',
     'teardown', 'RuntimeError', 'cleanup unavailable', None),
])
def test_worker_records_actual_collection_and_phase_exceptions(tmp_path, text, phase, kind, message, name):
    raw = worker_run(tmp_path, text)
    evidence = raw.get('diagnostic_exceptions', [])
    assert evidence, 'worker must preserve structured exceptions, including collection failures'
    assert evidence[0] == dict(phase=phase, type=kind, message=message, **({'name': name} if name else {}))
    if phase == 'collection':
        assert raw['collection_errors']
        assert raw['events'] == []
    else:
        event = next(event for event in raw['events'] if event['phase'] == phase)
        assert event['exception']['type'] == kind


def test_worker_bounds_diagnostic_exception_messages(tmp_path):
    raw = worker_run(tmp_path, 'def test_one():\n    raise ValueError("x" * 20000)\n')
    evidence = raw.get('diagnostic_exceptions', [])
    assert evidence and len(evidence[0]['message']) <= 6000


def run_record(kind='passed', exception=None, **updates):
    events = [{'phase': phase, 'outcome': 'failed' if phase == 'call' and kind == 'failed' else 'passed',
               'exception': exception if phase == 'call' else None} for phase in ('setup', 'call', 'teardown')]
    run = {'kind': kind, 'failure': exception, 'test_id': 'regression.py::test_one',
           'collected': 1, 'exit_code': 1 if kind == 'failed' else 0, 'timed_out': False,
           'phase_events': events, 'diagnostic_exceptions': [], 'collection_errors': [], 'has_xfail': False}
    run.update(updates)
    return run


def diagnose(run, expected=None, label='before'):
    from reprolab.diagnostics import diagnose_run
    return diagnose_run(run, label=label, expected=expected)


def test_declared_module_failure_is_reproduced_instead_of_blame_on_environment():
    exc = {'type': 'ModuleNotFoundError', 'message': 'missing intentionally', 'name': 'teaching'}
    run = run_record('failed', exc, diagnostic_exceptions=[dict(phase='call', **exc)])
    original = copy.deepcopy(run)
    diagnosis = diagnose(run, {'type': 'ModuleNotFoundError', 'message_contains': 'intentionally'})
    assert diagnosis['code'] == 'expected_failure_reproduced'
    assert '已复现声明的故障' in diagnosis['summary']
    assert run == original


def test_failed_type_or_message_mismatch_and_before_pass_have_specific_explanations():
    expected = {'type': 'ValueError', 'message_contains': 'declared'}
    for exc in ({'type': 'TypeError', 'message': 'declared'}, {'type': 'ValueError', 'message': 'other'}):
        assert diagnose(run_record('failed', exc), expected)['code'] == 'expected_failure_mismatch'
    assert diagnose(run_record(), expected)['code'] == 'not_reproduced'


@pytest.mark.parametrize(('updates', 'code'), [
    ({'timed_out': True}, 'timeout'),
    ({'collected': 0, 'phase_events': [], 'exit_code': 5}, 'no_tests'),
    ({'collected': 2}, 'multiple_tests'),
    ({'collected': 0, 'phase_events': [], 'exit_code': 4}, 'invalid_target'),
    ({'collected': 0, 'phase_events': [], 'collection_errors': ['SyntaxError: sample']}, 'collection_error'),
    ({'has_xfail': True}, 'xfail'),
    ({'phase_events': [{'phase': 'setup', 'outcome': 'skipped', 'exception': None}]}, 'skipped'),
    ({'phase_events': [{'phase': 'setup', 'outcome': 'failed', 'exception': {'type': 'ValueError', 'message': 'fixture'}}]}, 'setup_error'),
    ({'phase_events': [{'phase': 'teardown', 'outcome': 'failed', 'exception': {'type': 'ValueError', 'message': 'cleanup'}}]}, 'teardown_error'),
    ({'phase_events': []}, 'incomplete_events'),
    ({'phase_events': [], 'exit_code': 3}, 'worker_error'),
])
def test_inconclusive_explanations_distinguish_execution_evidence(updates, code):
    diagnosis = diagnose(run_record('inconclusive', **updates))
    assert diagnosis['code'] == code
    assert diagnosis['summary'] and diagnosis['detail'] and diagnosis['next_steps']


def test_missing_module_uses_exception_name_without_claiming_a_pip_package_or_root_cause():
    run = run_record('inconclusive', collected=0, phase_events=[], collection_errors=['traceback'],
                     diagnostic_exceptions=[{'phase': 'collection', 'type': 'ModuleNotFoundError',
                                             'message': "No module named 'project_helpers'", 'name': 'project_helpers'}])
    result = diagnose(run)
    assert result['code'] == 'module_not_found'
    assert 'project_helpers' in result['detail']
    assert '模块' in result['summary']
    assert 'pip install' not in json.dumps(result, ensure_ascii=False)
    assert '根因' not in result['summary']


def test_arbitrary_log_text_never_triggers_missing_module_diagnosis():
    run = run_record('inconclusive', phase_events=[], log_excerpt="ModuleNotFoundError: No module named 'secret'")
    assert diagnose(run)['code'] == 'incomplete_events'


def test_legacy_report_uses_existing_verdict_conservatively():
    run = {'kind': 'inconclusive', 'reason': 'old report', 'log_excerpt': 'No module named xyz'}
    result = diagnose(run)
    assert result['code'] == 'legacy_inconclusive'
    assert '旧报告' in result['detail']
    assert 'xyz' not in json.dumps(result, ensure_ascii=False)


def test_result_explains_same_test_and_never_mutates_verdict():
    from reprolab.diagnostics import diagnose_result
    expected = {'type': 'ValueError', 'message_contains': 'declared'}
    result = {'status': 'verified', 'expected_failure': expected,
              'before': run_record('failed', {'type': 'ValueError', 'message': 'declared'}), 'after': run_record()}
    original = copy.deepcopy(result)
    diagnosis = diagnose_result(result)
    assert diagnosis['code'] == 'verified'
    assert '已复现声明的故障' in diagnosis['before']['summary']
    assert '同一测试通过' in diagnosis['after']['summary']
    assert result == original
    result['after']['test_id'] = 'other.py::test_one'
    result['status'] = 'inconclusive'
    mismatch = diagnose_result(result)
    assert mismatch['code'] == 'test_id_mismatch'
    assert '不一致' in mismatch['summary']
    assert '同一测试通过' not in mismatch['after']['summary']


@pytest.mark.parametrize('status', ['still_failing', 'not_reproduced', 'inconclusive', 'cancelled'])
def test_legacy_result_status_remains_available(status):
    from reprolab.diagnostics import diagnose_result
    result = {'status': status, 'before': {'kind': 'inconclusive'}, 'after': {'kind': 'inconclusive'}}
    diagnosis = diagnose_result(result)
    assert diagnosis['code'] == status
    assert diagnosis['summary'] and diagnosis['next_steps']


def test_worker_preserves_real_syntax_error_from_pytest_collection_wrapper(tmp_path):
    raw = worker_run(tmp_path, 'def test_one(:\n    pass\n')
    assert raw['collection_errors']
    assert raw['diagnostic_exceptions'][0]['phase'] == 'collection'
    assert raw['diagnostic_exceptions'][0]['type'] == 'SyntaxError'


def test_worker_does_not_guess_root_exception_from_application_chaining(tmp_path):
    raw = worker_run(tmp_path, 'try:\n    import reprolab_teaching_missing_xyz\nexcept ImportError as exc:\n    raise RuntimeError("application wrapper") from exc\n')
    assert raw['diagnostic_exceptions'][0]['type'] == 'RuntimeError'
    assert raw['diagnostic_exceptions'][0]['message'] == 'application wrapper'


def test_worker_rejects_many_parameters_before_any_test_event(tmp_path):
    raw = worker_run(tmp_path, 'import pytest\n@pytest.mark.parametrize("number", range(35))\ndef test_one(number):\n    raise ValueError(str(number))\n')
    assert raw['collected'] == 35
    assert raw['diagnostic_exceptions'] == []
    assert raw['events'] == []
    assert raw['selection_guard']['status'] == 'rejected'


def test_worker_records_module_name_in_project_test_mode(tmp_path):
    raw = worker_run(tmp_path, 'def test_one():\n    import reprolab_teaching_missing_xyz\n', target='test_teaching.py::test_one')
    assert raw['diagnostic_exceptions'][0]['name'] == 'reprolab_teaching_missing_xyz'
    assert raw['events'][1]['test_id'] == 'test_teaching.py::test_one'


@pytest.mark.parametrize('updates', [
    {'collected': None},
    {'collected': -1},
    {'phase_events': [None, {'phase': 'setup', 'outcome': 'passed'},
                      {'phase': 'call', 'outcome': 'failed'}, {'phase': 'teardown', 'outcome': 'passed'}]},
    {'phase_events': [{'phase': 'setup', 'outcome': 'passed'},
                      {'phase': 'call', 'outcome': 'passed'}, {'phase': 'teardown', 'outcome': 'passed'}]},
])
def test_inconsistent_modern_evidence_does_not_claim_declared_failure_reproduced(updates):
    failure = {'type': 'ValueError', 'message': 'declared'}
    result = diagnose(run_record('failed', failure, **updates), {'type': 'ValueError', 'message_contains': 'declared'})
    assert result['code'] in {'incomplete_events', 'inconsistent_evidence'}
    assert '已复现' not in result['summary']


def test_empty_declared_message_does_not_create_a_match():
    result = diagnose(run_record('failed', {'type': 'ValueError', 'message': 'anything'}),
                      {'type': 'ValueError', 'message_contains': ''})
    assert result['code'] == 'expected_failure_mismatch'


def test_legacy_matching_failure_explains_limited_stage_evidence():
    result = diagnose({'kind': 'failed', 'failure': {'type': 'ValueError', 'message': 'declared'}},
                      {'type': 'ValueError', 'message_contains': 'declared'})
    assert result['code'] == 'expected_failure_reproduced'
    assert '旧报告' in result['detail']
