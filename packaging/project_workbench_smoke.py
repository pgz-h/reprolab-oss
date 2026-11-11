"""One concentrated real workflow for the major project workbench feature."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.project_sessions import (check_project_report, pack_project_report,
                                      unpack_project_report, replay_project_report, prepare_project_ai)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--session', type=Path, help='已有教学会话；只执行少量目标以核对打包分派')
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=False)
    app = QApplication(sys.argv[:1])
    output = {'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    window = None

    def wait(predicate, seconds=60):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
            time.sleep(.005)
        assert predicate(), '项目操作超时'
        app.processEvents()

    try:
        store = DesktopStore(root / 'state')
        store.settings.update(cases=str(root/'cases'), reports=str(root/'reports'),
                              drafts=str(root/'drafts'), imports=str(root/'imports'), python='', model_config='')
        window = MainWindow(store)
        window.show()
        bench = window.project_workbench
        wait(lambda: window.task is None and not bench.busy)
        assert window.tabs.currentWidget() is window.home_page
        window.grab().save(str(root/'start.png'))
        window.home_page.project_button.click()
        assert window.tabs.currentWidget() is bench
        window.resize(960, 680)
        app.processEvents()
        for widget in (bench.repository_edit, bench.repository_button, bench.python_combo):
            assert widget.height() >= widget.minimumSizeHint().height()
        window.grab().save(str(root/'compact-project.png'))
        window.resize(1160, 800)
        output['checks'].append('task_start_page_and_readable_small_window')
        if args.session:
            bench.open_session(args.session)
            wait(lambda: not bench.busy)
            assert bench.state, bench.status_label.text()
            bench.python_edit.clear()
            selected = ['tests/test_component.py::test_bulk[0]', 'tests/test_component.py::test_fixed']
        else:
            repo = root / 'project'
            repo.mkdir()
            def git(*parts):
                return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-C',str(repo),*parts],text=True).strip()
            git('init','-q')
            before = 'def value(kind):\n    return {"normal":42,"regress":42,"baseline":0,"fixed":0}[kind]\n'
            after = 'def value(kind):\n    return {"normal":42,"regress":0,"baseline":0,"fixed":42}[kind]\n'
            (repo/'component.py').write_text(before)
            (repo/'tests').mkdir()
            (repo/'tests/test_component.py').write_text('''import pytest
from component import value
@pytest.mark.parametrize('index', range(256))
def test_bulk(index): assert value('normal') == 42
def test_regression(): assert value('regress') == 42
def test_baseline(): assert value('baseline') == 42
def test_fixed(): assert value('fixed') == 42
def test_skip(): pytest.skip('teaching unavailable case')
@pytest.fixture
def broken(): raise RuntimeError('teaching fixture setup')
def test_setup(broken): assert value('normal') == 42
@pytest.fixture
def cleanup():
    yield
    raise RuntimeError('teaching fixture cleanup')
def test_cleanup(cleanup): assert value('normal') == 42
@pytest.mark.xfail
def test_xfail(): assert False, 'teaching expected failure'
''')
            git('add','.')
            git('-c','user.name=ReproLab QA','-c','user.email=qa@example.invalid','commit','-qm','baseline')
            baseline = git('rev-parse','HEAD')
            (repo/'component.py').write_text(after)
            bench.repository_edit.setText(str(repo))
            bench.before_combo.setEditText(baseline)
            bench.workspace_check.setChecked(True)
            bench.title_edit.setText('项目工作台 · 263 项真实回归演示')
            bench.issue_edit.setPlainText('修复 fixed 行为，并检查 normal 与 regress 的项目回归。')
            bench.prepare()
            wait(lambda: not bench.busy)
            assert bench.state, bench.status_label.text()
            (repo/'component.py').write_text('def value(kind): return 999\n')
            output['checks'].append('frozen_worktree_is_independent_of_later_repository_edits')
            selected = None
        bench.discover()
        wait(lambda: not bench.busy)
        assert bench.collection and bench.collection['status']=='collected', bench.status_label.text()
        bench.tabs.setCurrentIndex(1)
        bench.grab().save(str(root/'test-selection.png'))
        window.grab().save(str(root/'test-selection-full.png'))
        output['checks'].append('actual_parametrized_collection_and_multi_select')
        bench.run_selected(selected)
        wait(lambda: not bench.busy, 90)
        report = bench.report
        assert report, bench.status_label.text()
        if args.session:
            assert report['status']=='regression_passed', report['counts']
            assert len(report['entries'])==2
        else:
            assert len(report['entries'])==263, len(report['entries'])
            assert report['counts']=={'regression_passed':256,'regression_detected':1,
                                     'baseline_failure':1,'improved':1,'inconclusive':4}, report['counts']
            assert report['status']=='regression_detected'
            assert (repo/'component.py').read_text()=='def value(kind): return 999\n'
        assert report['before']['process_id'] != report['after']['process_id']
        assert check_project_report(report['report_dir'])['status']=='consistent'
        if not args.session:
            assert bench.results_tree.currentItem().data(0,256)['status']=='regression_detected'
            bench.attention_button.click()
            visible = sum(not bench.results_tree.topLevelItem(i).isHidden()
                          for i in range(bench.results_tree.topLevelItemCount()))
            assert visible==6
            output['checks'].append('focus_new_failure_and_filter_all_attention_items')
        output['checks'].append('one_pytest_process_per_version_and_per_test_result_matrix')
        window.refresh()
        wait(lambda: window.task is None)
        assert any(window.report_tree.topLevelItem(i).data(0,256).get('kind')=='project_regression'
                   for i in range(window.report_tree.topLevelItemCount()))
        output['checks'].append('project_reports_integrate_with_main_report_catalog')
        bench.tabs.setCurrentIndex(2)
        bench.grab().save(str(root/'project-results.png'))
        window.grab().save(str(root/'project-results-full.png'))
        if not args.session:
            bundle = root/'project-evidence.zip'
            pack_project_report(report['report_dir'], bundle)
            imported = unpack_project_report(bundle, root/'imported')
            assert check_project_report(imported['report_dir'])['status']=='consistent'
            preview = replay_project_report(imported['report_dir'],root/'replay',confirmed=False)
            assert preview['count']==263 and not (root/'replay').exists()
            replay = replay_project_report(imported['report_dir'],root/'replay',confirmed=True,
                                           python_executable=sys.executable)
            assert replay['counts']==report['counts']
            assert check_project_report(imported['report_dir'])['status']=='consistent'
            output['checks'].append('zip_export_import_and_explicit_replay_preserve_original_evidence')
            ai = prepare_project_ai(bench.state['directory'],bench.state['issue'],root/'ai-plans',python_executable=sys.executable)
            assert ai['source_sha256']==bench.state['source_sha256']
            assert 'return 999' not in (Path(ai['directory'])/'inputs/after/component.py').read_text()
            output['checks'].append('ai_plan_uses_same_frozen_inputs_without_model_request')
        session = bench.state['directory']
        bench.open_session(session)
        wait(lambda: not bench.busy)
        assert bench.state['directory']==session
        output['checks'].append('saved_session_and_run_history_reopen')
        output.update(ok=True,session_dir=session,report_dir=report['report_dir'],selected=len(report['entries']),
                      counts=report['counts'],runtime=report['runtime'],
                      before_seconds=report['before']['duration_seconds'],after_seconds=report['after']['duration_seconds'])
        window.close()
        app.processEvents()
    except Exception:
        output.update(ok=False,error=traceback.format_exc())
        if window:
            window.project_workbench.request_close()
            task = window.project_workbench.task
            if task:
                task.wait(15000)
    (root/'workbench-result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(output,ensure_ascii=False))
    return 0 if output.get('ok') else 2


if __name__=='__main__':
    raise SystemExit(main())
