"""Recheck feedback appears in the normal result page without opening a dialog."""
from pathlib import Path
import time
import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtTest import QTest
from test_desktop_ui import window
from test_desktop_usability import wait_project
from test_recheck_shortcut import original_run
from test_processing_history import chain


def wait_review(bench):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        QApplication.processEvents(); QTest.qWait(10); time.sleep(.005)
        if bench.task is None and bench.review_progress is not None:
            return
    raise AssertionError('review feedback did not finish')


def test_real_quick_recheck_automatically_shows_progress_and_reopens(window, tmp_path):
    repo, state, original = original_run(tmp_path)
    b = window.project_workbench; wait_project(b)
    window.tabs.setCurrentWidget(b); b.open_session(state['directory']); wait_project(b)
    (repo / 'calc.py').write_text('def answer():\n    return 42\n')
    b.quick_recheck_all_action.trigger(); wait_review(b)
    assert b.review_progress['comparison']['same_check_resolved'] == 1
    assert b.review_panel.isVisible() and '同条件转绿 1 项' in b.review_summary.text()
    assert b.comparison_dialog is None
    assert b.review_progress['comparison']['after']['run_id'] == b.report['run_id']
    saved = b.state['directory']; b.new_project(); assert b.review_panel.isHidden()
    b.open_session(saved); wait_review(b)
    assert b.review_progress['comparison']['same_check_resolved'] == 1
    b.compare_source_button.click(); QApplication.processEvents()
    assert b.comparison_dialog.isVisible()
    b.comparison_dialog.close()


def test_missing_source_inline_recovery_preserves_report(window, chain, tmp_path, monkeypatch):
    b = window.project_workbench; wait_project(b); window.tabs.setCurrentWidget(b)
    ref = chain[1]['review_source']
    window.store.settings['project_report_locations'] = {ref['report_sha256']: str(tmp_path / 'missing')}
    b._apply_report(chain[1]); b.tabs.setCurrentIndex(2); wait_review(b)
    assert b.review_progress['status'] == 'unavailable'
    assert b.report['run_id'] == chain[1]['run_id'] and b.review_relocate_button.isVisible()
    assert b.problem_panel.isHidden()
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a, **k: chain[0]['report_dir'])
    b.review_relocate_button.click(); wait_review(b)
    assert b.review_progress['status'] == 'compared' and '未复查 2 项' in b.review_scope.text()
    assert b.comparison_dialog is None


def test_late_review_cannot_replace_a_different_displayed_report(window, chain):
    b = window.project_workbench; wait_project(b)
    b._apply_report(chain[1])
    b._apply_report(chain[0])
    QApplication.processEvents(); QTest.qWait(100); QApplication.processEvents()
    assert b.report['run_id'] == chain[0]['run_id']
    assert b.review_panel.isHidden() and b.review_progress is None


def test_stopping_read_keeps_execution_result_and_allows_retry(window, chain, monkeypatch):
    from reprolab import project_history as history
    real = history._checked_report
    started = []
    def slow(directory, *, control=None):
        started.append(True)
        while True:
            control.check()
            time.sleep(.01)
    monkeypatch.setattr(history, '_checked_report', slow)
    b = window.project_workbench; wait_project(b); window.tabs.setCurrentWidget(b)
    b._apply_report(chain[1]); b.tabs.setCurrentIndex(2)
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        QApplication.processEvents(); QTest.qWait(10); time.sleep(.005)
    assert started and b.cancel_button.text() == '停止核对'
    b.cancel_button.click(); wait_review(b)
    assert b.review_progress['status'] == 'cancelled'
    assert b.report['status'] == chain[1]['status'] and b.results_tree.topLevelItemCount() == 1
    assert b.review_retry_button.isVisible() and not b.problem_panel.isVisible()
    monkeypatch.setattr(history, '_checked_report', real)
    b.review_retry_button.click(); wait_review(b)
    assert b.review_progress['status'] == 'compared'


def test_real_recheck_with_changed_timeout_shows_review_instead_of_resolution(window, tmp_path):
    repo, state, _ = original_run(tmp_path)
    b = window.project_workbench; wait_project(b); window.tabs.setCurrentWidget(b)
    b.open_session(state['directory']); wait_project(b)
    b.timeout_spin.setValue(30)
    (repo / 'calc.py').write_text('def answer():\n    return 42\n')
    b.quick_recheck_all_action.trigger(); wait_review(b)
    assert b.report['status'] == 'regression_passed'
    assert '同条件转绿 0 项' in b.review_summary.text() and '需复核 1 项' in b.review_summary.text()
