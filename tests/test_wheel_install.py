"""Exercise release wheel workflows using real pip and local fixtures."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement

from reprolab.project_cli import main


def write_wheel(directory, distribution, version, requirements=(), *, module=None, source='VALUE = 1\n'):
    directory.mkdir(parents=True, exist_ok=True)
    base = f'{distribution.replace("-", "_")}-{version}'
    path = directory / (base + '-py3-none-any.whl')
    with ZipFile(path, 'w') as archive:
        archive.writestr((module or distribution.replace('-', '_')) + '.py', source)
        info = base + '.dist-info/'
        archive.writestr(info + 'WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr(info + 'METADATA', f'Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n'
                         + ''.join(f'Requires-Dist: {item}\n' for item in requirements))
        archive.writestr(info + 'RECORD', '')
    return path


def _verify(wheel, house, output, *flags):
    return main(['verify-wheel-install', str(wheel), '--wheelhouse', str(house), '--output', str(output), *flags])


def _report(output, capsys):
    result = json.loads((output / 'wheel-install-check.json').read_text())
    assert json.loads(capsys.readouterr().out) == result
    return result


def test_local_dependency_must_be_in_wheelhouse(tmp_path, capsys, monkeypatch):
    # ReproLab's running Python already has this distribution.
    version = importlib.metadata.version('packaging')
    candidate = write_wheel(tmp_path / 'candidate', 'release-fixture', '1.0', [f'packaging=={version}'])
    populated = tmp_path / 'populated with space'
    dependency = write_wheel(populated, 'packaging', version)
    empty = tmp_path / 'empty'
    empty.mkdir()
    configuration = tmp_path / 'pip.conf'
    configuration.write_text(f'[global]\nfind-links = {populated.as_uri()}\n')
    monkeypatch.setenv('PIP_CONFIG_FILE', str(configuration))
    monkeypatch.setenv('PIP_FIND_LINKS', populated.as_uri())
    missing = tmp_path / 'missing'
    assert _verify(candidate, empty, missing) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ready'
    assert not missing.exists()
    assert _verify(candidate, empty, missing, '--confirm-run') == 1
    failed = _report(missing, capsys)
    assert failed['reason'] == 'install_failed'
    assert failed['environment']['initial_distributions'] == []
    assert 'pip-check' not in failed['steps']
    assert 'No matching distribution found' in (missing / 'install.log').read_text()

    supplied = tmp_path / 'supplied'
    assert _verify(candidate, populated, supplied, '--confirm-run') == 0
    passed = _report(supplied, capsys)
    assert passed['wheel'] == failed['wheel'] == {
        'filename': candidate.name, 'sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
    }
    assert passed['steps']['install'] == passed['steps']['pip-check'] == {'status': 'completed', 'exit_code': 0}
    assert passed['environment']['isolated_venv'] is True
    assert passed['environment']['initial_distributions'] == []
    assert passed['wheelhouse'] == [{'filename': dependency.name, 'sha256': hashlib.sha256(dependency.read_bytes()).hexdigest()}]


@pytest.mark.parametrize('direct_in_candidate', [True, False])
def test_local_wheel_policy_rejects_direct_file_url(tmp_path, capsys, direct_in_candidate):
    outside = write_wheel(tmp_path / 'outside', 'outside-fixture', '1.0')
    house = tmp_path / 'house'
    house.mkdir()
    requirement = f'outside-fixture @ {outside.as_uri()}'
    if not direct_in_candidate:
        write_wheel(house, 'dependency-fixture', '1.0', [requirement])
        requirement = 'dependency-fixture==1.0'
    wheel = write_wheel(tmp_path, 'release-fixture', '1.0', [requirement])
    output = tmp_path / 'output'
    assert _verify(wheel, house, output, '--confirm-run') == 2
    report = _report(output, capsys)
    assert report['reason'] == ('candidate_metadata_rejected' if direct_in_candidate else 'wheelhouse_input_rejected')
    assert report['steps'] == {}
    assert str(outside) not in (output / 'input.log').read_text(encoding='utf-8')


def test_unavailable_python_blocks_instead_of_failing_candidate(tmp_path, capsys):
    house = tmp_path / 'house'
    house.mkdir()
    wheel = write_wheel(tmp_path, 'release-fixture', '1.0')
    output = tmp_path / 'blocked'
    assert _verify(wheel, house, output, '--confirm-run', '--python', str(tmp_path / 'absent-python')) == 2
    report = _report(output, capsys)
    assert report['reason'] == 'tool-venv_unavailable'
    assert report['steps']['tool-venv'] == {'status': 'unavailable', 'exit_code': None}


def test_fingerprint_wheelhouse_is_portable_and_covers_names_and_bytes(tmp_path, capsys):
    house = tmp_path / 'trusted-house'
    house.mkdir()
    assert main(['fingerprint-wheelhouse', str(house)]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty['wheelhouse'] == []
    assert empty['wheelhouse_sha256'] == hashlib.sha256(b'[]').hexdigest()
    zulu = write_wheel(house, 'zulu', '2.0')
    alpha = write_wheel(house, 'alpha', '1.0')
    (house / 'notes.txt').write_text('not a wheel')
    assert main(['fingerprint-wheelhouse', str(house)]) == 0
    trusted = json.loads(capsys.readouterr().out)
    assert [item['filename'] for item in trusted['wheelhouse']] == [alpha.name, zulu.name]
    mirror = tmp_path / 'different-path'
    mirror.mkdir()
    for path in (alpha, zulu):
        (mirror / path.name).write_bytes(path.read_bytes())
    assert main(['fingerprint-wheelhouse', str(mirror)]) == 0
    assert json.loads(capsys.readouterr().out) == trusted
    alternate_tag = mirror / zulu.name.replace('py3-none-any', 'py311-none-any')
    (mirror / zulu.name).rename(alternate_tag)
    assert main(['fingerprint-wheelhouse', str(mirror)]) == 0
    assert json.loads(capsys.readouterr().out)['wheelhouse_sha256'] != trusted['wheelhouse_sha256']
    alternate_tag.rename(mirror / zulu.name)
    with ZipFile(mirror / zulu.name, 'a') as archive:
        archive.comment = b'different bytes but the same wheel filename'
    assert main(['fingerprint-wheelhouse', str(mirror)]) == 0
    assert json.loads(capsys.readouterr().out)['wheelhouse_sha256'] != trusted['wheelhouse_sha256']


def test_fingerprint_wheelhouse_rejects_invalid_inputs(tmp_path, capsys):
    house = tmp_path / 'house'
    house.mkdir()
    assert main(['fingerprint-wheelhouse', str(house / 'missing')]) == 2
    direct = write_wheel(house, 'fixture', '1.0', ['other @ file:///outside.whl'])
    assert main(['fingerprint-wheelhouse', str(house)]) == 2
    assert '直接 URL' in capsys.readouterr().err
    direct.write_bytes(b'not a wheel')
    assert main(['fingerprint-wheelhouse', str(house)]) == 2
    assert 'METADATA' in capsys.readouterr().err


def _repack_tool_wheel(directory, name):
    """Save the test runner's installed pure Python distributions for offline CI."""
    distribution = importlib.metadata.distribution(name)
    info = next(item.parent for item in distribution.files if item.name == 'WHEEL' and len(item.parts) == 2)
    with ZipFile(directory / (info.name.removesuffix('.dist-info') + '-py3-none-any.whl'), 'w') as archive:
        for entry in distribution.files:
            if '..' in entry.parts or entry.suffix == '.pyc' or entry.name == 'RECORD':
                continue
            source = distribution.locate_file(entry)
            if source.is_file():
                archive.write(source, entry.as_posix())
        archive.writestr(info.as_posix() + '/RECORD', '')
    return [Requirement(raw) for raw in distribution.requires or ()]


@pytest.fixture
def isolated_functional_scope(tmp_path):
    house = tmp_path / 'wheelhouse'
    house.mkdir()
    pending = ['pytest', 'tomlkit']
    copied = set()
    while pending:
        name = pending.pop()
        if name not in copied:
            copied.add(name)
            pending.extend(dep.name for dep in _repack_tool_wheel(house, name)
                           if dep.marker is None or dep.marker.evaluate({'extra': ''}))
    repo = tmp_path / 'downstream'
    repo.mkdir()
    (repo / 'test_release.py').write_text(
        'from release_fixture import public_api\n\ndef test_api():\n    assert public_api() == 4\n'
    )
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
           '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={os.devnull}']
    for args in (('init', '-q'), ('add', '.'), ('commit', '-qm', 'downstream')):
        subprocess.run([*git, *args], check=True, capture_output=True)
    baseline = write_wheel(tmp_path / 'baseline', 'release-fixture', '1.0', source='def public_api(): return 4\n')
    return repo, house, baseline


def _functional(repo, baseline, candidate, output, house=None):
    args = ['verify-wheels', str(repo), '--before-wheel', str(baseline), '--after-wheel', str(candidate),
            '--module', 'release_fixture', '--test', 'test_release.py::test_api', '--confirm-run',
            '--gate', 'regression', '--output', str(output)]
    if house is not None:
        args.extend(('--test-wheelhouse', str(house)))
    return main(args)


def test_isolated_functional_wheels_resolve_only_requested_dependencies(isolated_functional_scope, tmp_path, capsys):
    repo, house, baseline = isolated_functional_scope
    source = 'def public_api():\n    import tomlkit\n    return tomlkit.parse("result = 4")["result"]\n'
    missing = write_wheel(tmp_path / 'missing', 'release-fixture', '2.0', source=source)
    declared = write_wheel(tmp_path / 'declared', 'release-fixture', '2.0', ['tomlkit'], source=source)
    assert _functional(repo, baseline, missing, tmp_path / 'ambient') == 0
    capsys.readouterr()
    failing = tmp_path / 'failing'
    assert _functional(repo, baseline, missing, failing, house) == 1
    assert json.loads(capsys.readouterr().out)['comparison']['counts'] == {'regression_detected': 1}
    assert 'ModuleNotFoundError' in (failing / 'after/pytest.log').read_text()
    passing = tmp_path / 'passing'
    assert _functional(repo, baseline, declared, passing, house) == 0
    assert json.loads(capsys.readouterr().out)['comparison']['counts'] == {'regression_passed': 1}

    record_path = passing / 'wheel-comparison.json'
    record = json.loads(record_path.read_bytes())
    assert main(['fingerprint-wheelhouse', str(house)]) == 0
    lock = json.loads(capsys.readouterr().out)
    assert lock['wheelhouse'] == record['test_wheelhouse']
    plan = {'kind': 'reprolab_wheel_review_plan', 'schema_version': 1, 'gate': 'regression',
            'required_test_wheelhouse_sha256': lock['wheelhouse_sha256'],
            **{key: record[key] for key in ('downstream_commit', 'module', 'selected_tests')},
            'wheel_artifacts': {side: {'filename': Path(item['file']).name, 'sha256': item['sha256']}
                                for side, item in record['wheel_artifacts'].items()}}
    plan_file = tmp_path / 'plan.json'
    plan_file.write_text(json.dumps(plan))
    options = ['--review-plan', str(plan_file), '--verify-git-repository', str(repo)]
    archive = tmp_path / 'evidence.zip'
    assert main(['pack-wheels', str(passing), '--output', str(archive)]) == 0
    capsys.readouterr()
    assert main(['check-wheels', str(archive), *options]) == 0
    proof = json.loads(capsys.readouterr().out)['review_plan_acceptance']['test_installation']
    assert proof['status'] == 'matched'
    for side in ('before', 'after'):
        assert proof['observed'][side] == {'mode': 'isolated_venv', 'wheelhouse_sha256': lock['wheelhouse_sha256']}

    bad_record = json.loads((failing / 'wheel-comparison.json').read_bytes())
    plan['wheel_artifacts']['after']['sha256'] = bad_record['wheel_artifacts']['after']['sha256']
    plan_file.write_text(json.dumps(plan))
    assert main(['check-wheels', str(failing), *options]) == 1
    capsys.readouterr()
    assert main(['check-wheels', str(tmp_path / 'ambient'), *options]) == 2
    assert json.loads(capsys.readouterr().out)['review_plan_acceptance']['reason'] == 'test_installation_unavailable'

    for side in ('before', 'after'):
        modified = tmp_path / ('changed-' + side)
        shutil.copytree(failing, modified)
        phase_path = modified / side / 'pytest-phases.json'
        phase = json.loads(phase_path.read_bytes())
        phase.pop('test_installation')
        phase_path.write_text(json.dumps(phase))
        manifest = modified / 'wheel-comparison.json'
        record = json.loads(manifest.read_bytes())
        record['runs'][side]['evidence_sha256'][phase_path.name] = hashlib.sha256(phase_path.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(record))
        assert main(['check-wheels', str(modified), *options]) == 2
        result = json.loads(capsys.readouterr().out)['review_plan_acceptance']
        assert result['mismatched_fields'] == [f'test_installation.{side}']
    bad_record['test_wheelhouse'][0]['sha256'] = '0' * 64
    (failing / 'wheel-comparison.json').write_text(json.dumps(bad_record))
    assert main(['check-wheels', str(failing), *options]) == 2
    result = json.loads(capsys.readouterr().out)['review_plan_acceptance']
    assert result['reason'] == 'test_wheelhouse_mismatch'
    assert result['mismatched_fields'] == [
        'test_wheelhouse', 'test_installation.before.wheelhouse_sha256', 'test_installation.after.wheelhouse_sha256',
    ]


def test_isolated_functional_requires_tooling_and_module_ownership(isolated_functional_scope, tmp_path, capsys):
    repo, house, baseline = isolated_functional_scope
    empty = tmp_path / 'empty'
    empty.mkdir()
    missing_tool = tmp_path / 'missing-tool'
    assert _functional(repo, baseline, baseline, missing_tool, empty) == 2
    assert json.loads(capsys.readouterr().out)['runs']['after']['install_reason'] == 'install_failed'
    assert 'No matching distribution found for pytest' in (missing_tool / 'after/install.log').read_text()
    write_wheel(house, 'lookalike-provider', '1.0', module='release_fixture', source='def public_api(): return 4\n')
    incomplete = write_wheel(tmp_path / 'incomplete', 'release-fixture', '2.0', ['lookalike-provider'],
                             module='unrelated')
    output = tmp_path / 'lookalike'
    assert _functional(repo, baseline, incomplete, output, house) == 2
    assert json.loads(capsys.readouterr().out)['comparison']['issues']['after'] == {'module_origin_unverified': 1}
    assert 'cannot be attributed uniquely' in (output / 'after/pytest.log').read_text()


def test_sender_plan_preflights_and_freezes_inputs_once(isolated_functional_scope, tmp_path, capsys, monkeypatch):
    from reprolab import project_wheels
    from reprolab.runtime import PYTHON_ENVIRONMENT_PROBE
    import sys

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    repo, house, baseline = isolated_functional_scope
    broken = write_wheel(tmp_path / 'approved', 'release-fixture', '2.0', source='def public_api(): return 5\n')
    other = write_wheel(tmp_path / 'stale', 'release-fixture', '2.0', source='def public_api(): return 4\n')
    assert broken.name == other.name and sha(broken) != sha(other)
    assert main(['fingerprint-wheelhouse', str(house)]) == 0
    lock = json.loads(capsys.readouterr().out)['wheelhouse_sha256']
    environment = json.loads(subprocess.check_output(
        [sys.executable, '-I', '-c', PYTHON_ENVIRONMENT_PROBE + '\nimport json\nprint(json.dumps(reprolab_python_environment()))'],
    ))
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    plan = {'kind': 'reprolab_wheel_review_plan', 'schema_version': 1, 'gate': 'regression',
            'downstream_commit': commit, 'module': 'release_fixture', 'selected_tests': ['test_release.py::test_api'],
            'required_test_wheelhouse_sha256': lock, 'required_test_environment': environment,
            'wheel_artifacts': {side: {'filename': path.name, 'sha256': sha(path)}
                                for side, path in [('before', baseline), ('after', broken)]}}
    plan_file = tmp_path / 'maintainer.json'
    plan_file.write_text(json.dumps(plan))
    digest = sha(plan_file)
    output = tmp_path / 'functional'
    args = ['verify-wheels', str(repo), '--before-wheel', str(baseline), '--after-wheel', str(broken),
            '--review-plan', str(plan_file), '--test-wheelhouse', str(house), '--output', str(output)]
    assert main(args) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready['preflight_only'] is True and ready['functional_review_plan_sha256'] == digest
    assert not output.exists()
    for options in (('--ref', 'HEAD'), ('--module', 'release_fixture'), ('--test', 'test_release.py::test_api'),
                    ('--gate', 'regression'), ('--require-improvement', 'test_release.py::test_api')):
        assert main([*args, *options, '--confirm-run']) == 2
    capsys.readouterr()

    original = project_wheels._setup_wheel_test
    calls = []

    def swap_at_baseline(executable, frozen, *positional, **keywords):
        calls.append(sha(frozen))
        if len(calls) == 1:
            other.replace(broken)
            plan_file.write_text('{}')
        return original(executable, frozen, *positional, **keywords)

    monkeypatch.setattr(project_wheels, '_setup_wheel_test', swap_at_baseline)
    approved = broken.read_bytes()
    broken.write_bytes(other.read_bytes())
    assert main([*args, '--confirm-run']) == 2
    assert 'wheel_artifacts.after.sha256' in capsys.readouterr().err
    broken.write_bytes(approved)
    added = write_wheel(house, 'unexpected-input', '1.0')
    assert main([*args, '--confirm-run']) == 2
    assert 'required_test_wheelhouse_sha256' in capsys.readouterr().err
    added.unlink()
    plan_file.write_text(json.dumps({**plan, 'required_test_environment': {**environment, 'python_version': '3.0.0'}}))
    assert main([*args, '--confirm-run']) == 2
    assert 'test_environment.python_version' in capsys.readouterr().err
    assert calls == [] and not output.exists()

    plan_file.write_text(json.dumps(plan))
    saved_plan = tmp_path / 'receiver.json'
    shutil.copy2(plan_file, saved_plan)
    assert main([*args, '--confirm-run']) == 1
    report = json.loads(capsys.readouterr().out)
    assert calls == [plan['wheel_artifacts'][side]['sha256'] for side in ('before', 'after')]
    assert plan_file.read_text() == '{}'
    assert report['functional_review_plan_sha256'] == digest
    assert report['comparison']['gate']['exit_code'] == 1
    assert 'review_plan_acceptance' not in report
    assert main(['check-wheels', str(output), '--review-plan', str(saved_plan), '--verify-git-repository', str(repo)]) == 1
    capsys.readouterr()

    from reprolab.runner import RunControl
    from reprolab.wheel_review_plan import review_wheel_functional_execution

    manifest = output / 'wheel-comparison.json'
    record = json.loads(manifest.read_bytes())
    phases = output / 'after' / 'pytest-phases.json'
    phase = json.loads(phases.read_bytes())
    phase['environment']['python_version'] = '3.0.0'
    phases.write_text(json.dumps(phase))
    record['runs']['after']['evidence_sha256'][phases.name] = sha(phases)
    manifest.write_text(json.dumps(record))
    reviewed, code = review_wheel_functional_execution(output, plan, digest, control=RunControl())
    assert code == 2 and reviewed['comparison']['gate']['exit_code'] == 1
    assert reviewed['functional_plan_execution']['mismatched_fields'] == ['test_environment.after.python_version']
    phase['environment'] = environment
    phase['test_installation']['wheelhouse_sha256'] = '0' * 64
    phases.write_text(json.dumps(phase))
    record['runs']['after']['evidence_sha256'][phases.name] = sha(phases)
    manifest.write_text(json.dumps(record))
    reviewed, code = review_wheel_functional_execution(output, plan, digest, control=RunControl())
    assert code == 2
    assert reviewed['functional_plan_execution']['mismatched_fields'] == ['test_installation.after.wheelhouse_sha256']
