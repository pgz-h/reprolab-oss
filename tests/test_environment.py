import json
from pathlib import Path
import subprocess
import sys

import pytest


def api():
    from reprolab import environment
    return environment


def test_snapshot_records_only_metadata_and_is_deterministic(tmp_path, monkeypatch):
    env = api()
    info = tmp_path / 'benign_demo-1.0.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: Benign_Demo\nVersion: 1.0\nHome-page: https://secret.invalid/token\n')
    (info / 'direct_url.json').write_text('{"url":"https://secret.invalid/credential"}')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv('PRIVATE_API_KEY', 'not-for-reports')
    first = env.snapshot_environment()
    second = env.snapshot_environment()
    assert first['status'] == 'recorded'
    assert first['distributions']['benign-demo'] == '1.0'
    assert first['fingerprint'] == second['fingerprint']
    assert all(first[k] for k in ('executable', 'python_version', 'platform', 'machine', 'scope', 'notice'))
    assert 'secret.invalid' not in json.dumps(first)
    assert 'not-for-reports' not in json.dumps(first)
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: Benign_Demo\nVersion: 2.0\n')
    current = env.snapshot_environment()
    comparison = env.compare_environments(first, current)
    assert comparison['status'] == 'changed'
    assert {'name': 'benign-demo', 'before': '1.0', 'after': '2.0'} in comparison['changed']
    assert env.compare_environments(current, current)['status'] == 'same'


def test_external_venv_path_is_preserved_without_pytest_import(tmp_path):
    env = api()
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(tmp_path / 'venv')], check=True)
    executable = tmp_path / 'venv/bin/python'
    snapshot = env.snapshot_environment(executable)
    assert snapshot['status'] == 'recorded'
    assert snapshot['executable'] == str(executable)
    assert 'pytest' not in snapshot['distributions']


def test_frozen_own_executable_never_uses_python_flags(monkeypatch):
    env = api()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('frozen dispatcher must not be launched for metadata')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    assert env.snapshot_environment(sys.executable)['status'] == 'recorded'


def test_unavailable_and_legacy_environments_never_claim_same(tmp_path):
    env = api()
    missing = env.snapshot_environment(tmp_path / 'missing-python')
    assert missing['status'] == 'unavailable'
    assert missing['fingerprint'] is None
    for old in (None, {}, {'python_version': '3.11'}, missing):
        assert env.compare_environments(old, missing)['status'] == 'unknown'


def test_inventory_added_removed_and_interpreter_changes(monkeypatch):
    env = api()
    first = env.snapshot_environment()
    second = {**first, 'distributions': {'fresh': '2'}, 'executable': '/new/python', 'python_version': '3.99'}
    result = env.compare_environments(first, second)
    assert result['status'] == 'changed'
    assert result['path_changed'] and result['python_changed']
    assert result['added'] == [{'name': 'fresh', 'version': '2'}]
    assert result['removed']


def test_probe_errors_and_non_metadata_output_are_unavailable(tmp_path):
    env = api()
    executable = tmp_path / 'not-python'
    executable.write_text('#!/bin/sh\nprintf "private-token-in-output"\n')
    executable.chmod(0o755)
    result = env.snapshot_environment(executable)
    assert result['status'] == 'unavailable'
    assert 'private-token' not in json.dumps(result)


def test_broken_distribution_inventory_is_unavailable(monkeypatch):
    env = api()
    def broken():
        raise AttributeError('broken metadata provider')
    monkeypatch.setattr(env.importlib.metadata, 'distributions', broken)
    assert env.snapshot_environment()['status'] == 'unavailable'
