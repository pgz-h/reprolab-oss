"""Exercise environment recovery from the actual project and dialog entry points."""
from pathlib import Path
import sys

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from reprolab.desktop_environment import ProjectEnvironmentDialog
from test_desktop_ui import window
from test_desktop_usability import project, wait_project
from test_project_config_ui import wait
from test_workflow import make_environment


def open_dialog(project, tmp_path, **kwargs):
    dialog = ProjectEnvironmentDialog(project, python_executable=sys.executable, work_root=tmp_path, **kwargs)
    dialog.show()
    wait(lambda: dialog.inspection is not None and dialog.task is None)
    return dialog


def test_frozen_dialog_lists_original_environments_without_switching_or_starting_them(window, tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    frozen = tmp_path / 'frozen'; frozen.mkdir()
    marker = tmp_path / 'unexpected-start'
    candidate = repo / '.venv/bin/python'; candidate.parent.mkdir(parents=True)
    candidate.write_text(f'#!/bin/sh\ntouch "{marker}"\n'); candidate.chmod(0o755)
    dialog = open_dialog(frozen, tmp_path, repository=repo, can_refresh=True)
    try:
        assert dialog.python_combo.findText(str(candidate)) >= 0
        assert dialog.python_combo.currentText() == sys.executable
        assert not marker.exists()
    finally:
        dialog.reject()


def test_review_acknowledgement_is_explicit_and_resets_after_reinspection(window, tmp_path):
    (tmp_path / 'requirements.txt').write_text('-e .\n')
    dialog = open_dialog(tmp_path, tmp_path)
    try:
        assert dialog.inspection['status'] == 'review'
        assert dialog.reuse_review.isVisible() and not dialog.use_button.isEnabled()
        dialog.use_environment()
        assert not dialog.result_data
        dialog.reuse_review.setChecked(True)
        assert dialog.use_button.isEnabled()
        dialog.inspect_button.click()
        wait(lambda: dialog.inspection is not None and dialog.task is None)
        assert not dialog.reuse_review.isChecked() and not dialog.use_button.isEnabled()
        dialog.reuse_review.setChecked(True); dialog.use_button.click()
        assert dialog.result_data['environment']['status'] == 'review'
        assert dialog.result_data['review_acknowledged'] is True
    finally:
        dialog.reject()
    reopened = open_dialog(tmp_path, tmp_path)
    try:
        assert not reopened.reuse_review.isChecked()
        assert not reopened.use_button.isEnabled()
    finally:
        reopened.reject()


def test_switching_python_preserves_plugin_draft_but_not_old_inspection(window, tmp_path):
    executable, _ = make_environment(tmp_path, True)
    dialog = open_dialog(tmp_path, tmp_path)
    try:
        dialog.plugin_name.setText('local_plugin'); dialog.add_plugin_button.click()
        dialog.plugin_mode.setCurrentIndex(dialog.plugin_mode.findData('importlib'))
        dialog.python_combo.setEditText(str(executable))
        assert dialog.inspection is None and not dialog.use_button.isEnabled()
        dialog.inspect_button.click()
        wait(lambda: dialog.inspection is not None and dialog.task is None)
        assert dialog._plugin_draft == {'plugins': ['local_plugin'], 'import_mode': 'importlib'}
        assert 'local_plugin' in dialog.plugin_preview.toPlainText()
        assert not (tmp_path / 'pyproject.toml').exists()
    finally:
        dialog.reject()


@pytest.mark.parametrize('action', ['collect', 'use', 'cancel'])
def test_discovery_failure_can_reuse_existing_environment_and_continue_same_snapshot(window, tmp_path, action):
    repo = project(tmp_path, missing_dependency=True)
    (repo / 'requirements.txt').write_text('-e .\n')
    executable, purelib = make_environment(tmp_path, True)
    (purelib / 'reprolab_missing_demo_dependency.py').write_text('AVAILABLE = True\n')
    (repo / '.venv').symlink_to(executable.parent.parent, target_is_directory=True)
    selected = str(repo / '.venv/bin/python')
    marker = tmp_path / 'body-ran'
    path = repo / 'tests/test_component.py'
    path.write_text(path.read_text() + f'\ndef test_body_marker():\n    from pathlib import Path\n    Path({str(marker)!r}).touch()\n')
    bench = window.project_workbench; wait_project(bench)
    window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo)); bench.prepare_discover_button.click(); wait_project(bench)
    assert bench.collection['status'] == 'collection_failed'
    directory = bench.state['directory']; hashes = bench.state['source_sha256'].copy()
    errors = []
    def operate():
        dialog = bench.environment_dialog
        try:
            wait(lambda: dialog.inspection is not None and dialog.task is None)
            assert dialog.python_combo.findText(selected) >= 0
            dialog.python_combo.setCurrentIndex(dialog.python_combo.findText(selected))
            dialog.inspect_button.click()
            wait(lambda: dialog.inspection is not None and dialog.task is None)
            dialog.reuse_review.setChecked(True)
            assert dialog.use_collect_button.isEnabled()
            if action == 'collect':
                dialog.use_collect_button.click()
            elif action == 'use':
                dialog.use_button.click()
            else:
                dialog.reject()
        except BaseException as exc:
            errors.append(exc); dialog.reject()
    QTimer.singleShot(0, operate)
    bench.problem_environment_button.click()
    assert not errors, errors
    if action != 'collect':
        QApplication.processEvents()
        assert not bench.busy and bench.report is None and not marker.exists()
        assert bench.state['directory'] == directory and bench.state['source_sha256'] == hashes
        if action == 'cancel':
            assert bench.collection['status'] == 'collection_failed'
            assert bench.python_edit.text() != selected
        else:
            assert bench.collection is None and bench.python_edit.text() == selected
        return
    wait(lambda: bench.collection is not None and bench.collection['status'] == 'collected' and not bench.busy)
    assert bench.state['directory'] == directory and bench.state['source_sha256'] == hashes
    assert bench.python_edit.text() == selected
    assert not marker.exists() and bench.report is None
    assert len(bench.selected_test_ids()) == 6
    bench.run_button.click(); wait_project(bench)
    assert marker.exists() and bench.report['status'] == 'regression_detected'
    assert bench.report['runtime']['executable'] == selected


@pytest.mark.parametrize('change', ['plugin-draft', 'saved-profile', 'variable-draft'])
def test_continue_collection_never_silently_ignores_configuration_changes(window, tmp_path, change):
    repo = tmp_path / 'repo'; repo.mkdir()
    frozen = tmp_path / 'frozen'; frozen.mkdir()
    dialog = open_dialog(frozen, tmp_path, repository=repo, can_refresh=True, can_collect=True)
    try:
        if change == 'plugin-draft':
            dialog.plugin_name.setText('local_plugin'); dialog.add_plugin_button.click()
        elif change == 'saved-profile':
            (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\nimport_mode="importlib"\n')
        else:
            dialog.variables_panel.add_variable('LOCAL_TOKEN', 'LOCAL_SECRET', local=True)
        dialog.use_collect_button.click()
        assert not dialog.result_data and dialog.isVisible()
        assert '尚未重新发现' in dialog.status_label.text()
        assert 'LOCAL_SECRET' not in dialog.status_label.text()
    finally:
        dialog.reject()


def test_review_does_not_allow_known_missing_dependencies(window, tmp_path):
    (tmp_path / 'requirements.txt').write_text('-e .\nmissing-reprolab-recovery-dependency\n')
    dialog = open_dialog(tmp_path, tmp_path, can_collect=True)
    try:
        assert dialog.inspection['status'] == 'review'
        assert dialog.inspection['reuse']['status'] == 'blocked'
        dialog.reuse_review.setChecked(True)
        assert not dialog.use_button.isEnabled() and not dialog.use_collect_button.isEnabled()
        dialog.use_environment(collect=True)
        assert not dialog.result_data
    finally:
        dialog.reject()
