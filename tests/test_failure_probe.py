import json
from pathlib import Path
import pytest
from test_workflow import project_case
from reprolab.recipes import run_saved_case


def test_observed_failure_requires_confirmation_and_saves_the_probed_inputs(tmp_path):
    from reprolab.failure_probe import probe_failure, save_probe_case
    repo, before, after, target = project_case(tmp_path)
    result = probe_failure(repo, before, after, target, tmp_path / 'probes')
    assert result['status'] == 'failure_observed'
    assert result['suggestion'] == {'type': 'ValueError', 'message_contains': 'reported bug'}
    directory = Path(result['probe_dir'])
    assert (directory / 'before.events.json').is_file()
    assert not (directory / 'after.events.json').exists()
    assert not (directory / 'report.json').exists()
    case = tmp_path / 'cases/observed.json'
    with pytest.raises(ValueError, match='确认'):
        save_probe_case(directory, case)
    (repo / 'sample.py').write_text('def compute(): return -1\n')
    saved = save_probe_case(directory, case, confirmed=True)
    accepted = run_saved_case(saved['case'], tmp_path / 'reports')
    assert accepted['status'] == 'verified'
    assert accepted['failure_origin']['probe_id'] == result['probe_id']
    assert json.loads(case.read_text())['expected_failure'] == result['suggestion']


@pytest.mark.parametrize('test', ['def test_one(): pass\n',
    'import pytest\n@pytest.fixture\ndef broken(): raise ValueError("setup failed")\ndef test_one(broken): pass\n',
    'def test_one(): import reprolab_missing_teaching_package\n'])
def test_unusable_old_run_never_offers_an_automatic_failure_contract(tmp_path, test):
    from reprolab.failure_probe import probe_failure
    from reprolab.workspace import WORKTREE
    repo, before, _, _ = project_case(tmp_path)
    (repo / 'tests/test_probe.py').write_text(test)
    result = probe_failure(repo, before, WORKTREE, 'tests/test_probe.py::test_one', tmp_path / 'probes')
    assert result['status'] in ('before_passed', 'inconclusive')
    assert result['suggestion'] is None


def test_changed_probe_evidence_is_not_accepted(tmp_path):
    from reprolab.failure_probe import probe_failure, save_probe_case
    repo, before, after, target = project_case(tmp_path)
    result = probe_failure(repo, before, after, target, tmp_path / 'probes')
    (Path(result['probe_dir']) / 'before.events.json').write_text('{}')
    with pytest.raises(ValueError, match='证据|变化'):
        save_probe_case(result['probe_dir'], tmp_path / 'case.json', confirmed=True)


def test_recommendations_prioritize_changed_tests_without_importing_project(tmp_path):
    from reprolab.test_selection import recommend_tests
    from reprolab.workspace import WORKTREE
    repo, before, _, target = project_case(tmp_path)
    (repo / 'tests/test_added.py').write_text('raise RuntimeError("static reads must not import")\n')
    result = recommend_tests(repo, before, WORKTREE, ['tests/test_unrelated.py::test_one', 'tests/test_added.py::test_new', target])
    assert result['tests'][0] == 'tests/test_added.py::test_new'
    assert 'tests/test_added.py::test_new' in result['recommendations']
    assert 'tests/test_unrelated.py::test_one' not in result['recommendations']


def test_probe_accepts_a_file_that_collects_one_test_and_pins_its_nodeid(tmp_path):
    from reprolab.failure_probe import probe_failure, save_probe_case
    from reprolab.workspace import WORKTREE
    repo, before, _, _ = project_case(tmp_path)
    (repo / 'tests/test_single.py').write_text('from sample import compute\ndef test_one(): assert compute() == 42\n')
    result = probe_failure(repo, before, WORKTREE, 'tests/test_single.py', tmp_path / 'probes')
    assert result['status'] == 'failure_observed'
    saved = save_probe_case(result['probe_dir'], tmp_path / 'case.json', confirmed=True)
    assert json.loads(Path(saved['case']).read_text())['test_target'] == 'tests/test_single.py::test_one'


def test_confirmed_receipt_rejects_edits_to_target_or_failure_contract(tmp_path):
    from reprolab.failure_probe import probe_failure, save_probe_case
    repo, before, after, target = project_case(tmp_path)
    result = probe_failure(repo, before, after, target, tmp_path / 'probes')
    saved = save_probe_case(result['probe_dir'], tmp_path / 'case.json', confirmed=True)
    path = Path(saved['case'])
    original = json.loads(path.read_text())
    for field, value in [('test_target', 'tests/test_logic.py::test_other'),
                         ('expected_failure', {'type': 'RuntimeError', 'message_contains': 'other fault'})]:
        changed = dict(original)
        changed[field] = value
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match='确认|试运行'):
            run_saved_case(path, tmp_path / 'reports')


def test_corrupt_probe_gets_a_structured_cli_error(tmp_path):
    from reprolab.failure_probe import probe_failure
    from test_cli import cli
    repo, before, after, target = project_case(tmp_path)
    result = probe_failure(repo, before, after, target, tmp_path / 'probes')
    path = Path(result['probe_dir']) / 'probe.json'
    original = path.read_text()
    for section, key in [(None, 'source_sha256'), (None, 'case_template'), ('case_template', 'title'), ('case_template', 'source')]:
        damaged = json.loads(original)
        del (damaged if section is None else damaged[section])[key]
        path.write_text(json.dumps(damaged))
        response = cli('case-from-probe', path.parent, tmp_path / 'case.json', '--confirm-failure', '--json')
        assert response.returncode == 2
        assert json.loads(response.stdout)['status'] == 'inconclusive'
        assert not (tmp_path / 'case.json').exists()
