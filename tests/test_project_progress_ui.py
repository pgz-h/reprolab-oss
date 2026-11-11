"""Normal desktop entries show worker events and meaningful next actions."""
import time
from pathlib import Path

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from test_desktop_ui import window
from test_desktop_usability import wait_project
from test_project_progress import IDS, prepare
from test_project_failure import observation
from test_project_failure_ui import show_report


def test_live_progress_and_cancel_use_normal_buttons_and_preserve_observations(window, tmp_path):
    state = prepare(tmp_path, delay=8)
    bench = window.project_workbench
    wait_project(bench)
    window.navigation_buttons[bench].click()
    bench.open_session(state['directory'])
    wait_project(bench)
    bench._check_ids(IDS)
    bench.timeout_spin.setValue(20)
    bench.tabs.setCurrentIndex(1)
    QTest.mouseClick(bench.run_button, Qt.MouseButton.LeftButton)
    try:
        deadline = time.monotonic() + 10
        while IDS[1] not in bench.run_current_test.text() and time.monotonic() < deadline:
            QApplication.processEvents()
            QTest.qWait(10)
        assert bench.task is not None
        assert IDS[1] in bench.run_current_test.text()
        assert '已完成 1/3' in bench.run_progress_label.text() and '测试主体' in bench.run_progress_label.text()
        assert bench.progress_bar.maximum() == 6 and bench.progress_bar.value() == 1
        assert not bench.result_metrics.isVisible()
        window.grab().save(str(tmp_path / 'running.png'))
        QTest.mouseClick(bench.cancel_button, Qt.MouseButton.LeftButton)
        wait_project(bench)
    finally:
        if bench.task is not None:
            bench.task.control.cancel()
            wait_project(bench)
    assert bench.report['status'] == 'cancelled'
    assert bench.report['before']['tests'][IDS[0]]['observed_status'] == 'passed'
    assert '不计为正式通过' in bench.run_progress_label.text()
    assert bench.progress_bar.value() == 1 and bench.progress_bar.maximum() == 6
    bench.results_tree.setCurrentItem(bench._report_items[IDS[0]])
    assert '中断前观察：通过' in bench.result_details.toPlainText()
    assert bench.result_next_button.text() == '查看中断位置与日志'
    bench.results_scroll.ensureWidgetVisible(bench.results_tree)
    bench.result_detail_scroll.ensureWidgetVisible(bench.result_next_button)
    QTest.mouseClick(bench.result_next_button, Qt.MouseButton.LeftButton)
    assert bench.details_tabs.currentWidget() is bench.version_logs
    window.grab().save(str(tmp_path / 'cancelled.png'))
    window.resize(960, 680)
    QApplication.processEvents()
    window.grab().save(str(tmp_path / 'compact-cancelled.png'))


def test_import_failure_highlights_environment_and_routes_to_existing_dialog(window, tmp_path, monkeypatch):
    bench = window.project_workbench
    wait_project(bench)
    window.navigation_buttons[bench].click()
    row, report = observation()
    row['status'] = row['after']['status'] = 'inconclusive'
    error = {'type': 'ModuleNotFoundError', 'message': 'No module named demo_dependency'}
    row['after']['exception'] = error
    row['after']['phases'][0].update(outcome='failed', exception=error)
    bench.state = {'project_id': 'project'}
    show_report(bench, tmp_path, row, report)
    bench.tabs.setCurrentIndex(2)
    assert bench.result_next_button.text() == '检查项目环境'
    assert bench.result_next_button.isEnabled()
    assert not bench.repair_case_button.isVisible()
    bench.results_scroll.ensureWidgetVisible(bench.results_tree)
    bench.result_detail_scroll.ensureWidgetVisible(bench.result_next_button)
    calls = []
    monkeypatch.setattr(bench, 'inspect_result_environment', lambda: calls.append('environment'))
    QTest.mouseClick(bench.result_next_button, Qt.MouseButton.LeftButton)
    assert calls == ['environment']
    window.grab().save(str(tmp_path / 'environment-next-step.png'))
