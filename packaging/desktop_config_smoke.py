"""Packaged acceptance of reversible original-project configuration and refresh."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from reprolab import __version__
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.project_sessions import check_project_report
from reprolab.pytest_profile import read_pytest_profile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    root=parser.parse_args().directory.absolute()
    root.mkdir(parents=True,exist_ok=False)
    app=QApplication(sys.argv[:1])
    result={'version':__version__,'frozen':bool(getattr(sys,'frozen',False)),
            'model_requests_sent':False,'dependency_installation_performed':False,'checks':[]}
    window=None

    def wait(predicate, seconds=30):
        deadline=time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline:
            app.processEvents(); QTest.qWait(10); time.sleep(.003)
        assert predicate(),'桌面操作未按时结束'
        app.processEvents()

    def review(dialog,button,*,accept=True,screenshot=None):
        errors=[]
        def respond():
            preview=dialog.configuration_preview
            try:
                assert preview and 'pyproject.toml' in preview.diff_view.toPlainText()
                if screenshot: preview.grab().save(str(root/screenshot))
                if accept: preview.apply_button.click()
                else: preview.reject()
            except BaseException as exc:
                errors.append(str(exc))
                if preview: preview.reject()
        QTimer.singleShot(0,respond)
        button.click()
        assert not errors,errors

    try:
        repo=root/'project';repo.mkdir()
        original=b'# Preserve this project comment\n[project]\nname="plugin-demo"\nversion="0.1"\ndependencies=[]\n[tool.pytest.ini_options]\naddopts="-k no_test_should_match --maxfail=1"\n'
        (repo/'pyproject.toml').write_bytes(original)
        (repo/'support_plugin.py').write_text('import pytest\n@pytest.fixture\ndef expected(): return 42\n')
        (repo/'test_feature.py').write_text('def test_answer(expected):\n    assert expected == 42\n')
        def git(*parts):
            subprocess.run(['git','-C',str(repo),'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
                '-c','user.name=ReproLab QA','-c','user.email=qa@example.invalid',*parts],check=True,capture_output=True)
        git('init','-q');git('add','.');git('commit','-qm','baseline')
        store=DesktopStore(root/'state')
        store.settings.update(cases=str(root/'cases'),reports=str(root/'reports'),drafts=str(root/'drafts'),
                              imports=str(root/'imports'),python='',model_config='')
        window=MainWindow(store);window.resize(1280,840);window.show()
        bench=window.project_workbench
        wait(lambda: window.task is None and not bench.busy)
        window.navigation_buttons[bench].click()
        bench.repository_edit.setText(str(repo));bench.before_combo.setEditText('HEAD')
        bench.workspace_check.setChecked(True);bench.title_edit.setText('项目插件接入与可撤销配置')
        bench.prepare_button.click();wait(lambda:not bench.busy)
        assert bench.state,bench.status_label.text()
        bench.discover_button.click();wait(lambda:not bench.busy)
        bench.run_button.click();wait(lambda:not bench.busy)
        assert bench.report['status']=='attention'
        state=dict(bench.state)
        old_report=bench.report['report_dir']
        old_hash=hashlib.sha256((Path(old_report)/'report.json').read_bytes()).hexdigest()
        snapshot=Path(state['directory'])/'inputs/after/pyproject.toml'
        window.grab().save(str(root/'before-configuration.png'))
        result['checks'].append('real_missing_fixture_is_blocked')

        def environment(callback):
            errors=[]
            def operate():
                dialog=bench.environment_dialog
                try:
                    wait(lambda: dialog.inspection is not None and dialog.task is None)
                    callback(dialog)
                except BaseException:
                    errors.append(traceback.format_exc())
                finally:
                    if dialog.isVisible():dialog.reject()
            QTimer.singleShot(0,operate)
            bench.inspect_environment()
            assert not errors,'\n'.join(errors)

        def add(dialog):
            dialog.tabs.setCurrentIndex(1)
            dialog.plugin_name.setText('support_plugin');dialog.add_plugin_button.click()
            assert 'support_plugin' in dialog.plugin_preview.toPlainText()

        def first(dialog):
            assert '-k no_test_should_match' in dialog.execution_view.toPlainText()
            add(dialog)
            review(dialog,dialog.edit_plugins_button,accept=False)
            assert (repo/'pyproject.toml').read_bytes()==original
            result['checks'].append('cancel_preview_leaves_original_unchanged')
            review(dialog,dialog.edit_plugins_button,screenshot='configuration-preview.png')
            wait(lambda:dialog.task is None)
            assert read_pytest_profile(repo)['plugins']==['support_plugin']
            assert snapshot.read_bytes()==original
            assert b'# Preserve this project comment' in (repo/'pyproject.toml').read_bytes()
            dialog.grab().save(str(root/'configuration-applied.png'))
            dialog.tabs.setCurrentWidget(dialog.execution_view)
            dialog.grab().save(str(root/'execution-differences.png'))
            result['checks'].extend(['reviewed_edit_preserves_comments_and_frozen_input','execution_override_explanation'])
        environment(first)
        assert bench.state['project_id']==state['project_id']

        def second(dialog):
            dialog.tabs.setCurrentIndex(1)
            assert dialog.undo_plugins_button.isEnabled()
            review(dialog,dialog.undo_plugins_button,screenshot='undo-preview.png')
            wait(lambda:dialog.task is None)
            assert (repo/'pyproject.toml').read_bytes()==original
            assert not dialog.refresh_configuration_button.isEnabled()
            result['checks'].append('reopened_dialog_undo_restores_exact_original')
            add(dialog);review(dialog,dialog.edit_plugins_button)
            wait(lambda:dialog.task is None)
            assert dialog.refresh_configuration_button.isEnabled()
            dialog.refresh_configuration_button.click()
        environment(second)
        wait(lambda:bench.state['project_id']!=state['project_id'] and not bench.busy)
        assert bench.state['source_sha256']['before']==state['source_sha256']['before']
        assert bench.collection is None and snapshot.read_bytes()==original
        result['checks'].append('refresh_preserves_baseline_and_waits_for_explicit_execution')
        bench.discover_button.click();wait(lambda:not bench.busy)
        bench.run_button.click();wait(lambda:not bench.busy)
        assert bench.report['status']=='regression_passed' and bench.report['pytest_profile']['plugins']==['support_plugin']
        assert check_project_report(bench.report['report_dir'])['status']=='consistent'
        assert check_project_report(old_report)['status']=='consistent'
        assert hashlib.sha256((Path(old_report)/'report.json').read_bytes()).hexdigest()==old_hash
        result['checks'].extend(['configured_plugin_runs_on_both_versions','original_report_unchanged_and_new_evidence_consistent'])
        window.grab().save(str(root/'configured-project-passed.png'))
        window.resize(960,680);app.processEvents()
        window.grab().save(str(root/'compact-project.png'))
        result.update(status='passed',original_report=old_report,configured_report=bench.report['report_dir'],repository=str(repo))
    except Exception:
        result.update(status='failed',error=traceback.format_exc())
    finally:
        if window:
            bench=window.project_workbench
            if bench.task:
                bench.task.control.cancel();wait(lambda:bench.task is None)
            window.close();app.processEvents()
        (root/'config-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
