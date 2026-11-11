import json
from pathlib import Path
import time

import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QLineEdit
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from reprolab.desktop_environment import ProjectEnvironmentDialog
from reprolab.desktop_runtime_environment import LocalValuesDialog
from reprolab.desktop_store import DesktopStore
from reprolab.pytest_profile import read_pytest_profile
from test_pytest_profile import make_project


def wait(dialog):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        QApplication.processEvents(); QTest.qWait(10)
        if dialog.task is None: return
    raise AssertionError('dialog still busy')


def test_variable_preview_never_contains_local_value_and_undo_restores_configuration(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    repo = make_project(tmp_path / 'project')
    original = (repo / 'pyproject.toml').read_bytes()
    dialog = ProjectEnvironmentDialog(repo, repository=repo, work_root=tmp_path / 'work')
    dialog.show(); wait(dialog)
    panel = dialog.variables_panel
    panel.add_variable('APP_MODE', 'testing', local=False)
    panel.add_variable('DATABASE_TOKEN', 'private-input', local=True)
    assert panel.table.cellWidget(1, 2).echoMode() == QLineEdit.EchoMode.Password
    dialog.tabs.setCurrentWidget(panel); app.processEvents()
    assert panel.table.columnWidth(0) >= 200 and panel.table.columnWidth(1) >= 200
    previews = []
    def reject(plan):
        previews.append(plan.diff)
        return False
    monkeypatch.setattr(dialog, '_review_configuration', reject)
    panel.apply_button.click()
    assert (repo / 'pyproject.toml').read_bytes() == original and not dialog.runtime_values
    assert 'private-input' not in previews[0] and 'DATABASE_TOKEN' in previews[0]
    monkeypatch.setattr(dialog, '_review_configuration', lambda plan: True)
    panel.apply_button.click(); wait(dialog)
    profile = read_pytest_profile(repo)
    assert profile['environment'] == {'APP_MODE': 'testing'} and profile['pass_env'] == ['DATABASE_TOKEN']
    assert dialog.runtime_values == {'DATABASE_TOKEN': 'private-input'}
    for path in (tmp_path / 'work').rglob('*'):
        if path.is_file(): assert b'private-input' not in path.read_bytes()
    store = DesktopStore(tmp_path / 'state')
    store.set_runtime_values(repo, dialog.runtime_values); store.save()
    assert 'private-input' not in (store.directory / 'settings.json').read_text()
    assert DesktopStore(store.directory).local_runtime_values(repo) == {}
    assert store.local_runtime_values(tmp_path / 'different') == {}
    dialog.undo_plugins_button.click(); wait(dialog)
    assert (repo / 'pyproject.toml').read_bytes() == original
    dialog.close(); app.processEvents()


def test_local_value_dialog_requires_missing_inputs_without_echo_or_disk_storage(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.delenv('REQUIRED_VALUE', raising=False)
    dialog = LocalValuesDialog(['REQUIRED_VALUE'])
    dialog.accept()
    assert dialog.result() == 0 and 'REQUIRED_VALUE' in dialog.error.text()
    dialog.edits['REQUIRED_VALUE'].setText('only-in-memory')
    dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted and dialog.values == {'REQUIRED_VALUE': 'only-in-memory'}
    dialog.close(); app.processEvents()


def test_delete_uses_the_focused_variable_row(tmp_path):
    app = QApplication.instance() or QApplication([])
    repo = make_project(tmp_path / 'project')
    dialog = ProjectEnvironmentDialog(repo, repository=repo, work_root=tmp_path / 'work')
    dialog.show(); wait(dialog)
    panel = dialog.variables_panel; dialog.tabs.setCurrentWidget(panel)
    panel.add_variable('FIRST', 'first', local=False)
    panel.add_variable('SECOND', 'second', local=False)
    app.processEvents()
    QTest.mouseClick(panel.table.cellWidget(0, 2), Qt.MouseButton.LeftButton)
    panel.remove_button.click()
    assert panel.draft()[0] == {'SECOND': 'second'}
    dialog.close(); app.processEvents()


from test_desktop_ui import window, wait_idle


def strict_case(tmp_path):
    from test_runtime_environment import configured
    _, state = configured(tmp_path)
    folder = Path(state['directory'])
    path = tmp_path / 'strict.json'
    path.write_text(json.dumps({'title': 'local variable case', 'before': str(folder / 'inputs/before'),
        'after': str(folder / 'inputs/after'), 'test': str(folder / 'inputs/after/tests/test_calc.py'),
        'test_target': 'tests/test_calc.py::test_answer', 'expected_failure': {'type': 'AssertionError', 'message_contains': '42'}}))
    return path


def test_desktop_strict_case_prompts_and_passes_local_values_to_worker(window, tmp_path, monkeypatch):
    from test_runtime_environment import SECRET
    path = strict_case(tmp_path)
    monkeypatch.delenv('DATABASE_TOKEN', raising=False)
    def provide(dialog):
        dialog.edits['DATABASE_TOKEN'].setText(SECRET)
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(LocalValuesDialog, 'exec', provide)
    window.start_verification(path); wait_idle(window)
    assert window.last_result['status'] == 'verified'
    assert SECRET not in json.dumps(window.last_result)
    assert SECRET not in (window.store.directory / 'settings.json').read_text()
    assert SECRET not in (window.store.directory / 'activity.json').read_text()


def test_cancelling_local_values_keeps_existing_case_activity(window, tmp_path, monkeypatch):
    path = strict_case(tmp_path)
    previous = {'status': 'verified', 'report': 'previous-report', 'stamp': 'old-stamp'}
    window.store.activity[str(path)] = dict(previous)
    monkeypatch.setattr(LocalValuesDialog, 'exec', lambda dialog: dialog.DialogCode.Rejected)
    window.start_verification(path)
    assert window.store.activity[str(path)] == previous
    assert window.task is None


def test_imported_project_replay_can_request_local_values_without_original_repository(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog, QDoubleSpinBox, QLabel
    from test_runtime_environment import configured, control, SECRET
    from reprolab.project_sessions import run_project
    from test_desktop_usability import wait_project
    repo, state = configured(tmp_path)
    report = run_project(state['directory'], tmp_path / 'source-reports', timeout=8, confirmed=True, control=control())
    repo.rename(tmp_path / 'moved-project')
    bench = window.project_workbench; wait_project(bench)
    bench._apply_report(report)
    def confirm(dialog):
        assert dialog.windowTitle() == '重放项目报告'
        assert report['runtime']['executable'] in [label.text() for label in dialog.findChildren(QLabel)]
        timeout = dialog.findChild(QDoubleSpinBox)
        assert timeout.value() == 8
        timeout.setValue(15)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, 'exec', confirm)
    def provide(dialog):
        dialog.edits['DATABASE_TOKEN'].setText(SECRET); dialog.accept(); return dialog.result()
    monkeypatch.setattr(LocalValuesDialog, 'exec', provide)
    assert bench.replay_report()
    wait_project(bench)
    assert bench.report['status'] == 'regression_passed' and bench.report['run_id'] != report['run_id']
    assert bench.report['timeout'] == 15
