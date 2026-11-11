"""AI-plan probes observe one frozen old-version test, never accept a repair."""
import json
from pathlib import Path
import threading
import time

import pytest

from reprolab.repair_suite import prepare_suite, generate_suite
from reprolab.runner import RunControl, RunCancelled
from reprolab.suite_review import editable_plan, revise_suite
from test_cli import cli
from test_git_source import prepare_repo, git
from test_repair_suite import _plan_response
from test_suite_review import planned_suite, tree_bytes


def api():
    from reprolab import suite_probe
    return suite_probe


def project_suite(tmp_path, code, target='tests/test_behavior.py::test_one', python_executable=None):
    repo, before, _, _ = prepare_repo(tmp_path)
    (repo / 'tests').mkdir()
    (repo / 'tests/test_behavior.py').write_text(code)
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'project test')
    state = prepare_suite(repo, before, 'HEAD', '修复 compute', tmp_path / 'suites', python_executable=python_executable)
    plan = _plan_response()
    plan['checks'] = plan['checks'][:1]
    plan['checks'][0].update(test_code=None, test_target=target)
    response = tmp_path / 'response.json'
    response.write_text(json.dumps(plan))
    return generate_suite(state['directory'], response_file=response)


def test_probe_runs_only_old_version_and_preserves_the_plan(planned_suite, tmp_path):
    tool = api()
    original = Path(planned_suite['directory'])
    marker = tmp_path / 'executions.txt'
    plan = editable_plan(original)
    plan['checks'][0]['test_code'] = (
        'from pathlib import Path\nfrom sample import compute\n'
        f'def test_fix():\n    with Path({str(marker)!r}).open("a") as f: f.write("run\\n")\n'
        '    assert compute() == 42\n')
    state = revise_suite(original, plan, tmp_path / 'reviewed')
    folder = Path(state['directory'])
    saved = tree_bytes(folder)
    observed = tool.probe_suite_check(folder, 'repair', tmp_path / 'probes')
    assert observed['status'] == 'failure_observed'
    assert observed['before']['kind'] == 'failed'
    assert observed['suggestion'] == {'type': 'ValueError', 'message_contains': 'reported bug'}
    assert marker.read_text() == 'run\n'
    assert tree_bytes(folder) == saved
    probe = Path(observed['probe_dir'])
    assert (probe / 'before.log').is_file() and not (probe / 'after.log').exists()
    assert observed['plan_sha256'] == state['plan_sha256']
    suggestion = tool.checked_probe_suggestion(folder, probe)
    assert suggestion['check_id'] == 'repair'
    assert suggestion['expected_failure'] == observed['suggestion']
    moved = tmp_path / 'moved-probe'
    probe.rename(moved)
    assert tool.read_suite_probe(folder, moved)['probe_dir'] == str(moved)
    assert marker.read_text() == 'run\n', '查看和校验记录不能再次执行测试'


def test_project_probe_uses_the_same_selected_parameter_instance(planned_suite, tmp_path):
    tool = api()
    plan = editable_plan(planned_suite['directory'])
    plan['checks'][0].update(test_code=None, test_target='tests/test_project.py::test_compute[42]')
    state = revise_suite(planned_suite['directory'], plan, tmp_path / 'reviewed')
    observed = tool.probe_suite_check(state['directory'], 'repair', tmp_path / 'probes')
    assert observed['status'] == 'failure_observed'
    assert observed['before']['test_id'] == 'tests/test_project.py::test_compute[42]'
    assert observed['test_target'] == 'tests/test_project.py::test_compute[42]'


@pytest.mark.parametrize('code,timeout,status', [
    ('import pytest\nfrom sample import compute\ndef test_one():\n    with pytest.raises(ValueError): compute()\n', 5, 'before_passed'),
    ('import missing_probe_dependency\nfrom sample import compute\ndef test_one(): compute()\n', 5, 'inconclusive'),
    ('from sample import compute\ndef test_one():\n    import missing_probe_dependency\n    compute()\n', 5, 'inconclusive'),
    ('import pytest\nfrom sample import compute\n@pytest.fixture\ndef broken(): raise RuntimeError("fixture setup")\ndef test_one(broken): compute()\n', 5, 'inconclusive'),
    ('import pytest\nfrom sample import compute\n@pytest.fixture\ndef cleanup():\n    yield\n    raise RuntimeError("fixture cleanup")\ndef test_one(cleanup): compute()\n', 5, 'inconclusive'),
    ('import pytest\ndef test_one(): pytest.skip("not checked")\n', 5, 'inconclusive'),
    ('import pytest\nfrom sample import compute\n@pytest.mark.xfail\ndef test_one(): compute()\n', 5, 'inconclusive'),
    ('def test_one(): raise ValueError("")\n', 5, 'inconclusive'),
    ('import time\nfrom sample import compute\ndef test_one():\n    time.sleep(5)\n    compute()\n', .1, 'inconclusive'),
])
def test_non_target_outcomes_never_offer_failure_conditions(tmp_path, code, timeout, status):
    tool = api()
    state = project_suite(tmp_path, code)
    observed = tool.probe_suite_check(state['directory'], 'repair', tmp_path / 'probes', timeout=timeout)
    assert observed['status'] == status
    assert observed['suggestion'] is None
    with pytest.raises(ValueError, match='采用|故障|建议'):
        tool.checked_probe_suggestion(state['directory'], observed['probe_dir'])


@pytest.mark.parametrize('changed', ['log', 'events', 'test', 'source', 'mode', 'binding'])
def test_changed_probe_material_cannot_prefill_a_condition(planned_suite, tmp_path, changed):
    tool = api()
    state = tool.probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    probe = Path(state['probe_dir'])
    if changed == 'mode':
        path = probe / 'inputs/before/sample.py'
        path.chmod(path.stat().st_mode ^ 0o100)
    elif changed == 'binding':
        path = probe / 'probe.json'
        record = json.loads(path.read_text())
        record['case_sha256'] = '0' * 64
        path.write_text(json.dumps(record))
    else:
        names = {'log': 'before.log', 'events': 'before.events.json', 'test': 'inputs/regression.py',
                 'source': 'inputs/before/sample.py'}
        (probe / names[changed]).write_text('changed')
    with pytest.raises(ValueError, match='变化|一致|匹配|无效'):
        tool.checked_probe_suggestion(planned_suite['directory'], probe)


def test_changed_parent_plan_cannot_reuse_an_old_observation(planned_suite, tmp_path):
    tool = api()
    probe = tool.probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    plan = editable_plan(planned_suite['directory'])
    plan['title'] = 'another review'
    other = revise_suite(planned_suite['directory'], plan, tmp_path / 'reviewed')
    with pytest.raises(ValueError, match='匹配|一致|来源'):
        tool.checked_probe_suggestion(other['directory'], probe['probe_dir'])


def test_prefill_rechecks_evidence_after_python_metadata_probe(planned_suite, tmp_path, monkeypatch):
    tool = api()
    observed = tool.probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    choose = tool.choose_runtime
    def changed(*args, **kwargs):
        runtime = choose(*args, **kwargs)
        Path(observed['probe_dir'], 'before.log').write_text('changed while inspecting Python')
        return runtime
    monkeypatch.setattr(tool, 'choose_runtime', changed)
    with pytest.raises(ValueError, match='变化|一致'):
        tool.checked_probe_suggestion(planned_suite['directory'], observed['probe_dir'])


def test_cancelling_a_running_probe_leaves_no_adoptable_suggestion(tmp_path):
    tool = api()
    marker = tmp_path / 'entered.txt'
    state = project_suite(tmp_path, 'from pathlib import Path\nimport time\nfrom sample import compute\n'
                          f'def test_one():\n    Path({str(marker)!r}).write_text("entered")\n'
                          '    time.sleep(20)\n    compute()\n')
    control = RunControl()
    def cancel():
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        control.cancel()
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        with pytest.raises(RunCancelled):
            tool.probe_suite_check(state['directory'], 'repair', tmp_path / 'probes', timeout=10, control=control)
    finally:
        thread.join(6)
    record = json.loads(next((tmp_path / 'probes').glob('*/probe.json')).read_text())
    assert record['status'] == 'cancelled' and record['suggestion'] is None
    assert marker.exists()


def test_invalid_selection_and_timeout_do_not_create_probe_material(planned_suite, tmp_path):
    tool = api()
    for identity, timeout in [('unknown', 15), ('repair', True), ('repair', 0), ('repair', 301), ('repair', float('nan'))]:
        with pytest.raises(ValueError):
            tool.probe_suite_check(planned_suite['directory'], identity, tmp_path / 'probes', timeout=timeout)
    assert not (tmp_path / 'probes').exists()
    with pytest.raises(ValueError, match='之外|内部'):
        tool.probe_suite_check(planned_suite['directory'], 'repair', Path(planned_suite['directory']) / 'probes')


def test_cli_observes_then_reopens_the_same_saved_record(planned_suite, tmp_path):
    observed = cli('suite', 'probe', planned_suite['directory'], '--check', 'repair', '--output', tmp_path / 'probes')
    assert observed.returncode == 0, observed.stderr
    record = json.loads(observed.stdout)
    assert record['status'] == 'failure_observed'
    shown = cli('suite', 'probe-show', planned_suite['directory'], '--probe', record['probe_dir'])
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)['before']['events_sha256'] == record['before']['events_sha256']


@pytest.mark.parametrize('code,status', [
    ('import missing_probe_dependency\ndef test_one(): pass\n', 'inconclusive'),
    ('import pytest\nfrom sample import compute\ndef test_one():\n    with pytest.raises(ValueError): compute()\n', 'before_passed'),
])
def test_cli_blocked_probe_has_a_nonzero_exit_code(tmp_path, code, status):
    state = project_suite(tmp_path, code)
    observed = cli('suite', 'probe', state['directory'], '--check', 'repair', '--output', tmp_path / 'probes')
    assert observed.returncode == 2, observed.stdout + observed.stderr
    assert json.loads(observed.stdout)['status'] == status


def test_multiple_collected_tests_do_not_offer_failure_conditions(tmp_path):
    tool = api()
    state = project_suite(tmp_path, 'from sample import compute\ndef test_one(): compute()\ndef test_two(): compute()\n',
                          target='tests/test_behavior.py')
    observed = tool.probe_suite_check(state['directory'], 'repair', tmp_path / 'probes')
    assert observed['status'] == 'inconclusive' and observed['suggestion'] is None
    assert observed['before']['selection_guard']['status'] == 'rejected'


def test_suggestion_requires_the_python_used_for_the_observation(planned_suite, tmp_path):
    from test_workflow import make_environment
    tool = api()
    python, _ = make_environment(tmp_path, True)
    observed = tool.probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes', python_executable=python)
    with pytest.raises(ValueError, match='Python|环境'):
        tool.checked_probe_suggestion(planned_suite['directory'], observed['probe_dir'])
    assert tool.checked_probe_suggestion(planned_suite['directory'], observed['probe_dir'], python_executable=python)['expected_failure']['type'] == 'ValueError'


def test_probe_of_legacy_plan_still_binds_permissions_observed_now(planned_suite, tmp_path):
    import hashlib
    tool = api()
    folder = Path(planned_suite['directory'])
    state = json.loads((folder / 'suite.json').read_text())
    state.pop('source_mode_sha256')
    plan = json.loads((folder / 'plan.json').read_text())
    for check in plan['checks']:
        path = folder / check['case']
        case = json.loads(path.read_text())
        case['frozen_inputs'].pop('source_mode_sha256')
        path.write_text(json.dumps(case))
        check['case_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (folder / 'plan.json').write_text(json.dumps(plan))
    state['plan_sha256'] = hashlib.sha256((folder / 'plan.json').read_bytes()).hexdigest()
    (folder / 'suite.json').write_text(json.dumps(state))
    observed = tool.probe_suite_check(folder, 'repair', tmp_path / 'probes')
    assert observed['input_integrity_notice']
    path = folder / 'inputs/before/sample.py'
    path.chmod(path.stat().st_mode ^ 0o100)
    with pytest.raises(ValueError, match='权限|匹配|变化'):
        tool.checked_probe_suggestion(folder, observed['probe_dir'])


def test_missing_execution_record_is_not_presented_as_an_observed_failure(planned_suite, tmp_path):
    tool = api()
    observed = tool.probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    path = Path(observed['probe_dir']) / 'probe.json'
    data = json.loads(path.read_text())
    data['before'] = None
    path.write_text(json.dumps(data))
    shown = tool.read_suite_probe(planned_suite['directory'], observed['probe_dir'])
    assert shown['status'] == 'inconclusive' and shown['suggestion'] is None


@pytest.mark.parametrize('interruption', ['cancel', 'save_error'])
def test_completed_worker_without_committed_probe_never_becomes_adoptable(planned_suite, tmp_path, monkeypatch, interruption):
    tool = api()
    control = RunControl()
    if interruption == 'cancel':
        run = tool.run_snapshot
        def cancelled(*args, **kwargs):
            result = run(*args, **kwargs)
            control.cancel()
            return result
        monkeypatch.setattr(tool, 'run_snapshot', cancelled)
        error, wanted = RunCancelled, 'cancelled'
    else:
        save = tool.save_json
        def failed(path, data):
            if data.get('status') == 'failure_observed':
                raise OSError('teaching final record failure')
            return save(path, data)
        monkeypatch.setattr(tool, 'save_json', failed)
        error, wanted = ValueError, 'inconclusive'
    with pytest.raises(error):
        tool.probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes', control=control)
    folder = next((tmp_path / 'probes').iterdir())
    raw = json.loads((folder / 'probe.json').read_text())
    assert raw['status'] == wanted and raw['before']['kind'] == 'failed'
    reopened = tool.read_suite_probe(planned_suite['directory'], folder)
    assert reopened['status'] == wanted and reopened['suggestion'] is None
    with pytest.raises(ValueError, match='采用|故障|建议'):
        tool.checked_probe_suggestion(planned_suite['directory'], folder)


def test_reopen_uses_raw_diagnostics_and_log_instead_of_cached_summary(tmp_path):
    tool = api()
    state = project_suite(tmp_path, 'from sample import compute\ndef test_one():\n    import missing_probe_dependency\n    compute()\n')
    observed = tool.probe_suite_check(state['directory'], 'repair', tmp_path / 'probes')
    assert observed['status'] == 'inconclusive'
    folder = Path(observed['probe_dir'])
    path = folder / 'probe.json'
    raw = json.loads(path.read_text())
    raw['before']['diagnostic_exceptions'] = []
    raw['before']['log_excerpt'] = 'fabricated display content'
    path.write_text(json.dumps(raw))
    reopened = tool.read_suite_probe(state['directory'], folder)
    assert reopened['status'] == 'inconclusive'
    assert reopened['diagnosis']['code'] == 'module_not_found'
    assert 'fabricated display content' not in reopened['before']['log_excerpt']
    assert 'missing_probe_dependency' in reopened['before']['log_excerpt']
    with pytest.raises(ValueError, match='采用|故障|建议'):
        tool.checked_probe_suggestion(state['directory'], folder)


def test_record_replacement_during_python_check_cannot_mix_check_identities(planned_suite, tmp_path, monkeypatch):
    tool = api()
    plan = editable_plan(planned_suite['directory'])
    other = dict(plan['checks'][0])
    other.update(id='other', role='regression', expected_failure=None,
                 test_code='from sample import compute\ndef test_other():\n    try: compute()\n    except ValueError: raise RuntimeError("other check issue")\n')
    plan['checks'].append(other)
    state = revise_suite(planned_suite['directory'], plan, tmp_path / 'reviewed')
    first = tool.probe_suite_check(state['directory'], 'repair', tmp_path / 'probes')
    second = tool.probe_suite_check(state['directory'], 'other', tmp_path / 'probes')
    choose = tool.choose_runtime
    def replaced(*args, **kwargs):
        runtime = choose(*args, **kwargs)
        Path(first['probe_dir']).rename(tmp_path / 'original-probe')
        Path(second['probe_dir']).rename(first['probe_dir'])
        return runtime
    monkeypatch.setattr(tool, 'choose_runtime', replaced)
    with pytest.raises(ValueError, match='匹配|变化|一致'):
        tool.checked_probe_suggestion(state['directory'], first['probe_dir'])


def test_prefill_rechecks_evidence_after_rebuilding_diagnosis(planned_suite, tmp_path, monkeypatch):
    tool = api()
    observed = tool.probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    diagnose = tool._observation
    def changed(before):
        result = diagnose(before)
        Path(observed['probe_dir'], 'before.log').write_text('changed during diagnosis')
        return result
    monkeypatch.setattr(tool, '_observation', changed)
    with pytest.raises(ValueError, match='变化|一致'):
        tool.checked_probe_suggestion(planned_suite['directory'], observed['probe_dir'])
