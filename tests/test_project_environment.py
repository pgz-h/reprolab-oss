"""Project declarations are static; preparation requires an unchanged preview."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def api():
    from reprolab import project_environment
    return project_environment


def test_static_inspection_reports_versions_and_never_imports_project(tmp_path):
    env = api()
    (tmp_path / 'pyproject.toml').write_text('[project]\ndependencies = ["pytest>=8,<10", "missing-reprolab-test-package>=2"]\n')
    (tmp_path / 'setup.py').write_text('raise RuntimeError("must never execute")')
    result = env.inspect_project(tmp_path)
    assert result['status'] == 'attention'
    items = {row['name']: row for row in result['dependencies']}
    assert items['pytest']['status'] == 'satisfied'
    assert items['missing-reprolab-test-package']['status'] == 'missing'
    assert result['manifest_fingerprint'] and result['environment']['status'] == 'recorded'


def test_markers_use_selected_python_and_candidates_are_not_started(tmp_path, monkeypatch):
    env = api()
    (tmp_path / 'requirements.txt').write_text('win-only; sys_platform == "win32"\nlinux-only; sys_platform == "linux"\n')
    candidate = tmp_path / '.venv/bin/python'
    candidate.parent.mkdir(parents=True)
    candidate.write_text('#!/bin/sh\nexit 99\n')
    candidate.chmod(0o755)
    monkeypatch.setattr(env, 'snapshot_environment', lambda executable=None: {'status': 'recorded', 'executable': '/selected/python', 'python_version': '3.12.0', 'distributions': {'pytest': '8.4.0', 'win-only': '1'}})
    monkeypatch.setattr(env, '_marker_environment', lambda executable: {**env._local_marker_environment(), 'sys_platform': 'win32'})
    result = env.inspect_project(tmp_path, '/selected/python')
    rows = {row['name']: row for row in result['dependencies']}
    assert rows['win-only']['status'] == 'satisfied'
    assert rows['linux-only']['status'] == 'not-applicable'
    assert result['candidate_pythons'] == [str(candidate)]
    assert result['status'] == 'ready'


def test_includes_are_bounded_and_unsupported_lines_never_echo_credentials(tmp_path):
    env = api()
    (tmp_path / 'requirements.txt').write_text('-r nested.txt\n-e .\n--index-url https://user:SECRET@private.invalid/simple\nname @ https://SECRET.invalid/a.whl\n-r ../outside.txt\n')
    (tmp_path / 'nested.txt').write_text('pytest>=8,<10\n-r requirements.txt\n')
    result = env.inspect_project(tmp_path)
    assert result['status'] == 'review'
    assert any(row['status'] == 'needs-review' for row in result['dependencies'])
    assert 'SECRET' not in json.dumps(result)
    assert 'private.invalid' not in json.dumps(result)
    with pytest.raises(ValueError, match='人工'):
        env.plan_environment(tmp_path, tmp_path.parent / 'external', sys.executable)


def test_plan_rejects_stale_added_declarations_and_existing_destinations(tmp_path):
    env = api()
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'requirements.txt').write_text('pytest>=8,<10\n')
    destination = tmp_path / 'new-env'
    plan = env.plan_environment(project, destination, sys.executable)
    assert plan['index_url'] == 'https://pypi.org/simple'
    assert 'pytest<10,>=8' in plan['requirements']
    assert plan['files_sha256']['requirements.txt']
    (project / 'pyproject.toml').write_text('[project]\ndependencies = ["new-dependency"]\n')
    with pytest.raises(ValueError, match='改变|变化'):
        env.prepare_environment(plan)
    assert not destination.exists()
    destination.mkdir()
    with pytest.raises(ValueError, match='已存在'):
        env.plan_environment(project, destination, sys.executable)
    with pytest.raises(ValueError, match='项目.*之外'):
        env.plan_environment(project, project / 'inside-env', sys.executable)


def test_selected_venv_symlink_is_preserved(tmp_path):
    env = api()
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(tmp_path / 'env')], check=True)
    selected = tmp_path / 'env/bin/python'
    result = env.inspect_project(tmp_path, selected)
    assert result['environment']['executable'] == str(selected)
    assert result['runtime']['executable'] == str(selected)
    assert next(row for row in result['dependencies'] if row['name'] == 'pytest')['status'] == 'missing'


def test_frozen_dispatcher_cannot_be_used_to_prepare(tmp_path, monkeypatch):
    env = api()
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    with pytest.raises(ValueError, match='独立|内置'):
        env.plan_environment(project, tmp_path / 'new-env', sys.executable)


def test_prepare_cancel_preserves_status_without_secret_output(tmp_path, monkeypatch):
    env = api()
    from reprolab.runner import RunControl, RunCancelled
    project = tmp_path / 'project'
    project.mkdir()
    destination = tmp_path / 'new-env'
    plan = env.plan_environment(project, destination, sys.executable)
    control = RunControl()
    def stop(command, *, cwd, control, phase):
        control.cancel()
        control.check()
    monkeypatch.setattr(env, '_run_step', stop)
    with pytest.raises(RunCancelled):
        env.prepare_environment(plan, control=control)
    record = json.loads((destination / 'reprolab-environment.json').read_text())
    assert record['status'] == 'cancelled'
    assert (destination / 'preparation.log').is_file()


def test_dialog_preview_is_explicit_and_editing_invalidates_it(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from reprolab.desktop_environment import ProjectEnvironmentDialog
    import time
    app = QApplication.instance() or QApplication([])
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'requirements.txt').write_text('pytest>=8,<10\n')
    dialog = ProjectEnvironmentDialog(project, python_executable=sys.executable, work_root=tmp_path)
    dialog.show()
    def idle():
        deadline = time.monotonic() + 10
        while dialog.task is not None and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        app.processEvents()
        assert dialog.task is None
    app.processEvents()
    idle()
    assert dialog.inspection['status'] == 'ready'
    assert dialog.use_button.isEnabled()
    assert not dialog.create_button.isEnabled()
    dialog.destination_edit.setText(str(tmp_path / 'new-env'))
    dialog.preview_environment()
    idle()
    assert dialog.create_button.isEnabled()
    assert str(tmp_path / 'new-env') in dialog.preview.toPlainText()
    assert 'https://pypi.org/simple' in dialog.preview.toPlainText()
    assert not (tmp_path / 'new-env').exists()
    dialog.destination_edit.setText(str(tmp_path / 'different-env'))
    assert not dialog.create_button.isEnabled()
    dialog.use_environment()
    assert dialog.result_data['python'] == sys.executable
    dialog.close()


def test_dialog_close_waits_for_cancelled_task(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication, QDialog
    from PySide6.QtTest import QTest
    from reprolab.desktop_environment import ProjectEnvironmentDialog
    import threading
    import time
    app = QApplication.instance() or QApplication([])
    dialog = ProjectEnvironmentDialog(tmp_path, python_executable=sys.executable)
    app.processEvents()
    while dialog.task is not None:
        app.processEvents()
        QTest.qWait(10)
    started = threading.Event()
    def action(control):
        started.set()
        while True:
            control.check()
            time.sleep(.01)
    dialog.show()
    dialog._start(action, lambda result: None, '测试取消')
    assert started.wait(2)
    dialog.close()
    assert dialog.task is not None
    deadline = time.monotonic() + 5
    while dialog.task is not None and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert dialog.task is None
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_destination_symlink_is_existing_even_when_target_is_missing(tmp_path):
    env = api()
    project = tmp_path / 'project'
    project.mkdir()
    destination = tmp_path / 'existing-link'
    destination.symlink_to(tmp_path / 'missing-target')
    with pytest.raises(ValueError, match='已存在'):
        env.plan_environment(project, destination, sys.executable)
    assert destination.is_symlink()
    assert not (tmp_path / 'missing-target').exists()


def test_preparation_child_drops_credentials_and_cancels_own_group(tmp_path, monkeypatch):
    env = api()
    from reprolab.runner import RunControl, RunCancelled
    import threading
    import time
    monkeypatch.setenv('PRIVATE_API_KEY', 'fake-not-a-real-secret')
    monkeypatch.setenv('PIP_INDEX_URL', 'https://user:fake-password@invalid.invalid')
    monkeypatch.setenv('PIP_EXTRA_INDEX_URL', 'https://private.invalid')
    child_env = env._preparation_environment(tmp_path)
    assert 'PRIVATE_API_KEY' not in child_env
    assert 'PIP_INDEX_URL' not in child_env
    assert 'PIP_EXTRA_INDEX_URL' not in child_env
    assert child_env['PIP_CONFIG_FILE'] == os.devnull
    assert child_env['HOME'] == str(tmp_path / '.reprolab-home')
    control = RunControl()
    timer = threading.Timer(.2, control.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RunCancelled):
            env._run_step([sys.executable, '-I', '-c', 'import time; print("fake-secret-output"); time.sleep(30)'],
                          cwd=tmp_path, control=control, phase='creating')
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5
    assert not list(tmp_path.iterdir())
