"""Use real dialogs, original source edits, snapshot refresh and pytest execution."""
import hashlib
from pathlib import Path
import subprocess
import time

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from reprolab.project_sessions import check_project_report
from reprolab.pytest_profile import read_pytest_profile
from test_desktop_ui import window
from test_desktop_usability import wait_project


def wait(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents(); QTest.qWait(10); time.sleep(.003)
    assert predicate(), '等待界面操作超时'
    QApplication.processEvents()


def make_project(root):
    root.mkdir()
    (root / 'pyproject.toml').write_text('# preserve project comment\n[project]\nname="plugin-demo"\nversion="0.1"\ndependencies=[]\n[tool.pytest.ini_options]\naddopts="-k no_test_should_match --maxfail=1"\n')
    (root / 'support_plugin.py').write_text('import pytest\n@pytest.fixture\ndef expected(): return 42\n')
    (root / 'test_feature.py').write_text('def test_answer(expected):\n    assert expected == 42\n')
    def git(*args):
        subprocess.run(['git','-C',str(root),'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
                        '-c','user.name=ReproLab test','-c','user.email=test@example.invalid',*args],check=True,capture_output=True)
    git('init','-q'); git('add','.'); git('commit','-qm','baseline')
    return root


def review(dialog, button, *, accept=True, screenshot=None):
    errors=[]
    def respond():
        view=dialog.configuration_preview
        try:
            assert view is not None and 'pyproject.toml' in view.diff_view.toPlainText()
            if screenshot: view.grab().save(str(screenshot))
            if accept: QTest.mouseClick(view.apply_button, Qt.MouseButton.LeftButton)
            else: view.reject()
        except BaseException as exc:
            errors.append(exc)
            if view is not None: view.reject()
    QTimer.singleShot(0, respond)
    button.click()
    assert not errors, errors


def test_plugin_edit_cancel_apply_reopened_undo_and_refresh_use_real_desktop(window, tmp_path):
    repo=make_project(tmp_path/'repo')
    original=(repo/'pyproject.toml').read_bytes()
    bench=window.project_workbench
    wait_project(bench)
    window.navigation_buttons[bench].click()
    bench.repository_edit.setText(str(repo)); bench.before_combo.setEditText('HEAD')
    bench.workspace_check.setChecked(True); bench.prepare_button.click(); wait_project(bench)
    bench.discover_button.click(); wait_project(bench)
    bench.run_button.click(); wait_project(bench)
    assert bench.report['status']=='attention'
    state=bench.state.copy(); old_report=bench.report['report_dir']
    digest=hashlib.sha256((Path(old_report)/'report.json').read_bytes()).hexdigest()
    snapshot=Path(state['directory'])/'inputs/after/pyproject.toml'
    errors=[]

    def open_environment(callback):
        def operate():
            dialog=bench.environment_dialog
            try:
                wait(lambda: dialog.inspection is not None and dialog.task is None)
                callback(dialog)
            except BaseException as exc:
                errors.append(exc)
            finally:
                if dialog.isVisible(): dialog.reject()
        QTimer.singleShot(0, operate)
        bench.inspect_environment()
        assert not errors, errors

    def add(dialog):
        dialog.tabs.setCurrentIndex(1)
        dialog.plugin_name.setText('support_plugin'); dialog.add_plugin_button.click()
        assert 'support_plugin' in dialog.plugin_preview.toPlainText()

    def first(dialog):
        assert '-k no_test_should_match' in dialog.execution_view.toPlainText()
        add(dialog)
        review(dialog, dialog.edit_plugins_button, accept=False)
        assert (repo/'pyproject.toml').read_bytes()==original
        review(dialog, dialog.edit_plugins_button, screenshot=tmp_path/'configuration-preview.png')
        wait(lambda: dialog.task is None)
        assert read_pytest_profile(repo)['plugins']==['support_plugin']
        assert snapshot.read_bytes()==original
        assert dialog.refresh_configuration_button.isEnabled()
        dialog.grab().save(str(tmp_path/'configuration-applied.png'))
        dialog.tabs.setCurrentWidget(dialog.execution_view)
        dialog.grab().save(str(tmp_path/'execution-differences.png'))
    open_environment(first)
    assert bench.state['project_id']==state['project_id']

    def second(dialog):
        dialog.tabs.setCurrentIndex(1)
        assert dialog.undo_plugins_button.isEnabled()
        review(dialog, dialog.undo_plugins_button)
        wait(lambda: dialog.task is None)
        assert (repo/'pyproject.toml').read_bytes()==original
        assert not dialog.refresh_configuration_button.isEnabled()
        add(dialog); review(dialog, dialog.edit_plugins_button)
        wait(lambda: dialog.task is None)
        dialog.refresh_configuration_button.click()
    open_environment(second)
    wait(lambda: bench.state['project_id']!=state['project_id'] and not bench.busy)
    assert bench.state['source_sha256']['before']==state['source_sha256']['before']
    assert snapshot.read_bytes()==original
    assert bench.collection is None
    bench.discover_button.click(); wait_project(bench)
    bench.run_button.click(); wait_project(bench)
    assert bench.report['status']=='regression_passed'
    assert bench.report['pytest_profile']['plugins']==['support_plugin']
    assert bench.report['review_source']['report_dir']==old_report
    assert bench.report['review_source']['selection_mode']=='workspace'
    assert check_project_report(bench.report['report_dir'])['status']=='consistent'
    assert hashlib.sha256((Path(old_report)/'report.json').read_bytes()).hexdigest()==digest
    assert check_project_report(old_report)['status']=='consistent'
    window.grab().save(str(tmp_path/'configured-project-passed.png'))


def test_edit_before_freezing_rechecks_current_configuration(window, tmp_path):
    from reprolab.desktop_environment import ProjectEnvironmentDialog
    repo = make_project(tmp_path / 'repo')
    dialog = ProjectEnvironmentDialog(repo, repository=repo, work_root=tmp_path, parent=window)
    dialog.show()
    try:
        wait(lambda: dialog.inspection is not None and dialog.task is None)
        dialog.tabs.setCurrentIndex(1)
        dialog.plugin_name.setText('support_plugin'); dialog.add_plugin_button.click()
        review(dialog, dialog.edit_plugins_button)
        wait(lambda: dialog.task is None and dialog.inspection is not None
             and dialog.inspection['pytest_profile']['plugins'] == ['support_plugin'])
        assert 'support_plugin' in dialog.execution_view.toPlainText()
        assert not dialog.refresh_configuration_button.isVisible()
    finally:
        dialog.reject()
