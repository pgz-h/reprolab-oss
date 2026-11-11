"""Two-process draft interruption, strict repair and recheck acceptance; no models."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from reprolab import __version__
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.desktop_failure import RepairBriefDialog, RepairDraftPicker, ReportRepairDialog
from reprolab.project_sessions import check_project_report
from reprolab.evidence import check_report
from reprolab.repair_drafts import draft_path, identity


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--phase',choices=('interrupt','resume'),required=True)
    args=parser.parse_args(); root=args.directory.resolve()
    root.mkdir(parents=True,exist_ok=args.phase=='resume')
    app=QApplication.instance() or QApplication([])
    store=DesktopStore(root/'state')
    store.settings.update(cases=str(root/'cases'),reports=str(root/'reports'),drafts=str(root/'drafts'),
                          imports=str(root/'imports'),python='',model_config='')
    store.save()
    window=MainWindow(store); window.show(); window.resize(1280,850)
    window.raise_(); window.activateWindow(); bench=window.project_workbench
    result={'version':__version__,'frozen':bool(getattr(sys,'frozen',False)),
            'platform':app.platformName(),'phase':args.phase,'checks':[],'model_requests':False}
    def wait(predicate=None):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            app.processEvents(); QTest.qWait(10); time.sleep(.005)
            if predicate() if predicate else not bench.busy and window.task is None:
                return
        raise AssertionError(bench.status_label.text())
    def shot(widget,name):
        app.processEvents(); assert widget.grab().save(str(root/name))
    def select_failure():
        bench.results_tree.setCurrentItem(bench._report_items['tests/test_cart.py::test_money_display'])
    def open_brief():
        select_failure(); bench.prepare_repair_brief()
        wait(lambda:isinstance(bench.failure_dialog,RepairBriefDialog))
        return bench.failure_dialog
    try:
        wait()
        if args.phase=='interrupt':
            window.home_page.project_demo_button.click(); wait()
            bench.discover_button.click(); wait(); bench.run_button.click(); wait()
            assert bench.report['counts']['regression_detected']==1
            note=open_brief(); note.problem.setPlainText('金额显示缺少末尾零'); note.expected.setPlainText('固定两位小数')
            note.preview.setPlainText(note.preview.toPlainText()+'\n手工保留：不改变税率逻辑。')
            manual=note.preview.toPlainText(); note.expected.setPlainText('待进一步核对的期望')
            store.set_runtime_values(bench.state['repository'],{'PRIVATE_TOKEN':'synthetic-local-value-not-for-storage'})
            wait(lambda:note._draft_record is not None and not note._draft_timer.isActive())
            assert not note.copy_button.isEnabled()
            assert 'synthetic-local-value-not-for-storage' not in Path(note._draft_record['_path']).read_text()
            shot(note,'01-before-interruption.png')
            (root/'interruption.json').write_text(json.dumps({'session':bench.state['directory'],
                'report':bench.report['report_dir'],'manual':manual,'draft':note._draft_record['_path']},ensure_ascii=False,indent=2))
            result.update(status='passed',checks=['real_failure','autosaved_conflicting_manual_draft','runtime_value_excluded','abrupt_process_exit'])
            (root/'interrupt-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            sys.stdout.flush(); os._exit(0)  # Explicit QA interruption: no close handlers or final save.
        saved=json.loads((root/'interruption.json').read_text())
        bench.open_session(saved['session']); wait(); window.tabs.setCurrentWidget(bench)
        original=bench.report; before=(Path(original['report_dir'])/'report.json').read_bytes()
        note=open_brief()
        assert note.preview.toPlainText()==saved['manual'] and not note.copy_button.isEnabled()
        assert note.expected.toPlainText()=='待进一步核对的期望'
        shot(note,'02-restored-conflict.png'); note.close(); app.processEvents()
        # Missing source is view-only via the recovery list, even with no usable report.
        source=Path(original['report_dir']); moved=source.with_name(source.name+'-temporarily-moved'); source.rename(moved)
        errors=[]
        def choose():
            picker=app.activeModalWidget()
            try:
                assert isinstance(picker,RepairDraftPicker)
                picker.tree.setCurrentItem(picker.tree.topLevelItem(0)); picker.restore_button.click()
            except BaseException as exc:
                errors.append(str(exc)); picker.reject()
        QTimer.singleShot(0,choose); bench.reopen_repair_brief()
        assert not errors,errors
        wait(lambda:isinstance(bench.failure_dialog,RepairBriefDialog))
        unavailable=bench.failure_dialog
        assert not unavailable.source_available and unavailable.preview.toPlainText()==saved['manual']
        assert not unavailable.copy_button.isEnabled() and unavailable.preview.isReadOnly()
        unavailable.close(); app.processEvents(); moved.rename(source)
        note=open_brief(); note.expected.setPlainText('固定两位小数')
        assert note.copy_button.isEnabled() and note.preview.toPlainText()==saved['manual']
        note.copy_button.click(); assert QApplication.clipboard().text()==saved['manual']
        def confirm_discard():
            box=app.activeModalWidget(); assert isinstance(box,QMessageBox)
            next(button for button in box.buttons() if button.text()=='放弃草稿').click()
        QTimer.singleShot(0,confirm_discard); note.discard_draft_button.click(); app.processEvents()
        assert not Path(saved['draft']).exists()
        note=open_brief(); assert note.expected.toPlainText()==''
        note.problem.setPlainText('金额显示缺少末尾零'); note.expected.setPlainText('固定两位小数'); note.close(); app.processEvents()
        repo=Path(bench.state['repository']); code=repo/'cart.py'
        code.write_text(code.read_text().replace('return f"{value:.1f}"','return f"{value:.2f}"'))
        select_failure(); bench.repair_case_button.click(); strict=bench.failure_dialog
        assert isinstance(strict,ReportRepairDialog)
        strict.prepare_button.click(); wait(lambda:strict.task is None)
        assert strict.expected.toPlainText()=='固定两位小数' and strict.problem.toPlainText()=='金额显示缺少末尾零'
        assert strict.message.toPlainText()=='12.00' and not strict.confirm.isChecked()
        assert not strict.save_button.isEnabled(); shot(strict,'03-prefilled-unconfirmed.png')
        strict.confirm.setChecked(True); case=root/'cases/money-repair.json'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(case),'')):
            strict.save_button.click()
        wait(lambda:strict.task is None); assert strict.saved_case==str(case)
        strict.go_button.click(); wait(); window.run_button.click(); wait()
        assert window.last_result['status']=='verified'
        assert check_report(window.last_result['report_dir'])['status']=='consistent'
        shot(window,'04-strict-verified.png')
        window.tabs.setCurrentWidget(bench); bench.quick_recheck_all_action.trigger()
        wait(lambda:not bench.busy and bench.review_progress is not None)
        assert bench.review_progress['comparison']['same_check_resolved']==1
        assert bench.report['counts']['baseline_failure']==1 and bench.report['counts']['inconclusive']==1
        assert bench.report['status']=='attention' and check_project_report(bench.report['report_dir'])['status']=='consistent'
        assert (source/'report.json').read_bytes()==before
        shot(window,'05-project-rechecked.png')
        result.update(status='passed',checks=['cross_process_recovery','manual_conflict_preserved','missing_source_readonly',
            'explicit_discard','same_failure_prefill_unconfirmed','strict_real_execution','project_recheck','old_evidence_unchanged'],
            strict_report=window.last_result['report_dir'],project_report=bench.report['report_dir'],counts=bench.report['counts'])
    except Exception as exc:
        result.update(status='failed',error=str(exc)); shot(window,'failure.png')
    (root/(args.phase+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    if bench.failure_dialog:
        bench.failure_dialog.reject()
    window.close(); app.processEvents()
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
