"""Explicit local acceptance against a caller-selected trusted pytest repository."""
import argparse,json,sys,time
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from reprolab import __version__
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.project_sessions import check_project_report
from reprolab.evidence import check_report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--repository',required=True,type=Path)
    parser.add_argument('--python',required=True)
    parser.add_argument('--plugin',action='append',default=[])
    parser.add_argument('--count',required=True,type=int)
    parser.add_argument('--case',required=True,type=Path)
    args=parser.parse_args();root=args.directory.resolve();root.mkdir(parents=True,exist_ok=False)
    app=QApplication.instance() or QApplication([])
    store=DesktopStore(root/'state');store.settings.update(cases=str(root/'cases'),reports=str(root/'reports'),drafts=str(root/'drafts'),imports=str(root/'imports'),model_config='',python='')
    w=MainWindow(store);w.show();w.resize(1280,850);w.raise_();w.activateWindow();b=w.project_workbench
    result={'version':__version__,'frozen':bool(getattr(sys,'frozen',False)),'platform':app.platformName(),'checks':[],'timings':{}}
    def wait(predicate=None):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            app.processEvents();QTest.qWait(10);time.sleep(.005)
            if predicate() if predicate else not b.busy and w.task is None:return
        raise AssertionError(b.status_label.text())
    def shot(widget,name):app.processEvents();assert widget.grab().save(str(root/name))
    try:
        wait();w.open_project_workbench();b.repository_edit.setText(str(args.repository));b.python_edit.setText(args.python)
        start=time.monotonic();b.prepare_discover_button.click();wait();result['timings']['prepare_and_failed_collection']=time.monotonic()-start
        assert b.collection['status']=='collection_failed' and b.problem_environment_button.isVisible()
        original=dict(b.state);shot(w,'01-plugin-needed.png');errors=[]
        def configure():
            d=b.environment_dialog
            try:
                wait(lambda:d.task is None and d.inspection is not None)
                d.tabs.setCurrentIndex(1)
                for plugin in args.plugin:d.plugin_name.setText(plugin);d.add_plugin_button.click()
                def accept_config():
                    try:
                        assert d.configuration_preview is not None
                        shot(d.configuration_preview,'02-config-review.png');d.configuration_preview.apply_button.click()
                    except BaseException as exc:
                        errors.append(str(exc))
                        if d.configuration_preview:d.configuration_preview.reject()
                QTimer.singleShot(0,accept_config);d.edit_plugins_button.click();wait(lambda:d.task is None)
                assert d.refresh_configuration_button.isEnabled();d.refresh_configuration_button.click()
            except BaseException as exc:errors.append(str(exc));d.reject()
        start=time.monotonic();QTimer.singleShot(0,configure);b.problem_environment_button.click();assert not errors,errors
        wait(lambda:b.state is not None and b.state['project_id']!=original['project_id'] and not b.busy)
        assert b.state['source_sha256']['before']==original['source_sha256']['before'] and b.collection is None
        b.discover_button.click();wait();assert b.collection['status']=='collected'
        assert len(b.collection['tests'])==args.count and len(b.selected_test_ids())==args.count and b.report is None
        result['timings']['configure_refresh_collect_automated_controls']=time.monotonic()-start
        shot(w,'03-scope.png');start=time.monotonic();b.run_button.click();wait()
        result['timings']['two_version_run']=time.monotonic()-start
        assert b.report['counts']=={'regression_passed':args.count}
        assert check_project_report(b.report['report_dir'])['status']=='consistent'
        original_report=b.report['report_dir'];original_bytes=(Path(original_report)/'report.json').read_bytes();shot(w,'04-project-passed.png')
        start=time.monotonic();b.quick_recheck_all_action.trigger();wait(lambda:not b.busy and b.review_progress is not None)
        result['timings']['recheck_and_compare']=time.monotonic()-start
        assert b.report['counts']=={'regression_passed':args.count}
        assert b.review_progress['status']=='compared' and b.review_progress['comparison']['same_check_resolved']==0
        assert (Path(original_report)/'report.json').read_bytes()==original_bytes
        shot(w,'05-recheck.png')
        w.add_case_path(args.case);wait();w.python_edit.setText(json.loads(args.case.read_text())['python']);w.run_button.click();wait()
        assert w.last_result['status']=='verified' and check_report(w.last_result['report_dir'])['status']=='consistent'
        shot(w,'06-history-verified.png')
        result.update(status='passed',project_report=original_report,recheck_report=b.report['report_dir'],strict_report=w.last_result['report_dir'],checks=['real_project_collection_blocked_without_plugins','reviewed_explicit_plugins','refresh_retains_baseline','full_scope_review_before_run','actual_two_version_run','repeat_recheck_keeps_evidence','real_historical_strict_verification'])
    except Exception as exc:result.update(status='failed',error=str(exc));shot(w,'failure.png')
    (root/'adaptation-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    if b.environment_dialog and b.environment_dialog.isVisible():b.environment_dialog.reject()
    w.close();app.processEvents()
    return 0 if result['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
