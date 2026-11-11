"""Repair brief editing must never deliver stale inputs or discard unfinished forms."""
from pathlib import Path
import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog
from reprolab.desktop_failure import RepairBriefDialog
from reprolab.runner import RunControl
from test_desktop_ui import window
from test_project_failure import material


def ready(window, tmp_path):
    data = material()
    data['blocks']['log:after']['text'] = 'LOG-AFTER-ONLY: saved diagnostic evidence'
    dialog = RepairBriefDialog(data, tmp_path, window)
    dialog.show()
    dialog.problem.setPlainText('金额格式缺零')
    dialog.expected.setPlainText('固定两位小数')
    dialog.generate()
    return dialog


def test_form_changes_update_unedited_preview_without_another_generate(window, tmp_path):
    dialog = ready(window, tmp_path)
    dialog.expected.setPlainText('金额始终显示两位小数，保持原断言')
    dialog.copy_button.click()
    assert '期望行为：金额始终显示两位小数，保持原断言' in QApplication.clipboard().text()
    assert QApplication.clipboard().text() == dialog.preview.toPlainText()
    dialog.close()


def test_complete_form_generates_preview_and_can_be_copied_directly(window, tmp_path):
    dialog = RepairBriefDialog(material(), tmp_path, window)
    dialog.problem.setPlainText('金额丢失末尾零')
    dialog.expected.setPlainText('显示两位小数')
    assert '期望行为：显示两位小数' in dialog.preview.toPlainText()
    assert dialog.copy_button.isEnabled()
    dialog.copy(); dialog.close()


def test_deselecting_log_removes_it_before_copy_or_export(window, tmp_path, monkeypatch):
    dialog = ready(window, tmp_path)
    item = next(dialog.blocks.topLevelItem(i) for i in range(dialog.blocks.topLevelItemCount())
                if dialog.blocks.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == 'log:after')
    item.setCheckState(0, Qt.CheckState.Checked); dialog.generate()
    assert 'LOG-AFTER-ONLY' in dialog.preview.toPlainText()
    item.setCheckState(0, Qt.CheckState.Unchecked)
    dialog.copy_button.click()
    assert 'LOG-AFTER-ONLY' not in QApplication.clipboard().text()
    target = tmp_path / 'brief.txt'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a, **k: (str(target), ''))
    monkeypatch.setattr(dialog, '_run', lambda action, callback, message: callback(action(RunControl())))
    dialog.export()
    assert target.read_text() == dialog.preview.toPlainText() and 'LOG-AFTER-ONLY' not in target.read_text()
    dialog.close()


def test_manual_preview_is_preserved_and_stale_delivery_is_blocked(window, tmp_path, monkeypatch):
    dialog = ready(window, tmp_path)
    edited = dialog.preview.toPlainText() + '\n手工补充，需要保留'
    dialog.preview.setPlainText(edited); dialog.copy()
    dialog.expected.setPlainText('更新后的期望行为')
    assert dialog.preview.toPlainText() == edited
    assert not dialog.copy_button.isEnabled() and not dialog.export_button.isEnabled()
    QApplication.clipboard().setText('keep')
    dialog.copy()
    assert QApplication.clipboard().text() == 'keep'
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *a: False)
    dialog.generate()
    assert dialog.preview.toPlainText() == edited and not dialog.copy_button.isEnabled()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *a: True)
    dialog.generate()
    assert '更新后的期望行为' in dialog.preview.toPlainText()
    assert dialog.copy_button.isEnabled()
    dialog.copy(); dialog.close()


def test_reverting_form_restores_manual_preview_delivery_without_overwriting(window, tmp_path):
    dialog = ready(window, tmp_path)
    edited = dialog.preview.toPlainText() + '\n手动说明'
    dialog.preview.setPlainText(edited)
    original = dialog.expected.toPlainText()
    dialog.expected.setPlainText('临时输入')
    assert not dialog.copy_button.isEnabled()
    dialog.expected.setPlainText(original)
    assert dialog.preview.toPlainText() == edited and dialog.copy_button.isEnabled()
    dialog.copy(); dialog.close()


def test_unfinished_form_close_and_shared_then_changed_form_are_protected(window, tmp_path, monkeypatch):
    dialog = RepairBriefDialog(material(), tmp_path, window); dialog.show()
    dialog.problem.setPlainText('尚未填写完整的任务')
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *a: False)
    dialog.close(); QApplication.processEvents()
    assert dialog.isVisible() and dialog.problem.toPlainText() == '尚未填写完整的任务'
    dialog.expected.setPlainText('完整期望'); dialog.generate(); dialog.copy()
    dialog.expected.clear()
    assert not dialog.copy_button.isEnabled()
    dialog.close(); QApplication.processEvents()
    assert dialog.isVisible()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *a: True)
    dialog.close()


def test_no_changes_and_shared_unchanged_preview_close_without_prompt(window, tmp_path, monkeypatch):
    dialog = RepairBriefDialog(material(), tmp_path, window)
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *a: pytest.fail('unchanged content should not prompt'))
    dialog.show(); dialog.close(); QApplication.processEvents()
    assert not dialog.isVisible()
    dialog = ready(window, tmp_path); dialog.copy()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *a: pytest.fail('shared content should not prompt'))
    dialog.close(); QApplication.processEvents()
    assert not dialog.isVisible()


def test_export_rechecks_state_after_the_file_picker(window, tmp_path, monkeypatch):
    dialog = ready(window, tmp_path)
    dialog.preview.setPlainText(dialog.preview.toPlainText() + '\n手动内容')
    target = tmp_path / 'stale.txt'
    def chosen(*args, **kwargs):
        dialog.expected.setPlainText('文件选择期间改变的期望')
        return str(target), ''
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', chosen)
    dialog.export()
    assert not target.exists() and dialog.task is None
    assert not dialog.export_button.isEnabled()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *args: True)
    dialog.close()


def test_render_input_snapshot_survives_a_nested_confirmation(window, tmp_path, monkeypatch):
    dialog = ready(window, tmp_path)
    dialog.preview.setPlainText(dialog.preview.toPlainText() + '\n编辑内容')
    dialog.expected.setPlainText('准备生成的期望')
    def confirm(*args):
        dialog.expected.setPlainText('确认期间改变的期望')
        return True
    monkeypatch.setattr(dialog, '_confirm_discard', confirm)
    dialog.generate()
    assert '期望行为：准备生成的期望' in dialog.preview.toPlainText()
    assert dialog.expected.toPlainText() == '确认期间改变的期望'
    assert not dialog.copy_button.isEnabled()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *args: True)
    dialog.close()


def test_clearing_shared_form_is_still_an_unshared_edit(window, tmp_path, monkeypatch):
    dialog = ready(window, tmp_path); dialog.copy()
    dialog.problem.clear(); dialog.expected.clear()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *args: False)
    dialog.close(); QApplication.processEvents()
    assert dialog.isVisible() and not dialog.copy_button.isEnabled()
    monkeypatch.setattr(dialog, '_confirm_discard', lambda *args: True)
    dialog.close()
