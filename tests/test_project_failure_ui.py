"""Focused widget acceptance authored for later authorized execution."""
import json
from pathlib import Path

import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QFileDialog

from reprolab.desktop_failure import RepairBriefDialog, ReportRepairDialog
from reprolab.runner import RunControl
from test_desktop_ui import window
from test_desktop_usability import wait_project
from test_project_failure import ID, material, observation, prepared, prepare


def show_report(bench, tmp_path, row, version):
    folder = tmp_path / 'report'
    folder.mkdir(exist_ok=True)
    data = {'kind': 'project_regression', 'schema_version': 1, 'project_id': 'project', 'run_id': 'run',
            'report_dir': str(folder), 'status': version['status'], 'after': version['after'],
            'selected_tests': [ID], 'entries': [row]}
    (folder / 'report.json').write_text(json.dumps(data))
    bench._apply_report(data)


def test_failure_actions_follow_selected_record_and_keep_full_id_copy(window, tmp_path):
    bench = window.project_workbench
    wait_project(bench)
    row, version = observation()
    show_report(bench, tmp_path, row, version)
    assert bench.repair_brief_button.isEnabled() and bench.repair_case_button.isEnabled()
    assert ID in bench.selected_result_summary.toPlainText() and 'AssertionError' in bench.selected_result_summary.toPlainText()
    bench.copy_result_ids()
    assert QApplication.clipboard().text() == ID
    row['after']['phases'][0]['outcome'] = 'failed'
    row['after']['status'] = 'inconclusive'
    row['status'] = 'inconclusive'
    show_report(bench, tmp_path, row, version)
    assert not bench.repair_case_button.isEnabled() and 'fixture' in bench.repair_reason.text()
    assert bench.repair_brief_button.isEnabled()
    bench.results_tree.setCurrentItem(None)
    assert not bench.repair_brief_button.isEnabled() and not bench.repair_case_button.isEnabled()


def test_edited_preview_clipboard_and_export_use_one_string(window, tmp_path, monkeypatch):
    dialog = RepairBriefDialog(material(), tmp_path, window)
    dialog.problem.setPlainText('金额格式缺零')
    dialog.expected.setPlainText('固定两位小数')
    dialog.generate()
    edited = dialog.preview.toPlainText().replace('from cart import display', '[用户省略导入行]')
    dialog.preview.setPlainText(edited)
    dialog.copy()
    assert QApplication.clipboard().text() == edited
    destination = tmp_path / 'edited.txt'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a, **k: (str(destination), ''))
    monkeypatch.setattr(dialog, '_run', lambda action, callback, message: callback(action(RunControl())))
    dialog.export()
    assert destination.read_bytes() == edited.encode('utf-8')
    dialog.preview.setPlainText('x' * (96 * 1024 + 1))
    assert not dialog.copy_button.isEnabled() and not dialog.export_button.isEnabled()
    dialog.preview.setPlainText(edited)
    dialog.reject()


def test_stale_material_for_same_id_in_another_run_is_not_shown(window, tmp_path, monkeypatch):
    bench = window.project_workbench
    wait_project(bench)
    row, version = observation()
    show_report(bench, tmp_path, row, version)
    callbacks = []
    monkeypatch.setattr(bench, '_start', lambda action, callback, *a, **kw: callbacks.append(callback))
    bench.prepare_repair_brief()
    bench._report_reference = {**bench._report_reference, 'run_id': 'different-run'}
    callbacks[0](material())
    assert bench.failure_dialog is None and bench._after_finished is None
    assert '已改变' in bench.status_label.text()



def test_regenerating_brief_preserves_edits_until_confirmed_and_is_undoable(window, tmp_path, monkeypatch):
    dialog = RepairBriefDialog(material(), tmp_path, window)
    dialog.problem.setPlainText('金额显示不完整')
    dialog.expected.setPlainText('固定两位小数')
    dialog.generate()
    edited = dialog.preview.toPlainText() + '\n用户手动补充的重要说明'
    dialog.preview.setPlainText(edited)
    dialog.expected.setPlainText('金额始终显示两位小数')
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *args: False, raising=False)
    dialog.generate()
    assert dialog.preview.toPlainText() == edited
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *args: True, raising=False)
    dialog.generate()
    assert '金额始终显示两位小数' in dialog.preview.toPlainText()
    assert '用户手动补充的重要说明' not in dialog.preview.toPlainText()
    dialog.preview.undo()
    assert dialog.preview.toPlainText() == edited
    dialog.copy()
    dialog.reject()


def test_closing_unshared_brief_can_keep_the_editor_open(window, tmp_path, monkeypatch):
    dialog = RepairBriefDialog(material(), tmp_path, window)
    dialog.preview.setPlainText('仍需保留的手工编辑')
    dialog.show()
    QApplication.processEvents()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *args: False, raising=False)
    dialog.close()
    assert dialog.isVisible()
    assert dialog.preview.toPlainText() == '仍需保留的手工编辑'
    dialog.copy()
    dialog.close()
    assert not dialog.isVisible()


def test_repair_confirmation_requires_valid_fields_and_the_frozen_candidate(window, tmp_path, prepared):
    plan = prepare(prepared, tmp_path)
    dialog = ReportRepairDialog(None, None, None, {'cases': str(tmp_path / 'cases')}, tmp_path, window)
    dialog._show_plan(plan)
    dialog.confirm.setChecked(True)
    assert not dialog.save_button.isEnabled()
    dialog.problem.setPlainText('金额丢失末尾零')
    dialog.expected.setPlainText('固定两位小数')
    dialog.message.setPlainText('12.00')
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled()
    dialog.message.setPlainText('历史异常里没有的片段')
    dialog.confirm.setChecked(True)
    assert not dialog.save_button.isEnabled()
    dialog.message.setPlainText('12.00')
    dialog.confirm.setChecked(True)
    dialog.repository.setText(str(tmp_path / 'another-project'))
    dialog.confirm.setChecked(True)
    assert not dialog.save_button.isEnabled()
    assert '重新冻结' in dialog.review_hint.text()
    dialog.repository.setText(plan['repository'])
    assert not dialog.confirm.isChecked()
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled()
    dialog.reject()



def test_result_to_strict_acceptance_uses_normal_desktop_entries(window, tmp_path, monkeypatch):
    import time
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from reprolab.evidence import check_report
    from test_desktop_ui import wait_idle

    def wait_for(predicate, message):
        deadline = time.monotonic() + 30
        while not predicate() and time.monotonic() < deadline:
            QApplication.processEvents()
            QTest.qWait(10)
            time.sleep(.005)
        assert predicate(), message
        QApplication.processEvents()

    bench = window.project_workbench
    wait_project(bench)
    window.resize(1280, 840)
    window.tabs.setCurrentWidget(window.home_page)
    window.home_page.project_demo_button.click()
    wait_project(bench)
    assert window.navigation_buttons[bench].isChecked()
    bench.discover_button.click()
    wait_project(bench)
    bench.run_button.click()
    wait_project(bench)
    assert bench.report and bench.report['counts']['regression_detected'] == 1
    window.grab().save(str(tmp_path / 'results.png'))
    selected = next(bench.results_tree.topLevelItem(i) for i in range(bench.results_tree.topLevelItemCount())
                    if bench.results_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)['test_id'] == ID)
    bench.results_tree.setCurrentItem(selected)
    bench.result_detail_scroll.ensureWidgetVisible(bench.result_next_button)
    assert bench.result_next_button.text() == "准备修复说明" and bench.result_next_button.isVisible()
    QTest.mouseClick(bench.result_next_button, Qt.MouseButton.LeftButton)
    wait_for(lambda: isinstance(bench.failure_dialog, RepairBriefDialog), '修复说明入口未打开')
    brief = bench.failure_dialog
    brief.problem.setPlainText('金额显示缺少末尾零')
    brief.expected.setPlainText('固定显示两位小数')
    brief.generate_button.click()
    assert 'test_money_display' in brief.preview.toPlainText()
    brief.grab().save(str(tmp_path / 'repair-brief.png'))
    brief.copy_button.click()
    brief.reject()
    QApplication.processEvents()
    repository = Path(bench.state['repository'])
    source = repository / 'cart.py'
    source.write_text(source.read_text().replace('return f"{value:.1f}"', 'return f"{value:.2f}"'))
    bench.result_detail_scroll.ensureWidgetVisible(bench.repair_case_button)
    bench.repair_case_button.click()
    dialog = bench.failure_dialog
    assert isinstance(dialog, ReportRepairDialog)
    dialog.prepare_button.click()
    wait_for(lambda: dialog.task is None, '冻结修复输入未完成')
    assert dialog.plan is not None, dialog.status.toPlainText()
    dialog.problem.setPlainText('金额显示缺少末尾零')
    dialog.expected.setPlainText('固定显示两位小数')
    dialog.message.setPlainText('12.00')
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled(), dialog.review_hint.text()
    dialog.grab().save(str(tmp_path / 'repair-confirmation.png'))
    target = tmp_path / 'saved-cases' / 'money.json'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args, **kwargs: (str(target), ''))
    dialog.save_button.click()
    wait_for(lambda: dialog.task is None, '保存修复案例未完成')
    assert dialog.saved_case == str(target), dialog.status.toPlainText()
    dialog.go_button.click()
    wait_idle(window)
    assert window.tabs.currentWidget() is window.cases_page
    assert window.navigation_buttons[window.cases_page].isChecked()
    window.run_button.click()
    wait_idle(window)
    assert window.last_result['status'] == 'verified'
    assert check_report(window.last_result['report_dir'])['status'] == 'consistent'
    window.grab().save(str(tmp_path / 'verified-case.png'))



def test_navigation_metrics_and_full_test_search_remain_consistent(window, tmp_path):
    bench = window.project_workbench
    wait_project(bench)
    window.navigation_buttons[bench].click()
    assert window.tabs.currentWidget() is bench
    assert window.tabs.tabBar().isHidden()
    row, version = observation()
    show_report(bench, tmp_path, row, version)
    bench.tabs.setCurrentIndex(2)
    assert bench.prepare_button.isHidden()
    assert bench.input_section.isHidden()
    metric, _ = bench.result_metric_buttons['regression_detected']
    metric.click()
    assert bench.result_filter.currentData() == 'regression_detected'
    bench.result_search.setText(ID)
    assert not bench.results_tree.topLevelItem(0).isHidden()
    bench.recheck_scope.setCurrentIndex(bench.recheck_scope.findData('attention'))
    row['status'] = 'regression_passed'
    row['after']['status'] = 'passed'
    version['status'] = 'regression_passed'
    version['after']['exit_code'] = 0
    show_report(bench, tmp_path, row, version)
    assert bench.recheck_button.isEnabled()
    assert bench.recheck_all_action.isEnabled()
    assert not bench.recheck_attention_action.isEnabled()
