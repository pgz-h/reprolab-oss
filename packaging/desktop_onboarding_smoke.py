"""Packaged first-use flow using isolated local fixtures and actual Qt controls."""
import json
from pathlib import Path
import subprocess
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from reprolab import __version__
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.project_sessions import check_project_report


def main():
    root = Path(sys.argv[1]).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    app = QApplication.instance() or QApplication([])
    store = DesktopStore(root / 'state')
    store.settings.update(cases=str(root / 'cases'), drafts=str(root / 'drafts'), reports=str(root / 'reports'),
                          imports=str(root / 'imports'), model_config='', python='')
    window = MainWindow(store)
    window.show()
    bench = window.project_workbench
    def wait():
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            app.processEvents(); QTest.qWait(10)
            if not bench.busy and window.task is None:
                app.processEvents()
                return
        raise TimeoutError(bench.status_label.text())
    def shot(name):
        app.processEvents()
        assert window.grab().save(str(root / name))
    result = {'version': __version__, 'frozen': bool(getattr(sys, 'frozen', False)),
              'model_requests_sent': False, 'checks': []}
    try:
        wait(); shot('00-home.png'); window.open_project_workbench()
        repo = root / 'project'; repo.mkdir()
        marker = root / 'body-ran'
        (repo / 'feature.py').write_text('def value(): return 42\n')
        (repo / 'test_feature.py').write_text(f'from feature import value\ndef test_pass():\n    from pathlib import Path\n    Path({str(marker)!r}).touch()\ndef test_change():\n    assert value() == 42\n')
        def git(*args):
            subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                '-c', 'user.name=ReproLab demo', '-c', 'user.email=demo@example.invalid', *args], check=True, capture_output=True)
        git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
        (repo / 'feature.py').write_text('def value(): return 0\n')
        bench.repository_edit.setText(str(repo)); bench.timeout_spin.setValue(47)
        window.resize(960, 680); app.processEvents()
        assert bench.prepare_discover_button.isVisible() and bench.prepare_discover_button.isEnabled()
        assert bench.python_combo.width() >= 180
        shot('01-compact-start.png')
        result['checks'].append('compact_primary_action_and_environment_visible')
        window.resize(1280, 840); bench.prepare_discover_button.click(); wait()
        assert bench.collection['status'] == 'collected' and len(bench.selected_test_ids()) == 2
        assert not marker.exists() and bench.report is None and bench.tabs.currentIndex() == 1
        shot('02-scope-review.png')
        result['checks'].append('one_action_prepares_and_collects_without_running_test_bodies')
        bench.clear_tests_button.click()
        assert not bench.run_button.isEnabled() and bench.selected_test_ids() == []
        bench.select_visible_button.click()
        result['checks'].append('empty_scope_never_automatically_runs_all_tests')
        original_selection = bench.selected_test_ids()
        bench.run_button.click(); wait()
        report = bench.report
        assert report['status'] == 'regression_detected' and report['selected_tests'] == original_selection and marker.exists()
        assert bench.results_tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)['status'] == 'regression_detected'
        assert check_project_report(report['report_dir'])['status'] == 'consistent'
        shot('03-failure-first.png')
        result['checks'].append('failure_first_display_preserves_complete_report_and_execution_scope')
        bench.new_project()
        assert bench.tabs.currentIndex() == 0 and bench.result_count_label.text() == '尚无检查结果'
        assert bench.session_combo.currentData() is None
        bench.repository_edit.setText(str(repo))
        assert bench.saved_options_button.isVisible() and bench.timeout_spin.value() == 120
        shot('04-project-memory.png')
        bench.saved_options_button.click()
        assert bench.timeout_spin.value() == 47 and bench.task is None and bench.state is None
        assert DesktopStore(root / 'state').project_defaults(repo)['timeout'] == 47
        result['checks'].append('new_project_returns_to_inputs_and_explicit_preferences_survive_restart')
        (repo / 'test_feature.py').write_text('import reprolab_deliberately_missing_dependency\n')
        bench.prepare_discover_button.click(); wait()
        assert bench.collection['status'] == 'collection_failed' and bench.state is not None
        assert bench.problem_environment_button.isVisible() and bench.problem_retry_button.isVisible()
        assert not bench.run_button.isEnabled() and bench.report is None
        shot('05-discovery-recovery.png')
        result['checks'].append('discovery_failure_keeps_snapshot_and_exposes_environment_and_retry')
        result.update(status='passed', report=report['report_dir'])
    except Exception as exc:
        result.update(status='failed', error=str(exc))
        shot('failure.png')
        raise
    finally:
        (root / 'onboarding-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        window.close(); app.processEvents()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
