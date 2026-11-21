import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from reprolab.project_cli import main
from reprolab.project_wheels import check_wheels
from reprolab.runner import RunControl, source_fingerprint, source_mode_fingerprint
from reprolab.wheel_archive import open_wheel_evidence, pack_wheel_evidence


def legacy_wheel_evidence(root):
    checkout = root / 'downstream'
    checkout.mkdir(parents=True)
    (checkout / 'empty').mkdir()
    probe = checkout / 'test_receipt.py'
    probe.write_text('def test_ok(): assert True\n')
    probe.chmod(0o755)
    record = {
        'kind': 'reprolab_downstream_wheels', 'schema_version': 1, 'status': 'passed',
        'downstream_commit': 'c' * 40, 'module': 'receipt', 'selected_tests': ['test_receipt.py::test_ok'],
        'downstream': {'sha256': source_fingerprint(checkout)[1], 'mode_sha256': source_mode_fingerprint(checkout)},
        'runs': {}, 'wheel_artifacts': {},
    }
    for side in ('before', 'after'):
        folder = root / side
        folder.mkdir()
        wheel = folder / 'receipt_example-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('receipt.py', 'VALUE = 1')
        (folder / 'pytest.xml').write_text('<testsuites><testsuite><testcase classname="test_receipt" '
                                          'name="test_ok"/></testsuite></testsuites>')
        for log in ('install.log', 'pytest.log'):
            (folder / log).write_text('example')
        def digest(file):
            return hashlib.sha256(file.read_bytes()).hexdigest()
        record['wheel_artifacts'][side] = {'file': f'{side}/{wheel.name}', 'sha256': digest(wheel)}
        record['runs'][side] = {'install_status': 'completed', 'install_exit_code': 0, 'status': 'completed',
                                'exit_code': 0, 'tests': {'tests': 1, 'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 0},
                                'evidence_sha256': {n: digest(folder / n) for n in ('install.log', 'pytest.log', 'pytest.xml')}}
    (root / 'wheel-comparison.json').write_text(json.dumps(record))


def test_pack_wheels_round_trip_contains_only_bound_evidence(tmp_path, capsys):
    root = tmp_path / 'source'
    archive = tmp_path / 'evidence.zip'
    legacy_wheel_evidence(root)
    (root / 'private-notes.txt').write_text('not for export')
    (root / 'before' / 'other.whl').write_bytes(b'unrelated')
    assert main(['pack-wheels', str(root), '--output', str(archive)]) == 0
    assert json.loads(capsys.readouterr().out)['sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        assert not any(Path(n).name in ('private-notes.txt', 'other.whl') for n in bundle.namelist())
    root.rename(tmp_path / 'original-moved')
    with open_wheel_evidence(archive, control=RunControl()) as saved:
        assert check_wheels(saved, control=RunControl())['integrity'] == 'consistent'
        assert (saved / 'downstream/empty').is_dir()
    assert not saved.exists()


def test_wheel_export_preserves_destination_and_rejects_changed_evidence(tmp_path):
    root = tmp_path / 'source'
    legacy_wheel_evidence(root)
    existing = tmp_path / 'existing.zip'
    existing.write_bytes(b'keep')
    with pytest.raises(ValueError):
        pack_wheel_evidence(root, existing, control=RunControl())
    assert existing.read_bytes() == b'keep'
    (root / 'after/pytest.xml').write_text('<testsuites/>')
    target = tmp_path / 'new.zip'
    with pytest.raises(ValueError):
        pack_wheel_evidence(root, target, control=RunControl())
    assert not target.exists()


def test_wheel_markdown_puts_late_failure_ahead_of_unchanged_cases():
    from reprolab.wheel_results import render_wheel_markdown

    stable = [{'test_id': f'case_{i}', 'status': 'regression_passed', 'before': 'passed', 'after': 'passed'}
              for i in range(70)]
    regression = {'test_id': 'late_regression', 'status': 'regression_detected', 'before': 'passed', 'after': 'failed'}
    result = {'status': 'failed', 'runs': {'before': {}, 'after': {}},
              'comparison': {'gate': {'policy': 'regression', 'status': 'failed', 'exit_code': 1},
                             'counts': {'regression_passed': 70, 'regression_detected': 1},
                             'phase_basis': {'before': 'legacy_pytest_junit', 'after': 'legacy_pytest_junit'},
                             'required_improvements': [], 'issues': {'before': {}, 'after': {}},
                             'entries': [*stable, regression]}}
    text = render_wheel_markdown(result, exit_policy='Comparison gate', exit_code=1)
    assert 'late_regression' in text and 'case_0' not in text
    assert 'Showing 1 of 71 individual results' in text and '--format json' in text
    assert len(result['comparison']['entries']) == 71


def test_verify_wheels_preflights_pip_only_on_confirmed_run(tmp_path, capsys, monkeypatch):
    import subprocess

    from reprolab import project_wheels

    repo = tmp_path / 'repo'
    repo.mkdir()
    git = ['git', '-C', str(repo), '-c', 'user.name=Reviewer',
           '-c', 'user.email=review@example.invalid', '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'init', '-q'], check=True)
    subprocess.run([*git, 'commit', '--allow-empty', '-qm', 'downstream'], check=True)
    artifact = tmp_path / 'example-1.0-py3-none-any.whl'
    artifact.touch()
    output = tmp_path / 'output-parent' / 'evidence'
    python = str(tmp_path / 'unseeded' / 'python')
    calls = []
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[0] != python:
            return real_run(command, *args, **kwargs)
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, '', f'{python}: No module named pip')

    monkeypatch.setattr(project_wheels, 'choose_runtime', lambda path: {'executable': str(path)})
    monkeypatch.setattr(subprocess, 'run', run)
    argv = ['verify-wheels', str(repo), '--before-wheel', str(artifact), '--after-wheel', str(artifact),
            '--test', 'test_receipt.py::test_ok', '--module', 'example', '--python', python,
            '--output', str(output)]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ready'
    assert main([*argv, '--format', 'markdown']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ready'
    assert calls == []
    assert main([*argv, '--confirm-run']) == 2
    message = json.loads(capsys.readouterr().err)['error']['message']
    assert calls == [[python, '-I', '-m', 'pip', '--version']]
    assert python in message and '缺少 pip' in message
    assert not output.parent.exists()


@pytest.mark.parametrize('options', [
    ['--gate', 'legacy'], ['--gate', 'regression'], ['--gate', 'no-new-failures'],
    ['--require-improvement', 'test_receipt.py::test_ok'],
    ['--gate', 'regression', '--require-improvement', 'test_receipt.py::test_ok'],
])
def test_verify_wheels_requires_confirmation_for_explicit_gate(tmp_path, capsys, monkeypatch, options):
    from reprolab import project_wheels

    def unexpected_run(*args, **kwargs):
        pytest.fail('A gated preview must stop before calling run_wheels')

    monkeypatch.setattr(project_wheels, 'run_wheels', unexpected_run)
    output = tmp_path / 'uncreated' / 'evidence'
    argv = ['verify-wheels', str(tmp_path), '--before-wheel', 'before.whl', '--after-wheel', 'after.whl',
            '--test', 'test_receipt.py::test_ok', '--module', 'example', '--output', str(output)]
    assert main([*argv, *options]) == 2
    error = json.loads(capsys.readouterr().err)['error']
    assert error['type'] == 'argument_error' and '--confirm-run' in error['message']
    assert not output.parent.exists()
