"""First-use shortcuts retain frozen inputs, deliberate execution and local preferences."""
import json
from pathlib import Path
import sys
import time

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from test_desktop_ui import window
from test_desktop_usability import project, wait_project
from test_workflow import make_environment
from reprolab.desktop_store import DesktopStore


def ready(window, repo):
    bench = window.project_workbench
    wait_project(bench)
    window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo))
    QApplication.processEvents()
    return bench


def test_prepare_and_discover_stops_for_scope_review_then_prioritizes_failures(window, tmp_path):
    repo = project(tmp_path)
    marker = tmp_path / 'test-body-ran'
    path = repo / 'tests/test_component.py'
    path.write_text(path.read_text() + f'\ndef test_marker():\n    from pathlib import Path\n    Path({str(marker)!r}).touch()\n')
    bench = ready(window, repo)
    QTest.mouseClick(bench.prepare_discover_button, Qt.MouseButton.LeftButton)
    wait_project(bench)
    assert bench.state is not None and bench.collection['status'] == 'collected'
    assert bench.tabs.currentIndex() == 1 and len(bench.selected_test_ids()) == 6
    assert not marker.exists() and bench.report is None
    assert not list(Path(window.store.settings['reports']).glob('*/report.json'))
    assert bench.run_button.isEnabled()
    selected = bench.selected_test_ids()
    QTest.mouseClick(bench.run_button, Qt.MouseButton.LeftButton)
    wait_project(bench)
    assert marker.exists() and bench.report['selected_tests'] == selected
    first = bench.results_tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)
    assert first['status'] == 'regression_detected'
    assert '需处理项优先' in bench.result_count_label.text()
    saved = json.loads((Path(bench.report['report_dir']) / 'report.json').read_text())
    assert [row['test_id'] for row in saved['entries']] == selected
    assert len(bench._report_items) == len(selected)
    bench.new_project()
    assert bench.tabs.currentIndex() == 0 and bench.session_combo.currentData() is None
    assert bench.result_count_label.text() == '尚无检查结果'


def test_discovery_failure_exposes_recovery_and_keeps_the_snapshot(window, tmp_path):
    repo = project(tmp_path, missing_dependency=True)
    bench = ready(window, repo)
    bench.prepare_discover_button.click(); wait_project(bench)
    assert bench.collection['status'] == 'collection_failed'
    assert bench.problem_environment_button.isVisible() and bench.problem_retry_button.isVisible()
    assert not bench.run_button.isEnabled()
    frozen = bench.state['directory']; identity = bench.state['source_sha256'].copy()
    executable, purelib = make_environment(tmp_path, True)
    (purelib / 'reprolab_missing_demo_dependency.py').write_text('AVAILABLE = True\n')
    bench.python_edit.setText(str(executable))
    bench.discover_button.click(); wait_project(bench)
    assert bench.state['directory'] == frozen and bench.state['source_sha256'] == identity
    assert bench.collection['status'] == 'collected' and bench.report is None
    assert bench.problem_panel.isHidden()


def test_cancelled_collection_keeps_prepared_inputs_and_never_runs_tests(window, tmp_path):
    repo = project(tmp_path)
    marker = tmp_path / 'collect-started'
    (repo / 'conftest.py').write_text(f'import time\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\ntime.sleep(30)\n')
    bench = ready(window, repo)
    bench.prepare_discover_button.click()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        QApplication.processEvents(); QTest.qWait(10)
    assert marker.exists()
    bench.cancel_button.click(); wait_project(bench)
    assert bench.state is not None and Path(bench.state['directory']).is_dir()
    assert bench.collection is None and bench.report is None
    assert bench.discover_button.isEnabled() and '取消' in bench.status_label.text()


def test_error_after_freezing_is_visible_and_recoverable(window, tmp_path, monkeypatch):
    import reprolab.desktop_project as desktop
    repo = project(tmp_path); bench = ready(window, repo)
    def failed(*args, **kwargs):
        raise ValueError('controlled collection failure')
    monkeypatch.setattr(desktop, 'collect_project', failed)
    bench.prepare_discover_button.click(); wait_project(bench)
    assert bench.state and Path(bench.state['directory']).exists()
    assert 'controlled collection failure' in bench.problem_details.toPlainText()
    assert bench.problem_retry_button.isVisible() and bench.problem_retry_button.isEnabled()
    assert bench.report is None


def test_project_options_restore_only_after_explicit_choice_and_survive_restart(window, tmp_path):
    repo = project(tmp_path); bench = ready(window, repo)
    executable, _ = make_environment(tmp_path, True)
    bench.python_edit.setText(str(executable)); bench.timeout_spin.setValue(47)
    bench.prepare_discover_button.click(); wait_project(bench)
    assert window.store.project_defaults(repo) == {'python': str(executable), 'timeout': 47.0}
    reopened = DesktopStore(window.store.directory)
    assert reopened.project_defaults(repo) == window.store.project_defaults(repo)
    bench.new_project(); bench.repository_edit.setText(str(repo))
    initial = bench.python_edit.text(); bench.read_commits(); wait_project(bench)
    assert bench.python_edit.text() == initial and bench.timeout_spin.value() == 120
    assert bench.saved_options_button.isVisible()
    bench.saved_options_button.click()
    assert bench.python_edit.text() == str(executable) and bench.timeout_spin.value() == 47
    assert bench.state is None and bench.task is None
    bench.new_project(); bench.repository_edit.setText(str(tmp_path / 'different-project'))
    assert not bench.saved_options_button.isVisible()


def test_compact_window_keeps_primary_action_and_interpreter_readable(window, tmp_path):
    bench = ready(window, project(tmp_path))
    window.resize(960, 680); QApplication.processEvents()
    button = bench.prepare_discover_button
    assert button.isVisible() and button.width() >= button.sizeHint().width()
    assert bench.python_combo.width() > bench.python_button.width()
    assert bench.python_combo.width() >= 180
