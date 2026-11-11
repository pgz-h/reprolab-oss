"""Packaged desktop acceptance for per-test progress and interruption evidence."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from reprolab import __version__
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.project_sessions import check_project_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=False)
    app = QApplication(sys.argv[:1])
    result = {'version': __version__, 'frozen': bool(getattr(sys, 'frozen', False)),
              'model_requests_sent': False, 'checks': []}
    window = None

    def wait(predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
            time.sleep(.005)
        assert predicate(), '桌面操作未在时限内完成'
        app.processEvents()

    try:
        repo = root / 'project'
        repo.mkdir()
        (repo / 'settings.py').write_text('PAUSE=8\nMISSING=False\nVALUE=42\n')
        (repo / 'test_progress.py').write_text('''import time
from settings import PAUSE, MISSING, VALUE
def test_first(): assert True
def test_slow(): time.sleep(PAUSE)
def test_import():
    if MISSING: import reprolab_progress_demo_missing
def test_regression(): assert VALUE == 42
''')
        def git(*parts):
            subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null',
                '-c', 'commit.gpgsign=false', '-c', 'user.name=ReproLab QA',
                '-c', 'user.email=qa@example.invalid', *parts], check=True, capture_output=True)
        git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
        (repo / 'settings.py').write_text('PAUSE=0\nMISSING=True\nVALUE=0\n')
        store = DesktopStore(root / 'state')
        store.settings.update(cases=str(root / 'cases'), reports=str(root / 'reports'),
                              drafts=str(root / 'drafts'), imports=str(root / 'imports'), python='', model_config='')
        window = MainWindow(store)
        window.resize(1280, 840)
        window.show()
        bench = window.project_workbench
        wait(lambda: window.task is None and not bench.busy)
        window.navigation_buttons[bench].click()
        bench.repository_edit.setText(str(repo))
        bench.before_combo.setEditText('HEAD')
        bench.workspace_check.setChecked(True)
        bench.title_edit.setText('逐项进度与中断恢复示例')
        bench.prepare_button.click()
        wait(lambda: not bench.busy)
        assert bench.state, bench.status_label.text()
        bench.discover_button.click()
        wait(lambda: not bench.busy)
        assert bench.collection['status'] == 'collected'
        bench._check_ids(bench.collection['tests'])
        bench.run_button.click()
        wait(lambda: 'test_slow' in bench.run_current_test.text() and '测试主体' in bench.run_progress_label.text())
        assert '已完成 1/4' in bench.run_progress_label.text()
        assert bench.progress_bar.maximum() == 8 and bench.progress_bar.value() == 1
        assert not bench.result_metrics.isVisible()
        window.grab().save(str(root / 'running.png'))
        bench.cancel_button.click()
        wait(lambda: not bench.busy)
        cancelled = bench.report
        assert cancelled['status'] == 'cancelled'
        first = cancelled['before']['tests']['test_progress.py::test_first']
        assert first['observed_status'] == 'passed' and first['status'] == 'inconclusive'
        assert check_project_report(cancelled['report_dir'])['status'] == 'consistent'
        assert '不计为正式通过' in bench.run_progress_label.text()
        bench.results_tree.setCurrentItem(bench._report_items['test_progress.py::test_first'])
        bench.results_scroll.ensureWidgetVisible(bench.results_tree)
        bench.result_detail_scroll.ensureWidgetVisible(bench.result_next_button)
        assert bench.result_next_button.text() == '查看中断位置与日志'
        bench.result_next_button.click()
        assert bench.details_tabs.currentWidget() is bench.version_logs
        window.grab().save(str(root / 'cancelled.png'))
        result['checks'].extend(['real_worker_live_phase_and_counts', 'cancel_retains_observations_without_passing',
                                 'interrupted_evidence_verifies', 'interruption_action_opens_logs'])
        result['cancelled_report'] = cancelled['report_dir']
        result['session'] = bench.state['directory']

        chosen = [identity for identity in bench.collection['tests'] if 'test_slow' not in identity]
        bench._check_ids(chosen)
        bench.tabs.setCurrentIndex(1)
        bench.run_button.click()
        wait(lambda: not bench.busy)
        assert bench.report['counts'] == {'regression_passed': 1, 'regression_detected': 2}
        assert all(bench.report[side]['progress']['completed'] == 3 for side in ('before', 'after'))
        assert check_project_report(bench.report['report_dir'])['status'] == 'consistent'
        bench.results_tree.setCurrentItem(bench._report_items['test_progress.py::test_import'])
        bench.results_scroll.ensureWidgetVisible(bench.results_tree)
        bench.result_detail_scroll.ensureWidgetVisible(bench.result_next_button)
        assert bench.result_next_button.text() == '检查项目环境' and bench.result_next_button.isEnabled()
        window.grab().save(str(root / 'environment-action.png'))
        closed = []
        timer = QTimer()
        def close_environment():
            dialog = bench.environment_dialog
            if dialog is not None and dialog.isVisible() and dialog.task is None:
                dialog.grab().save(str(root / 'environment-dialog.png'))
                closed.append(True)
                timer.stop()
                dialog.reject()
        timer.timeout.connect(close_environment)
        timer.start(100)
        bench.result_next_button.click()
        timer.stop()
        assert closed, '环境操作没有打开可用窗口'
        bench.results_tree.setCurrentItem(bench._report_items['test_progress.py::test_regression'])
        bench.results_scroll.ensureWidgetVisible(bench.results_tree)
        bench.result_detail_scroll.ensureWidgetVisible(bench.result_next_button)
        assert bench.result_next_button.text() == '准备修复说明'
        bench.result_next_button.click()
        wait(lambda: bench.failure_dialog is not None and not bench.task)
        brief = bench.failure_dialog
        brief.problem.setPlainText('变更后返回值从 42 变为 0')
        brief.expected.setPlainText('保持返回 42')
        brief.generate_button.click()
        assert 'test_regression' in brief.preview.toPlainText()
        brief.grab().save(str(root / 'repair-brief.png'))
        brief.copy_button.click()
        brief.reject()
        app.processEvents()
        window.resize(960, 680)
        app.processEvents()
        bench.results_scroll.ensureWidgetVisible(bench.results_tree)
        window.grab().save(str(root / 'compact-results.png'))
        result['checks'].extend(['completed_run_has_matching_progress_evidence', 'import_action_opens_environment',
                                 'failure_action_opens_editable_repair_brief', 'compact_results_remain_scrollable'])
        result['completed_report'] = bench.report['report_dir']
        result['status'] = 'passed'
    except Exception:
        result.update(status='failed', error=traceback.format_exc())
    finally:
        if window is not None:
            bench = window.project_workbench
            if bench.task:
                bench.task.control.cancel()
                wait(lambda: not bench.task)
            window.close()
            app.processEvents()
        (root / 'progress-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
