"""Save a portable acceptance plan without running the reviewed project."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from reprolab.acceptance_plan import read_acceptance_plan
from reprolab.project_cli import main
from test_project_suggest import project, suggest


@pytest.fixture
def reviewed(project, capsys, tmp_path):
    repository, marker = project
    (repository / 'logic.py').write_text('VALUE = 2\n')
    tests = repository / 'test_logic.py'
    tests.write_text(tests.read_text().replace('def test_changed(): assert VALUE > 0',
                                              'def test_changed(): assert VALUE == 2'))
    result, _ = suggest(repository, tmp_path / 'review output', capsys)
    return result, repository, marker


def new_plan(result, output, *extra, selection=None):
    return ['plan-from-selection', result['directory'],
            '--selection-file', str(selection or result['selection_file']), '--output', str(output), *extra]


def test_save_plan_and_verify_scope_on_real_reports(reviewed, capsys, tmp_path, monkeypatch):
    result, _, marker = reviewed
    plan_path = tmp_path / "reviewer's $ plan.json"
    target = 'test_logic.py::test_changed'
    full = ['test_logic.py::test_existing', target]
    session_state = Path(result['directory'], 'project.json')
    before = session_state.read_bytes()
    def unexpected_spawn(*args, **kwargs):
        pytest.fail('plan creation must not launch a subprocess')

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'Popen', unexpected_spawn)
        assert main(new_plan(result, plan_path, '--require-improvement', target)) == 0
    created = json.loads(capsys.readouterr().out)
    plan = read_acceptance_plan(plan_path)
    assert plan.record()['schema_version'] == 3
    assert list(plan.approved_selection) == full and list(plan.targets) == [target]
    assert created['baseline']['source_sha256'] == result['source_sha256']['before']
    assert created['acceptance_plan_sha256'] == plan.fingerprint()
    assert created['collection_sha256'] == result['collection_sha256']
    assert not marker.exists() and before == session_state.read_bytes()
    if sys.platform != 'win32':
        run = shlex.split(created['run_command'])[2:]
    else:
        run = ['run', result['directory'], '--acceptance-plan', str(plan_path),
               '--verify-plan', '--output', str(tmp_path / 'reports')]
    assert main(run) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed['selected_tests'] == full
    assert completed['acceptance']['gate']['status'] == 'passed' and marker.exists()

    output = tmp_path / 'perimeter'
    assert main(['run', result['directory'], '--test', full[0], '--output', str(output)]) == 0
    perimeter = json.loads(capsys.readouterr().out)
    assert perimeter['counts'] == {'regression_passed': 1}
    archive = tmp_path / 'perimeter.zip'
    assert main(['pack', perimeter['report_dir'], '--output', str(archive)]) == 0
    capsys.readouterr()
    args = [str(archive) if arg == '<REPORT_OR_ZIP>' else arg for arg in created['offline_summary_arguments']]
    assert main([*args, '--format', 'json']) == 2
    current = json.loads(capsys.readouterr().out)
    assert current['required_improvements'][0]['status'] == 'blocked'
    assert current['approved_scope']['missing_tests'] == [target]


def test_create_scope_only_plan_and_reject_different_frozen_baseline(reviewed, capsys, tmp_path):
    result, repository, marker = reviewed
    path = tmp_path / 'scope plan.json'
    assert main(new_plan(result, path, '--gate', 'no-new-failures')) == 0
    capsys.readouterr()
    plan = read_acceptance_plan(path)
    assert plan.policy == 'no-new-failures' and not plan.targets and plan.approved_selection
    git = ['git', '-C', str(repository), '-c', 'user.name=Plan fixture',
           '-c', 'user.email=plan@example.invalid', '-c', 'commit.gpgsign=false',
           '-c', f'core.hooksPath={os.devnull}']
    for args in (('add', '.'), ('commit', '-qm', 'different baseline')):
        subprocess.run([*git, *args], check=True, capture_output=True)
    frozen, _ = suggest(repository, tmp_path / 'another session', capsys)
    assert main(['run', frozen['directory'], '--acceptance-plan', str(path),
                 '--verify-plan', '--output', str(tmp_path / 'reports')]) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'baseline_mismatch'
    assert not marker.exists()


@pytest.mark.parametrize(('selection', 'targets'), [
    ('', []),
    ('test_logic.py::test_existing\ntest_logic.py::test_existing\n', []),
    ('{"selected_tests":["test_other.py::test_other"],"selected_tests":["test_logic.py::test_existing"]}', []),
    ('test_other.py::test_other\n', ['test_logic.py::test_changed']),
    ('test_logic.py::test_changed\n', ['test_logic.py::test_changed'] * 2),
    ('test_logic.py::test_changed\n', ['test_changed']),
    ('test_logic.py::test_absent\n', ['test_logic.py::test_absent']),
])
def test_invalid_scopes_never_create_plan(reviewed, tmp_path, capsys, selection, targets):
    result, _, marker = reviewed
    source = tmp_path / 'reviewed.json'
    source.write_text(selection)
    output = tmp_path / 'plan.json'
    flags = [value for target in targets for value in ('--require-improvement', target)]
    assert main(new_plan(result, output, *flags, selection=source)) == 2
    assert not output.exists() and not marker.exists()


def test_reject_unproven_discovery_and_protect_existing_evidence(reviewed, tmp_path, capsys):
    result, repository, marker = reviewed
    existing = tmp_path / 'already.json'
    existing.write_text('do not replace')
    report = tmp_path / 'saved report'
    report.mkdir()
    (report / 'report.json').write_text(json.dumps({'kind': 'project_regression', 'schema_version': 1}))
    protected = [existing, repository / '.git/plan.json', Path(result['directory'], 'inputs', 'plan.json'),
                 report / 'plan.json']
    if sys.platform != 'win32':
        alias = tmp_path / 'report alias'
        alias.symlink_to(report, target_is_directory=True)
        protected.append(alias / 'plan.json')
    for target in protected:
        assert main(new_plan(result, target)) == 2
        capsys.readouterr()
        if target != existing:
            assert not target.exists()
    assert existing.read_text() == 'do not replace'

    record = Path(result['collection_record'])
    collection = json.loads(record.read_text())
    del collection['worker_record_complete']
    record.write_text(json.dumps(collection))
    session = Path(result['directory'], 'project.json')
    state = json.loads(session.read_text())
    state['collection_sha256'] = hashlib.sha256(record.read_bytes()).hexdigest()
    session.write_text(json.dumps(state))
    path = tmp_path / 'unproven.json'
    assert main(new_plan(result, path)) == 2
    assert '缺少完整性证据' in json.loads(capsys.readouterr().err)['error']['message']
    assert not path.exists() and not marker.exists()


def test_preserve_exact_parameter_identity_with_special_characters(project, capsys, tmp_path):
    repository, marker = project
    label = "reviewer's $ exact choice"
    (repository / 'test_quote.py').write_text(
        "import pytest\n" + f'@pytest.mark.parametrize("value", [1], ids=[{label!r}])\n'
        'def test_quote(value): assert value == 1\n')
    result, _ = suggest(repository, tmp_path / "reviewer's output", capsys)
    target = f'test_quote.py::test_quote[{label}]'
    assert target in Path(result['discovered_file']).read_text().splitlines()
    selection = tmp_path / "reviewer's $ tests.txt"
    output = tmp_path / "reviewer's $ plan.json"
    selection.write_text(target + '\n')
    assert main(new_plan(result, output, '--require-improvement', target, selection=selection)) == 0
    capsys.readouterr()
    plan = read_acceptance_plan(output)
    assert plan.approved_selection == plan.targets == (target,)
    assert not marker.exists()


def test_save_ci_plan_in_worktree_and_run_original_frozen_session(reviewed, capsys):
    result, repository, marker = reviewed
    config = repository / '.ci'
    config.mkdir()
    # Unrelated application manifests do not identify ReproLab evidence.
    (repository / 'project.json').write_text('{"name": "unrelated"}')
    (config / 'report.json').write_text('{"status": "draft"}')
    plan = config / "reviewer's $ plan.json"
    state_path = Path(result['directory'], 'project.json')
    original = state_path.read_bytes()
    target = 'test_logic.py::test_changed'
    assert main(new_plan(result, plan, '--require-improvement', target)) == 0
    created = json.loads(capsys.readouterr().out)
    assert plan.is_file() and state_path.read_bytes() == original and not marker.exists()
    expected_reports = Path(result['directory']).parents[1] / 'reports'
    if sys.platform != 'win32':
        run = shlex.split(created['run_command'])[2:]
        assert Path(run[run.index('--output') + 1]) == expected_reports
    else:
        run = ['run', result['directory'], '--acceptance-plan', str(plan),
               '--verify-plan', '--output', str(expected_reports)]
    # The worktree now contains the new file and differs further from the frozen inputs.
    (repository / 'logic.py').write_text('VALUE = -1\n')
    assert main(run) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['source_sha256'] == result['source_sha256']
    assert report['acceptance']['gate']['exit_code'] == 0
    assert Path(report['report_dir']).parent == expected_reports
    assert not (config / 'reports').exists()
