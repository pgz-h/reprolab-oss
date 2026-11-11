import json
from pathlib import Path
import time

import pytest
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from test_desktop_ui import window, wait_idle
from test_runner import make_case
from test_batch import saved_case
from reprolab.desktop_batch import BatchDialog


def wait_dialog(dialog, seconds=20):
    deadline = time.monotonic() + seconds
    while dialog.task and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert dialog.task is None


def test_gui_preflight_blocks_invalid_test_without_running(window, tmp_path):
    folder = tmp_path / "invalid"; folder.mkdir()
    path = make_case(folder, test="def broken(:\n    pass\n")
    window.add_case_path(path)
    wait_idle(window)
    window.run_button.click()
    wait_idle(window)
    assert window.last_result.get("tests_executed") is False, "GUI did not stop at preflight"
    assert window.last_result["preflight"]["status"] == "attention"
    assert not list(Path(window.store.settings["reports"]).glob("*/report.json"))
    assert not window.report_button.isEnabled()
    assert "预检" in window.guidance_label.text()


def test_gui_explains_real_missing_module_and_next_action(window, tmp_path):
    folder = tmp_path / "missing"; folder.mkdir()
    path = make_case(folder, test="import no_such_reprolab_teaching_module\ndef test_one(): pass\n")
    window.add_case_path(path)
    wait_idle(window)
    window.run_button.click()
    wait_idle(window)
    assert hasattr(window, "guidance_label"), "actionable diagnosis UI missing"
    assert "无法导入" in window.before_label.text()
    assert "Python" in window.guidance_label.text()
    assert window.last_result["status"] == "inconclusive"


def test_gui_compares_current_environment_with_previous_report(window):
    window.run_button.click()
    wait_idle(window)
    assert "environment" in window.last_result
    report = Path(window.last_result["report_dir"]) / "report.json"
    data = json.loads(report.read_text())
    data["environment"]["distributions"]["previous-only-package"] = "1.0"
    report.write_text(json.dumps(data))
    window.run_button.click()
    wait_idle(window)
    assert window.last_result["status"] == "verified"
    assert window.last_result["environment_comparison"]["status"] == "changed"
    assert "环境" in window.guidance_label.text()


def test_retry_ui_runs_only_nonverified_and_preserves_old_summary(window, tmp_path):
    a = saved_case(tmp_path / "cases", "good")
    b = saved_case(tmp_path / "cases", "broken", after='raise ValueError("the reported bug")')
    original = BatchDialog(window.store, [a, b], parent=window)
    original.show()
    original.start_button.click()
    wait_dialog(original)
    assert hasattr(original, "retry_button"), "contextual retry actions missing"
    source = Path(original.result_data["summary_dir"]) / "summary.json"
    raw = source.read_bytes()
    first_report = window.store.activity[str(a)]["report"]
    (tmp_path / "cases/fixtures/broken/after/sample.py").write_text("def compute(): return 42\n")
    state = {}
    timer = QTimer()
    def drive():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, BatchDialog) and dialog is not original:
            if "started" not in state:
                state["started"] = True
                state["paths"] = dialog.paths
                dialog.start_button.click()
            elif dialog.task is None:
                state["summary"] = dialog.result_data
                dialog.close()
                timer.stop()
    timer.timeout.connect(drive)
    timer.start(15)
    original.retry_button.click()
    timer.stop()
    assert state["paths"] == [str(b)]
    assert state["summary"]["status"] == "verified"
    assert state["summary"]["retry_origin"]["source_summary"] == str(source)
    assert source.read_bytes() == raw
    assert window.store.activity[str(a)]["report"] == first_report
    original.close()
    window.refresh()
    wait_idle(window)
    for i in range(window.report_tree.topLevelItemCount()):
        item = window.report_tree.topLevelItem(i)
        if item.data(0, Qt.ItemDataRole.UserRole)["directory"] == str(source.parent):
            window.report_tree.setCurrentItem(item)
            break
    assert window.retry_failed_button.isEnabled()
    assert not window.resume_batch_button.isEnabled()
