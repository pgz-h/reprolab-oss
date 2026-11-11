"""Normal desktop discovery keeps incomplete scope visible and recoverable."""
from pathlib import Path
import pytest
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from test_desktop_ui import window
from test_desktop_usability import wait_project
from test_collection_recovery import project


def test_partial_scope_requires_choice_and_recovery_keeps_snapshot(window, tmp_path):
    repo = project(tmp_path, content="[pytest]\n")
    bench = window.project_workbench
    wait_project(bench); window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo)); bench.prepare_discover_button.click(); wait_project(bench)
    assert bench.collection["partial"]
    assert bench.problem_panel.isVisible() and bench.problem_environment_button.isVisible()
    assert bench.problem_retry_button.isVisible() and bench.problem_refresh_button.isVisible()
    assert "不完整" in bench.status_label.text()
    assert bench.selected_test_ids() == [] and not bench.run_button.isEnabled()
    bench.select_visible_button.click()
    assert bench.run_button.isEnabled() and len(bench.selected_test_ids()) == 2
    bench.run_button.click(); wait_project(bench)
    assert bench.report["collection"]["partial"] and len(bench.report["selected_tests"]) == 2
    old = bench.state["directory"]
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    bench.tabs.setCurrentIndex(1); QApplication.processEvents()
    assert bench.problem_refresh_button.isVisible()
    bench.problem_refresh_button.click(); wait_project(bench)
    assert bench.state["directory"] != old and Path(old).is_dir()
    assert bench.collection["status"] == "collected" and not bench.collection["partial"]
    assert bench.problem_panel.isHidden() and bench.report is None
    assert len(bench.selected_test_ids()) == 2


def test_timeout_offers_adjustment_and_retries_same_snapshot(window, tmp_path):
    repo = project(tmp_path)
    (repo / "conftest.py").write_text("import time; time.sleep(1)\n")
    bench = window.project_workbench
    wait_project(bench); window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo)); bench.timeout_spin.setValue(.1)
    bench.prepare_discover_button.click(); wait_project(bench)
    original = bench.state["directory"]
    assert "超时" in bench.problem_details.toPlainText()
    assert bench.problem_environment_button.isHidden()
    bench.timeout_spin.setValue(10); bench.problem_retry_button.click(); wait_project(bench)
    assert bench.state["directory"] == original and bench.collection["status"] == "collected"
    assert bench.problem_panel.isHidden() and bench.report is None


def test_cancel_refresh_discovery_preserves_new_snapshot_and_selection(window, tmp_path):
    import time
    from PySide6.QtTest import QTest
    repo = project(tmp_path, content='[pytest]\n')
    bench = window.project_workbench
    wait_project(bench); window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo)); bench.prepare_discover_button.click(); wait_project(bench)
    bench.select_visible_button.click()
    original = bench.state['directory']
    marker = tmp_path / 'collecting'
    (repo / 'conftest.py').write_text(f'from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(30)\n')
    bench.problem_refresh_button.click()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        QApplication.processEvents(); QTest.qWait(10)
    assert marker.exists()
    bench.cancel_button.click(); wait_project(bench)
    assert bench.state['directory'] != original and Path(original).is_dir()
    assert bench.collection is None and bench.report is None
    assert len(bench.state['selection_request']['selected_tests']) == 2
    assert bench.discover_button.isEnabled() and '取消' in bench.status_label.text()


def test_workbench_scopes_collection_before_freezing_and_gates_a_scope_edit(window, tmp_path):
    from reprolab.project_sessions import read_project
    repo = project(tmp_path, content="[pytest]\n")
    bench = window.project_workbench
    wait_project(bench); window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo))
    bench.collection_path_edit.setText('tests')
    bench.prepare_discover_button.click(); wait_project(bench)
    assert bench.collection['collection_path'] == 'tests' and not bench.collection['partial']
    assert len(bench.selected_test_ids()) == 2 and bench.run_button.isEnabled()

    bench.collection_path_edit.setText('tests/test_real.py')
    assert not bench.run_button.isEnabled()
    assert not bench.run_selected()
    assert '重新发现' in bench.status_label.text()
    bench.discover_button.click(); wait_project(bench)
    assert bench.collection['collection_path'] == 'tests/test_real.py'
    assert bench.run_button.isEnabled()
    saved = read_project(bench.state['directory'])
    bench._apply_state(saved)
    assert bench.collection_path_edit.text() == 'tests/test_real.py'
    assert len(bench.selected_test_ids()) == 2
