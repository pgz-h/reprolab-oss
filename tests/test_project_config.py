"""Original project edits preserve unrelated content and reject stale changes."""
from dataclasses import replace
import json
from pathlib import Path
import os
import stat
import subprocess

import pytest

from reprolab.project_config import (preview_configuration, apply_configuration, preview_configuration_undo,
                                     undo_configuration, latest_configuration_edit)
from reprolab.pytest_profile import read_pytest_profile
from reprolab.runner import RunControl, RunCancelled


def repository(tmp_path, content=None):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    if content is not None:
        (root / 'pyproject.toml').write_bytes(content)
    return root


@pytest.mark.parametrize('content', [
    b'# keep comment\n[project]\nname = "demo" # project name\nversion = "1"\n',
    b'[project]\nname="demo"',
    b'[tool.reprolab.pytest]\nplugins = ["old"] # chosen plugins\nimport_mode = "append"\n\n[tool.other]\nvalue = 7 # keep\n',
    b'tool = {reprolab = {pytest = {plugins = ["old"]}}, other = {enabled = true}}\n',
    b'["tool"."reprolab"."pytest"]\nplugins = ["old"]\n\n[tool.other]\ntext = """[tool.reprolab.pytest]\nnot a header\n"""\n',
    b'\xef\xbb\xbf# keep comment\r\n[project]\r\nname="demo"\r\n',
])
def test_preview_apply_and_reopened_undo_preserve_original_bytes(tmp_path, content):
    root = repository(tmp_path, content)
    path = root / 'pyproject.toml'
    path.chmod(0o640)
    history = tmp_path / 'history'
    plan = preview_configuration(root, ['test_support', 'test_support'], 'importlib')
    assert path.read_bytes() == content and not history.exists()
    assert 'pyproject.toml' in plan.diff and plan.changed
    result = apply_configuration(plan, history)
    assert read_pytest_profile(root)['plugins'] == ['test_support']
    assert read_pytest_profile(root)['import_mode'] == 'importlib'
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    if b'# keep' in content:
        assert b'# keep' in path.read_bytes()
    if b'\r\n' in content:
        assert b'\n' not in path.read_bytes().replace(b'\r\n', b'')
    record = latest_configuration_edit(root, history)
    assert record == result['record']
    assert stat.S_IMODE((Path(record) / 'before.toml').stat().st_mode) == 0o600
    undo = preview_configuration_undo(root, record)
    assert path.read_bytes() == plan.updated
    assert undo_configuration(undo)['status'] == 'undone'
    assert path.read_bytes() == content and latest_configuration_edit(root, history) is None


def test_new_file_undo_removes_only_the_file_created_by_this_edit(tmp_path):
    root = repository(tmp_path)
    plan = preview_configuration(root, ['plugin'])
    record = apply_configuration(plan, tmp_path / 'history')
    undo_configuration(preview_configuration_undo(root, record['record']))
    assert not (root / 'pyproject.toml').exists()


@pytest.mark.parametrize('change', ['content', 'permissions', 'symlink'])
def test_apply_refuses_changes_since_preview_without_overwriting(tmp_path, change):
    root = repository(tmp_path, b'[project]\nname="demo"\n')
    plan = preview_configuration(root, ['plugin'])
    path = root / 'pyproject.toml'
    if change == 'content': path.write_text('# user edit\n[project]\nname="changed"\n')
    elif change == 'permissions': path.chmod(0o444)
    else:
        original = tmp_path / 'elsewhere.toml'; original.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(original)
    expected = path.read_bytes()
    with pytest.raises((ValueError, OSError)):
        apply_configuration(plan, tmp_path / 'history')
    assert path.read_bytes() == expected


def test_undo_refuses_later_user_edits_and_tampered_backups(tmp_path):
    root = repository(tmp_path, b'[project]\nname="demo"\n')
    record = apply_configuration(preview_configuration(root, ['plugin']), tmp_path / 'history')
    path = root / 'pyproject.toml'
    written = path.read_bytes()
    path.write_bytes(written + b'\n# later user edit\n')
    with pytest.raises(ValueError, match='变化'):
        preview_configuration_undo(root, record['record'])
    assert path.read_bytes().endswith(b'# later user edit\n')
    path.write_bytes(written)
    (Path(record['record']) / 'before.toml').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='不一致'):
        preview_configuration_undo(root, record['record'])


def test_cancel_and_failed_replace_leave_original_intact(tmp_path, monkeypatch):
    root = repository(tmp_path, b'[project]\nname="demo"\n')
    plan = preview_configuration(root, ['plugin'])
    control = RunControl(); control.cancel()
    with pytest.raises(RunCancelled): apply_configuration(plan, tmp_path / 'history', control=control)
    original_replace = os.replace
    def fail(source, destination):
        if Path(destination) == plan.path: raise OSError('simulated save failure')
        return original_replace(source, destination)
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError, match='save failure'): apply_configuration(plan, tmp_path / 'history')
    assert plan.path.read_bytes() == plan.original
    assert not list(root.glob('.reprolab-config-*'))


def test_invalid_config_or_forged_preview_never_changes_unrelated_settings(tmp_path):
    root = repository(tmp_path, b'[project]\nname="demo"\n')
    plan = preview_configuration(root, ['plugin'])
    forged = replace(plan, updated=plan.updated.replace(b'name="demo"', b'name="other"'))
    with pytest.raises(ValueError): apply_configuration(forged, tmp_path / 'history')
    assert plan.path.read_bytes() == plan.original
    plan.path.write_text('[tool.reprolab.pytest]\nunknown="keep"\n')
    with pytest.raises(ValueError): preview_configuration(root, ['plugin'])


def test_frozen_snapshot_and_in_project_history_are_rejected(tmp_path):
    root = repository(tmp_path, b'[project]\nname="demo"\n')
    snapshot = tmp_path / 'snapshot'; snapshot.mkdir()
    with pytest.raises(ValueError): preview_configuration(snapshot, ['plugin'])
    plan = preview_configuration(root, ['plugin'])
    with pytest.raises(ValueError, match='之外'): apply_configuration(plan, root / 'history')
    assert plan.path.read_bytes() == plan.original


def test_record_failure_after_write_is_visible_and_undo_can_recover(tmp_path, monkeypatch):
    import reprolab.project_config as config
    root = repository(tmp_path, b'[project]\nname="demo"\n')
    plan = preview_configuration(root, ['plugin'])
    save = config.save_json
    def fail_applied(path, value):
        if value.get('state') == 'applied': raise OSError('record state unavailable')
        return save(path, value)
    monkeypatch.setattr(config, 'save_json', fail_applied)
    result = apply_configuration(plan, tmp_path / 'history')
    assert result['status'] == 'applied' and result['warning']
    record = latest_configuration_edit(root, tmp_path / 'history')
    undo_configuration(preview_configuration_undo(root, record))
    assert plan.path.read_bytes() == plan.original


def test_concurrent_edit_is_blocked_and_previous_preview_cannot_overwrite_new_settings(tmp_path):
    import reprolab.project_config as config
    root = repository(tmp_path, b'[project]\nname="demo"\n')
    first = preview_configuration(root, ['first'])
    second = preview_configuration(root, ['second'])
    history = tmp_path / 'history'
    parent = config._history(root, history); parent.mkdir(parents=True)
    with config._edit_lock(parent):
        with pytest.raises(ValueError, match='正在保存'):
            apply_configuration(second, history)
    assert first.path.read_bytes() == first.original
    apply_configuration(first, history)
    with pytest.raises(ValueError, match='变化'):
        apply_configuration(second, history)
    assert read_pytest_profile(root)['plugins'] == ['first']
