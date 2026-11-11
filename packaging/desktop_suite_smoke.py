"""Actual Qt + local API mock + real workers for the AI acceptance workflow."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QPushButton
from reprolab.desktop import MainWindow
from reprolab.desktop_models import ModelConnectionDialog
from reprolab.desktop_environment import ProjectEnvironmentDialog
from reprolab.desktop_suite import AISuiteDialog, ExplanationDialog
from reprolab.desktop_suite_review import SuiteReviewDialog
from reprolab.desktop_store import DesktopStore
from reprolab.retry import plan_retry


def _plan():
    result = {'title': '目标修复与相关功能验收', 'overview': '确认compute修复并保护legacy', 'checks': []}
    for identity, role, code, expected in (
        ('repair', 'repair', 'from sample import compute\ndef test_fix(): assert compute() == 42\n', {'type':'ValueError','message_contains':'unconfirmed AI assumption'}),
        ('guard', 'regression', 'from sample import legacy\ndef test_legacy(): assert legacy() == 1\n', None),
    ):
        result['checks'].append({'id':identity,'title':identity,'role':role,'test_code':code,'test_target':None,
            'expected_failure':expected,'purpose':'检查真实项目行为','expected_behavior':'满足断言','assumptions':['需要审阅输入和检查目的']})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--python', default='')
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=False)
    app = QApplication(sys.argv[:1])
    output = {'frozen':bool(getattr(sys,'frozen',False)), 'checks':[]}
    requests = []
    windows = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            assert self.path == '/v1/models'
            assert self.headers.get('Authorization') == 'Bearer teaching-public-key-not-a-real-credential'
            requests.append(self.path)
            payload = {'models': {'small-model': {'name': '轻量模型'},
                                  'teaching-model': {'id': 'canonical-name', 'name': '教学验收模型'}}}
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert data['model'] == 'teaching-model'
            requests.append(self.path)
            text = json.dumps(data, ensure_ascii=False)
            if 'public connection test' not in text:
                assert data.get('reasoning') == {'effort': 'vendor-high'}
                assert data['max_output_tokens'] == 384000
            if 'public connection test' in text:
                assert 'reasoning' not in data
                content = {'ok':True}
            elif 'Explain only the supplied' in text:
                content = {'based_on_status':'attention','summary':'目标修复通过，相关功能出现新增失败。',
                           'findings':[{'id':'2','comment':'legacy在旧版通过，新版失败。'}],
                           'next_steps':['查看legacy的修改并处理新增失败。']}
            else:
                content = _plan()
            payload = {'status':'completed','model':'teaching-model','output':[{'type':'message','role':'assistant','status':'completed',
                       'content':[{'type':'output_text','text':json.dumps(content,ensure_ascii=False)}]}],
                       'usage':{'input_tokens':10,'output_tokens':10}}
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    second_server = None

    def wait(predicate, seconds=60):
        deadline=time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5);time.sleep(.005)
        assert predicate(),'UI operation timed out'
        app.processEvents()

    def consent_preview():
        dialog=QApplication.activeModalWidget()
        assert isinstance(dialog,QDialog) and dialog.windowTitle()=='发送前预览'
        from PySide6.QtWidgets import QLabel
        assert any('high' in widget.text() for widget in dialog.findChildren(QLabel))
        dialog.grab().save(str(root/'reasoning-preview.png'))
        checkbox=next(widget for widget in dialog.findChildren(QCheckBox) if '同意发送' in widget.text())
        checkbox.setChecked(True)
        next(widget for widget in dialog.findChildren(QPushButton) if widget.text()=='发送').click()

    try:
        repo=root/'project';repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
        git('init','-q')
        (repo/'sample.py').write_text('def compute(): raise ValueError("teaching bug")\ndef legacy(): return 1\n')
        (repo/'pyproject.toml').write_text('[project]\nname="reprolab-teaching-project"\nversion="0.0.0"\nrequires-python=">=3.11"\ndependencies=["pytest>=8,<10"]\n')
        git('add','.')
        git('-c','user.name=ReproLab QA','-c','user.email=qa@example.invalid','commit','-qm','before')
        (repo/'sample.py').write_text('def compute(): return 42\ndef legacy(): return 0\n')
        original_status=git('status','--porcelain')
        store=DesktopStore(root/'state')
        store.settings.update(cases=str(root/'cases'),drafts=str(root/'drafts'),reports=str(root/'reports'),
                              imports=str(root/'imports'),python=args.python,model_config='')
        window=MainWindow(store);windows.append(window);window.show();wait(lambda:window.task is None)
        connection=ModelConnectionDialog(output_dir=root/'models',parent=window);windows.append(connection);connection.show()
        connection.protocol_combo.setCurrentIndex(connection.protocol_combo.findData('responses'))
        connection.base_url_edit.setText(f'http://127.0.0.1:{server.server_port}/v1')
        connection.display_name_edit.setText('本地教学网关')
        connection.api_key_edit.setText('teaching-public-key-not-a-real-credential')
        connection.fetch_button.click();wait(lambda:connection.task is None)
        assert connection.catalog_picker is not None
        connection.catalog_picker.select_all_button.click()
        connection.catalog_picker.grab().save(str(root/'model-multi-select.png'))
        connection.catalog_picker.apply_button.click();app.processEvents()
        assert connection.model_combo.count() == 2, connection.status_label.text()
        connection.model_combo.setCurrentIndex(1)
        assert connection._connection_data()['model'] == 'teaching-model'
        for card in connection.cards.cards:
            card.reasoning_check.setChecked(True)
        card=connection.cards.cards[1]
        card.expand_button.setChecked(True)
        card.context_edit.setText('1M');card.output_edit.setText('384K');card.image_check.setChecked(True)
        for _ in range(3):card.add_level_button.click()
        high=next(row for row in card.rows if row.level.currentData()=='high')
        high.wire.setText('vendor-high')
        app.processEvents();QTest.qWait(50);app.processEvents()
        assert all(row.wire.height() >= 34 and row.level.height() >= 34 for row in card.rows)
        card.grab().save(str(root/'model-card.png'))
        output['checks'].append('model_cards_edit_capacities_inputs_and_seven_wire_mappings')
        connection.model_combo.showPopup();app.processEvents()
        connection.model_combo.view().grab().save(str(root/'model-list.png'))
        connection.model_combo.hidePopup()
        output['checks'].append('custom_provider_fetches_catalog_and_selects_gateway_alias_without_typing_model')
        connection.test_button.click();wait(lambda:connection.task is None)
        assert '连接成功' in connection.status_label.text(),connection.status_label.text()
        connection.grab().save(str(root/'model-connection.png'))
        connection.save_button.click();config=connection.result_data['config']
        if os.name == 'nt':
            import win32security
            security = win32security.GetNamedSecurityInfo(str(config), win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION)
            assert security.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
            assert security.GetSecurityDescriptorDacl().GetAceCount() == 2
        else:
            assert Path(config).stat().st_mode & 0o777 == 0o600
        reopened_connection = ModelConnectionDialog(config, root/'models', window)
        windows.append(reopened_connection)
        assert reopened_connection.model_combo.count() == 2
        assert reopened_connection._connection_data()['model'] == 'teaching-model'
        assert reopened_connection.cards.cards[1].value()['reasoning_levels']['high']=='vendor-high'
        assert reopened_connection.cards.cards[1].value()['max_tokens']==384000
        assert reopened_connection.display_name_edit.text() == '本地教学网关'
        second_server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=second_server.serve_forever,daemon=True).start()
        reopened_connection.base_url_edit.setText(f'http://127.0.0.1:{second_server.server_port}/v1')
        before_requests = len(requests)
        reopened_connection.fetch_button.click()
        assert reopened_connection.task is None and len(requests) == before_requests
        assert '确认' in reopened_connection.status_label.text()
        assert not reopened_connection.reuse_key_check.isHidden()
        reopened_connection.reuse_key_check.setChecked(True)
        reopened_connection.fetch_button.click();wait(lambda:reopened_connection.task is None)
        assert reopened_connection.catalog_picker is not None
        reopened_connection.catalog_picker.select_all_button.click()
        reopened_connection.catalog_picker.apply_button.click();app.processEvents()
        assert len(requests) == before_requests + 1 and reopened_connection.model_combo.count() == 2
        output['checks'].append('changed_provider_requires_explicit_consent_before_reusing_saved_key')
        reopened_connection.close()
        output['checks'].append('saved_service_and_selected_catalog_model_reopen')
        store.settings['model_config']=config;store.save()
        window.path_edits['model_config'].setText(config)
        selector=window.model_selector
        selector.combo.setCurrentIndex(0);selector.combo.activated.emit(0)
        selector.reasoning_editor.effort_combo.setCurrentIndex(selector.reasoning_editor.effort_combo.findData('low'))
        selector.combo.setCurrentIndex(1);selector.combo.activated.emit(1)
        selector.reasoning_editor.effort_combo.setCurrentIndex(selector.reasoning_editor.effort_combo.findData('high'))
        assert selector.reasoning_editor.effort_combo.currentData()=='high'
        config=window.path_edits['model_config'].text()
        output['checks'].append('runtime_selector_uses_declared_levels_and_saves_per_model_choice')
        output['checks'].append('form_url_key_model_responses_connection_test_and_private_profile')
        environment=ProjectEnvironmentDialog(repo,args.python,root/'environments',window);windows.append(environment);environment.show()
        wait(lambda:environment.task is None and environment.inspection is not None)
        assert environment.inspection['status']=='ready',environment.inspection
        environment.use_button.click()
        output['checks'].append('project_declared_dependencies_checked')
        suite=AISuiteDialog(store,window);windows.append(suite);suite.show()
        assert suite.model_selector.reasoning_editor.effort_combo.currentData() == 'high'
        suite.repository_edit.setText(str(repo));suite.refresh_button.click()
        wait(lambda:suite.task is None and suite.pending is None)
        suite.issue_edit.setPlainText('修复compute返回42，同时保护legacy的旧行为。')
        suite.save_button.click();wait(lambda:suite.task is None)
        assert suite.directory and suite.state['status']=='prepared',suite.status_label.text()
        before_requests=len(requests)
        QTimer.singleShot(20,consent_preview)
        suite.save_button.click();wait(lambda:suite.task is None)
        assert len(requests)==before_requests+1
        assert suite.state['status']=='planned' and suite.check_tree.topLevelItemCount()==2,suite.status_label.text()
        suite.scroll_area.ensureWidgetVisible(suite.check_tree)
        suite.grab().save(str(root/'ai-plan.png'))
        output['checks'].append('previewed_model_call_generates_reviewable_repair_and_regression_checks')
        original_plan_directory=suite.directory
        original_plan_bytes=Path(original_plan_directory,'plan.json').read_bytes()
        before_review_requests=len(requests)
        suite.probe_button.click();wait(lambda:suite.task is None)
        assert suite.probe_data['status']=='failure_observed',suite.status_label.text()
        assert suite.probe_data['suggestion']=={'type':'ValueError','message_contains':'teaching bug'}
        probe_directory=suite.probe_data['probe_dir']
        assert not suite.use_probe_button.isEnabled()
        suite.scroll_area.ensureWidgetVisible(suite.probe_output)
        suite.grab().save(str(root/'old-version-observation.png'))
        suite.probe_confirm.setChecked(True)
        assert suite.use_probe_button.isEnabled()
        output['checks'].append('frozen_plan_old_version_probe_observes_real_failure_before_human_confirmation')
        def edit_check():
            editor=QApplication.activeModalWidget()
            assert isinstance(editor,SuiteReviewDialog)
            assert editor.failure_type_edit.text()=='ValueError'
            assert editor.failure_message_edit.text()=='teaching bug'
            editor.check_title_edit.setText('人工核对 compute 的修复')
            editor.grab().save(str(root/'plan-review-editor.png'))
            watchdog=QTimer(editor);watchdog.setSingleShot(True)
            watchdog.timeout.connect(editor.reject);editor.finished.connect(watchdog.stop)
            watchdog.start(30000)
            editor.save_button.click()
        QTimer.singleShot(20,edit_check)
        suite.use_probe_button.click()
        assert suite.directory!=original_plan_directory,suite.status_label.text()
        assert suite.state['revision']['parent_plan_sha256']
        assert Path(original_plan_directory,'plan.json').read_bytes()==original_plan_bytes
        assert len(requests)==before_review_requests
        assert 'teaching bug' in suite.review.toPlainText()
        output['checks'].append('offline_human_review_creates_independent_plan_and_keeps_original')
        batch_done={}
        timer=QTimer()
        def close_completed_batch():
            batch=suite.last_batch
            if batch and batch.task is None and batch.result_data:
                batch_done['result']=batch.result_data
                batch.grab().save(str(root/'project-results.png'))
                timer.stop();batch.close_button.click()
        timer.timeout.connect(close_completed_batch);timer.start(20)
        suite.save_button.click();timer.stop()
        result=batch_done['result']
        assert result['status']=='attention' and result['counts']['verified']==1 and result['counts']['regression_detected']==1
        assert plan_retry(result['summary_dir'])['paths']==[result['entries'][1]['file']]
        assert git('status','--porcelain')==original_status
        output['checks'].append('real_runs_find_new_regression_and_retry_selects_only_failed_check')
        summary=Path(result['summary_dir'])/'summary.json';original=summary.read_bytes()
        explain=ExplanationDialog(result['summary_dir'],config,window);windows.append(explain);explain.show()
        QTimer.singleShot(20,consent_preview)
        explain.generate_button.click();wait(lambda:explain.task is None)
        assert '新增失败' in explain.text.toPlainText(),explain.text.toPlainText()
        assert summary.read_bytes()==original
        explain.grab().save(str(root/'ai-explanation.png'));explain.close()
        output['checks'].append('previewed_ai_explanation_preserves_actual_verdict')
        saved_directory=suite.directory;suite.close()
        reopened=AISuiteDialog(store,window);windows.append(reopened);reopened.load_plan(saved_directory)
        assert reopened.state['status']=='planned' and reopened.check_tree.topLevelItemCount()==2
        reopened.close()
        original_view=AISuiteDialog(store,window);windows.append(original_view)
        original_view.load_plan(original_plan_directory)
        original_view.open_probe(probe_directory)
        assert original_view.probe_data['status']=='failure_observed'
        assert not original_view.use_probe_button.isEnabled()
        original_view.close()
        output['checks'].append('saved_old_version_observation_reopens_against_original_plan')
        # Simulate a killed publisher after response and partial artifacts exist.
        interrupted=root/'interrupted-plan'
        shutil.copytree(original_plan_directory,interrupted)
        record=json.loads((interrupted/'suite.json').read_text())
        record['status']='generation_failed'
        (interrupted/'suite.json').write_text(json.dumps(record,ensure_ascii=False))
        recovered=AISuiteDialog(store,window);windows.append(recovered);recovered.load_plan(interrupted)
        before_recovery_requests=len(requests)
        assert recovered.recover_button.isEnabled()
        recovered.recover_button.click();wait(lambda:recovered.task is None)
        assert recovered.state['status']=='planned',recovered.status_label.text()
        assert Path(interrupted,recovered.state['recovery_artifacts'],'plan.json').read_bytes()==original_plan_bytes
        assert len(requests)==before_recovery_requests
        recovered.close()
        output['checks'].append('interrupted_plan_response_recovers_offline_and_preserves_partial_artifacts')
        window.refresh();wait(lambda:window.task is None)
        assert any(window.report_tree.topLevelItem(i).data(0,256).get('kind')=='suite' for i in range(window.report_tree.topLevelItemCount()))
        output['checks'].append('saved_plan_and_project_summary_reopen')
        window.close();output.update(ok=True,summary_dir=result['summary_dir'],suite_dir=saved_directory,api_requests=len(requests))
    except Exception:
        output.update(ok=False,error=traceback.format_exc())
        for widget in windows:
            task=getattr(widget,'task',None)
            if task:
                task.control.cancel();task.wait(20000)
    finally:
        server.shutdown();server.server_close()
        if second_server is not None:
            second_server.shutdown();second_server.server_close()
    (root/'suite-result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(output,ensure_ascii=False))
    return 0 if output['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
