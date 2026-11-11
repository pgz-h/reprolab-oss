from pathlib import Path
from copy import deepcopy

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from reprolab.desktop_failure import RepairBriefDialog, ReportRepairDialog
from reprolab.repair_drafts import identity, draft_path, read_draft, recovery_material
from test_desktop_ui import window
from test_project_failure import material, prepared, prepare
from test_project_config_ui import wait


def editor(window,tmp_path,data=None,**kwargs):
    d=RepairBriefDialog(data or material(),tmp_path,window,draft_root=tmp_path/'drafts',**kwargs)
    d.show(); return d


def test_autosave_restore_conflict_discard_and_reopen(window,tmp_path,monkeypatch):
    d=editor(window,tmp_path); d.problem.setPlainText('问题'); d.expected.setPlainText('期望行为')
    d.preview.setPlainText(d.preview.toPlainText()+'\n手工原文')
    original=d.preview.toPlainText(); d.expected.setPlainText('新期望')
    wait(lambda: d._draft_record is not None)
    assert '已保存' in d.draft_status.text(); assert not d.copy_button.isEnabled()
    d.close()
    restored=editor(window,tmp_path)
    assert restored.preview.toPlainText()==original and restored.expected.toPlainText()=='新期望'
    assert not restored.copy_button.isEnabled() and not restored.export_button.isEnabled()
    restored.expected.setPlainText('期望行为'); assert restored.copy_button.isEnabled()
    monkeypatch.setattr(restored,'_confirm_discard',lambda *a:True)
    restored.discard_draft_button.click(); QApplication.processEvents()
    assert not restored.isVisible() and not draft_path(tmp_path/'drafts',identity(material())).exists()
    fresh=editor(window,tmp_path); assert fresh.expected.toPlainText()==''
    fresh.close()


def test_save_failure_visible_and_close_protects_unsaved_work(window,tmp_path,monkeypatch):
    import reprolab.desktop_failure as module
    d=editor(window,tmp_path); d.problem.setPlainText('保留输入')
    real=module.save_draft
    def fail(*a,**kw): raise OSError('controlled full disk')
    monkeypatch.setattr(module,'save_draft',fail)
    assert not d.save_draft(); assert '未保存' in d.draft_status.text()
    monkeypatch.setattr(d,'_confirm_discard',lambda *a:False)
    d.close(); assert d.isVisible()
    monkeypatch.setattr(module,'save_draft',real); d.retry_draft_button.click()
    assert '已保存' in d.draft_status.text(); d.close()
    restored=editor(window,tmp_path); assert restored.problem.toPlainText()=='保留输入'; restored.close()


def test_missing_source_keeps_readonly_content_without_delivery(window,tmp_path):
    d=editor(window,tmp_path); d.problem.setPlainText('问题'); d.expected.setPlainText('行为'); d.save_draft()
    record=d._draft_record; text=d.preview.toPlainText(); d.close()
    unavailable=editor(window,tmp_path,recovery_material(record),draft=record,source_available=False)
    assert unavailable.preview.toPlainText()==text
    assert unavailable.preview.isReadOnly() and not unavailable.copy_button.isEnabled()
    assert not unavailable.export_button.isEnabled() and not unavailable.generate_button.isEnabled()
    assert '来源不可用' in unavailable.draft_status.text(); unavailable.close()


def test_incomplete_restored_form_cannot_deliver_manual_preview(window,tmp_path):
    d=editor(window,tmp_path); d.preview.setPlainText('手工内容'); d.close()
    restored=editor(window,tmp_path)
    assert restored.preview.toPlainText()=='手工内容' and not restored.copy_button.isEnabled()
    restored.close()


def test_strict_prefill_bound_to_same_failure_and_refreeze_keeps_user_fields(window,tmp_path,prepared):
    plan=prepare(prepared,tmp_path)
    binding={'repository':plan['repository'],'test_id':plan['test_target'],'test_sha256':plan['test_sha256'],
             **{k:plan['source_report'][k] for k in ('project_id','run_id','report_sha256')}}
    draft={'identity':binding,'state':{'inputs':['金额缺零','固定两位小数','','',[]]}}
    d=ReportRepairDialog(None,None,None,{'cases':str(tmp_path/'cases')},tmp_path,window,brief_draft=draft)
    d._show_plan(plan)
    assert d.problem.toPlainText()=='金额缺零' and d.expected.toPlainText()=='固定两位小数'
    assert d.message.toPlainText()=='12.00' and not d.confirm.isChecked() and not d.save_button.isEnabled()
    assert plan['source_sha256']['after'][:12] in d.plan_summary.text()
    d.expected.setPlainText('用户修改的期望'); d._show_plan(plan)
    assert d.expected.toPlainText()=='用户修改的期望' and not d.confirm.isChecked()
    d.reject()
    other=deepcopy(draft); other['identity']['run_id']='different'
    d=ReportRepairDialog(None,None,None,{'cases':str(tmp_path/'cases')},tmp_path,window,brief_draft=other)
    d._show_plan(plan); assert d.expected.toPlainText()==''; d.reject()
