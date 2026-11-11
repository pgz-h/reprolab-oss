"""Real Qt interactions for reviewing, revising and recovering plans offline."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from test_desktop_creation import wait_idle
from test_suite_review import planned_suite
from test_suite_recovery import prepared_suite, fail_publication


def test_suite_review_shows_the_exact_failure_conditions(planned_suite, tmp_path):
    from reprolab.desktop_store import DesktopStore
    from reprolab.desktop_suite import AISuiteDialog
    app = QApplication.instance() or QApplication([])
    dialog = AISuiteDialog(DesktopStore(tmp_path / 'desktop'))
    dialog.load_plan(planned_suite['directory'])
    assert 'ValueError' in dialog.review.toPlainText()
    assert 'incorrect AI assumption' in dialog.review.toPlainText()
    assert '目标修复' in dialog.review.toPlainText()
    dialog.close()
    app.processEvents()


def test_editor_preserves_invalid_input_then_saves_a_corrected_copy(planned_suite, tmp_path):
    from reprolab.desktop_suite_review import SuiteReviewDialog
    app = QApplication.instance() or QApplication([])
    dialog = SuiteReviewDialog(planned_suite['directory'], tmp_path / 'reviewed')
    dialog.show()
    dialog.failure_message_edit.clear()
    dialog.save_button.click()
    wait_idle(dialog)
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not dialog.result_data
    assert dialog.failure_message_edit.text() == ''
    assert dialog.code_edit.toPlainText().startswith('from sample import compute')
    dialog.failure_message_edit.setText('reported bug')
    dialog.title_edit.setText('已审阅的检查')
    dialog.save_button.click()
    wait_idle(dialog)
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.status_label.text()
    assert dialog.result_data['directory'] != planned_suite['directory']
    assert dialog.result_data['plan']['title'] == '已审阅的检查'
    assert dialog.result_data['plan']['checks'][0]['expected_failure']['message_contains'] == 'reported bug'
    dialog.close()
    app.processEvents()


def test_editor_keeps_changes_when_switching_checks_and_selects_project_target(planned_suite, tmp_path):
    from reprolab.desktop_suite_review import SuiteReviewDialog
    app = QApplication.instance() or QApplication([])
    dialog = SuiteReviewDialog(planned_suite['directory'], tmp_path / 'reviewed')
    dialog.show()
    dialog.failure_message_edit.setText('reported bug')
    dialog.add_button.click()
    dialog.id_edit.setText('guard')
    dialog.check_title_edit.setText('保护参数化行为')
    dialog.role_combo.setCurrentIndex(dialog.role_combo.findData('regression'))
    dialog.source_combo.setCurrentIndex(dialog.source_combo.findData('project'))
    dialog.target_edit.setText('tests/test_project.py::test_compute[42]')
    dialog.purpose_edit.setPlainText('保护正常输出')
    dialog.expected_edit.setPlainText('应输出42')
    dialog.assumptions_edit.setPlainText('用户已核对项目 fixture')
    dialog.check_list.setCurrentRow(0)
    assert dialog.failure_message_edit.text() == 'reported bug'
    dialog.check_list.setCurrentRow(1)
    assert dialog.target_edit.text() == 'tests/test_project.py::test_compute[42]'
    dialog.save_button.click()
    wait_idle(dialog)
    checks = dialog.result_data['plan']['checks']
    assert len(checks) == 2 and checks[1]['role'] == 'regression'
    assert checks[1]['test_code'] is None and checks[1]['expected_failure'] is None
    dialog.close()
    app.processEvents()


def test_main_window_switches_to_reviewed_plan_and_clears_old_result(planned_suite, tmp_path):
    from reprolab.desktop_store import DesktopStore
    from reprolab.desktop_suite import AISuiteDialog
    from reprolab.desktop_suite_review import SuiteReviewDialog
    app = QApplication.instance() or QApplication([])
    dialog = AISuiteDialog(DesktopStore(tmp_path / 'desktop'))
    dialog.load_plan(planned_suite['directory'])
    dialog.last_summary = 'previous report'
    dialog.explain_button.setEnabled(True)
    opened = []

    def edit():
        editor = QApplication.activeModalWidget()
        assert isinstance(editor, SuiteReviewDialog)
        opened.append(editor)
        editor.failure_message_edit.setText('reported bug')
        editor.save_button.click()
        watchdog = QTimer(editor)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(editor.reject)
        editor.finished.connect(watchdog.stop)
        watchdog.start(10000)

    QTimer.singleShot(30, edit)
    dialog.edit_button.click()
    assert dialog.directory != planned_suite['directory'], opened[0].status_label.text()
    assert dialog.last_summary is None and not dialog.explain_button.isEnabled()
    assert 'reported bug' in dialog.review.toPlainText()
    dialog.close()
    app.processEvents()


def test_main_window_recovers_saved_response_without_model_request(prepared_suite, tmp_path, monkeypatch):
    from reprolab import repair_suite
    from reprolab.desktop_store import DesktopStore
    from reprolab.desktop_suite import AISuiteDialog
    folder, response = prepared_suite
    with monkeypatch.context() as failure:
        fail_publication(failure)
        with pytest.raises(OSError):
            repair_suite.generate_suite(folder, response_file=response)
    monkeypatch.setattr(repair_suite, 'generate_api', lambda *a, **k: pytest.fail('恢复不应联网'))
    app = QApplication.instance() or QApplication([])
    dialog = AISuiteDialog(DesktopStore(tmp_path / 'desktop'))
    dialog.load_plan(folder)
    assert dialog.recover_button.isEnabled()
    dialog.recover_button.click()
    wait_idle(dialog)
    assert dialog.state['status'] == 'planned'
    assert dialog.check_tree.topLevelItemCount() == 1
    assert not dialog.recover_button.isEnabled() and dialog.edit_button.isEnabled()
    assert not Path(folder, 'reports').exists()
    dialog.close()
    app.processEvents()


def test_failed_generation_enables_recovery_immediately_and_keeps_error(prepared_suite, tmp_path, monkeypatch):
    from reprolab.desktop_store import DesktopStore
    from reprolab.desktop_suite import AISuiteDialog
    folder, response = prepared_suite
    app = QApplication.instance() or QApplication([])
    dialog = AISuiteDialog(DesktopStore(tmp_path / 'desktop'))
    dialog.load_plan(folder)
    assert not dialog.recover_button.isEnabled()
    with monkeypatch.context() as failure:
        fail_publication(failure)
        dialog.import_response(response)
        wait_idle(dialog)
    assert dialog.recover_button.isEnabled()
    assert 'disk failure' in dialog.status_label.text()
    dialog.recover_button.click()
    wait_idle(dialog)
    assert dialog.state['status'] == 'planned'
    assert not dialog.recover_button.isEnabled()
    dialog.close()
    app.processEvents()


def test_revised_plan_can_run_immediately_without_deleting_a_live_qt_thread(planned_suite, tmp_path):
    # A subprocess makes a native Qt lifetime crash an ordinary test failure.
    script = '''
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from reprolab.desktop_store import DesktopStore
from reprolab.desktop_suite import AISuiteDialog
app = QApplication([])
root = Path(sys.argv[2])
store = DesktopStore(root / 'state')
store.settings.update(cases=str(root/'cases'), reports=str(root/'reports'))
suite = AISuiteDialog(store)
suite.load_plan(sys.argv[1])
def edit():
    editor = QApplication.activeModalWidget()
    editor.failure_message_edit.setText('reported bug')
    editor.save_button.click()
QTimer.singleShot(10, edit)
suite.edit_button.click()
assert suite.directory != sys.argv[1]
result = {}
timer = QTimer()
def completed():
    batch = suite.last_batch
    if batch and batch.task is None and batch.result_data:
        result.update(batch.result_data)
        timer.stop()
        batch.close_button.click()
timer.timeout.connect(completed)
timer.start(10)
suite.save_button.click()
assert result['status'] == 'verified', result
suite.close()
app.processEvents()
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    child = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script,
                            planned_suite['directory'], str(tmp_path / 'ui-run')],
                           capture_output=True, text=True, timeout=30, env=env)
    assert child.returncode == 0, child.stdout + child.stderr
