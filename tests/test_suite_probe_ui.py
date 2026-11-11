"""The desktop uses recorded old-version failures only as reviewed prefill data."""
import os
from pathlib import Path
import time
import sys

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from reprolab.desktop_store import DesktopStore
from reprolab.desktop_suite import AISuiteDialog
from reprolab.suite_review import editable_plan, revise_suite
from test_desktop_creation import wait_idle
from test_suite_review import planned_suite
from test_suite_probe import project_suite


def window(state, root):
    store = DesktopStore(root / 'desktop')
    store.settings.update(cases=str(root / 'cases'), reports=str(root / 'reports'))
    dialog = AISuiteDialog(store)
    dialog.load_plan(state['directory'])
    dialog.show()
    return dialog


def test_probe_then_confirm_prefills_review_and_runs_a_new_revision(planned_suite, tmp_path):
    app = QApplication.instance() or QApplication([])
    dialog = window(planned_suite, tmp_path)
    assert not dialog.use_probe_button.isEnabled()
    dialog.probe_button.click()
    wait_idle(dialog)
    assert dialog.probe_data['status'] == 'failure_observed'
    assert 'reported bug' in dialog.probe_output.toPlainText()
    assert not dialog.use_probe_button.isEnabled()
    original = dialog.directory
    dialog.probe_confirm.setChecked(True)
    assert dialog.use_probe_button.isEnabled()

    def save_prefilled():
        editor = QApplication.activeModalWidget()
        assert editor.failure_type_edit.text() == 'ValueError'
        assert editor.failure_message_edit.text() == 'reported bug'
        editor.save_button.click()
        watchdog = QTimer(editor)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(editor.reject)
        editor.finished.connect(watchdog.stop)
        watchdog.start(15000)

    QTimer.singleShot(20, save_prefilled)
    dialog.use_probe_button.click()
    assert dialog.directory != original
    assert dialog.probe_data is None and not dialog.use_probe_button.isEnabled()
    from reprolab.repair_suite import run_suite
    assert run_suite(dialog.directory, tmp_path / 'accepted', confirmed=True)['status'] == 'verified'
    dialog.close()
    app.processEvents()


def test_switching_check_or_python_invalidates_the_observation(planned_suite, tmp_path):
    app = QApplication.instance() or QApplication([])
    plan = editable_plan(planned_suite['directory'])
    check = dict(plan['checks'][0])
    check.update(id='other', title='另一检查')
    plan['checks'].append(check)
    state = revise_suite(planned_suite['directory'], plan, tmp_path / 'reviewed')
    dialog = window(state, tmp_path)
    dialog.probe_button.click()
    wait_idle(dialog)
    dialog.probe_confirm.setChecked(True)
    assert dialog.use_probe_button.isEnabled()
    dialog.check_tree.setCurrentItem(dialog.check_tree.topLevelItem(1))
    assert dialog.probe_data is None and not dialog.probe_confirm.isChecked()
    dialog.probe_button.click()
    wait_idle(dialog)
    dialog.python_edit.setText('/different/python')
    assert dialog.probe_data is None and not dialog.use_probe_button.isEnabled()
    dialog.close()
    app.processEvents()


def test_saved_observation_reopens_and_tampered_log_cannot_prefill(planned_suite, tmp_path):
    from reprolab.suite_probe import probe_suite_check
    app = QApplication.instance() or QApplication([])
    observed = probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    dialog = window(planned_suite, tmp_path)
    dialog.open_probe(observed['probe_dir'])
    assert dialog.probe_data['probe_id'] == observed['probe_id']
    dialog.probe_confirm.setChecked(True)
    Path(observed['probe_dir'], 'before.log').write_text('changed log')
    dialog.use_probe_button.click()
    assert dialog.last_review is None
    assert not dialog.use_probe_button.isEnabled()
    assert '变化' in dialog.status_label.text() or '一致' in dialog.status_label.text()
    dialog.close()
    app.processEvents()


def test_probe_can_be_cancelled_and_closing_waits_for_cleanup(tmp_path):
    app = QApplication.instance() or QApplication([])
    marker = tmp_path / 'entered'
    state = project_suite(tmp_path, 'from pathlib import Path\nimport time\nfrom sample import compute\n'
                          f'def test_one():\n    Path({str(marker)!r}).write_text("entered")\n'
                          '    time.sleep(20)\n    compute()\n')
    dialog = window(state, tmp_path)
    dialog.probe_button.click()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert marker.exists()
    assert dialog.cancel_probe_button.isEnabled()
    dialog.cancel_probe_button.click()
    wait_idle(dialog)
    assert dialog.probe_data is None
    assert not dialog.use_probe_button.isEnabled()
    assert '取消' in dialog.status_label.text()
    marker.unlink()
    dialog.probe_button.click()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert marker.exists()
    dialog.reject()
    wait_idle(dialog)
    assert dialog.result() == QDialog.DialogCode.Rejected and not dialog.isVisible()
    app.processEvents()


def test_clearing_saved_python_uses_the_builtin_interpreter_for_probe(tmp_path):
    from test_workflow import make_environment
    app = QApplication.instance() or QApplication([])
    python, _ = make_environment(tmp_path, True)
    state = project_suite(tmp_path, 'from sample import compute\ndef test_one(): compute()\n', python_executable=python)
    dialog = window(state, tmp_path)
    assert dialog.python_edit.text() == str(python)
    dialog.python_edit.clear()
    dialog.probe_button.click()
    wait_idle(dialog)
    assert dialog.probe_data['runtime']['executable'] == sys.executable
    dialog.close()
    app.processEvents()


def test_confirmed_display_is_bound_to_the_same_observation_record(planned_suite, tmp_path):
    from reprolab.suite_probe import probe_suite_check
    app = QApplication.instance() or QApplication([])
    first = probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    second = probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    dialog = window(planned_suite, tmp_path)
    dialog.open_probe(first['probe_dir'])
    dialog.probe_confirm.setChecked(True)
    Path(first['probe_dir']).rename(tmp_path / 'original-probe')
    Path(second['probe_dir']).rename(first['probe_dir'])
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: app.activeModalWidget().reject() if app.activeModalWidget() else None)
    watchdog.start(20)
    dialog.use_probe_button.click()
    watchdog.stop()
    assert dialog.last_review is None
    assert dialog.probe_data is None and not dialog.use_probe_button.isEnabled()
    assert '变化' in dialog.status_label.text() or '一致' in dialog.status_label.text()
    dialog.close()
    app.processEvents()


def test_loading_prepared_plan_clears_confirmed_observation_with_same_python(planned_suite, tmp_path):
    from reprolab.suite_probe import probe_suite_check
    from reprolab.repair_suite import prepare_suite
    from test_git_source import prepare_repo
    app = QApplication.instance() or QApplication([])
    observed = probe_suite_check(planned_suite['directory'], 'repair', tmp_path / 'probes')
    root = tmp_path / 'another'
    root.mkdir()
    repo, before, after, _ = prepare_repo(root)
    prepared = prepare_suite(repo, before, after, '另一个问题', root / 'suites')
    dialog = window(planned_suite, tmp_path)
    dialog.open_probe(observed['probe_dir'])
    dialog.probe_confirm.setChecked(True)
    assert dialog.use_probe_button.isEnabled()
    dialog.load_plan(prepared['directory'])
    assert dialog.state['status'] == 'prepared'
    assert dialog.probe_data is None and not dialog.probe_confirm.isChecked()
    assert not dialog.use_probe_button.isEnabled() and not dialog.probe_button.isEnabled()
    assert dialog.check_tree.topLevelItemCount() == 0
    dialog.close()
    app.processEvents()
