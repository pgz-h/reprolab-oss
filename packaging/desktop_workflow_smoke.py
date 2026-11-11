"""Exercise v0.11 creation and batch UI using only newly authored local fixtures."""
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

from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.desktop_batch import BatchDialog
from reprolab.desktop_creation import CaseWizard, DraftWizard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    window = None
    result = {"frozen": bool(getattr(sys, "frozen", False)), "checks": []}

    def wait(predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(15)
            # Let the background Python worker acquire the GIL between Qt polls.
            time.sleep(.005)
        assert predicate(), "Timed out waiting for UI"
        app.processEvents()

    def modal(trigger, expected, configure, finish):
        state = {"phase": 0, "error": None, "dialog": None}
        deadline = time.monotonic() + 60
        timer = QTimer()
        def drive():
            try:
                if time.monotonic() >= deadline:
                    raise AssertionError("Modal workflow timed out")
                dialog = QApplication.activeModalWidget()
                if not isinstance(dialog, expected):
                    return
                state["dialog"] = dialog
                if state["phase"] == 0:
                    configure(dialog)
                    state["phase"] = 1
                elif state["phase"] == 1 and dialog.task is None and getattr(dialog, "pending", None) is None:
                    finish(dialog)
                    state["phase"] = 2
                    if expected is not BatchDialog:
                        timer.stop()
                elif state["phase"] == 2 and dialog.task is None:
                    assert dialog.result_data, dialog.status_label.text()
                    dialog.grab().save(str(root / "batch.png"))
                    dialog.close()
                    timer.stop()
            except Exception:
                state["error"] = traceback.format_exc()
                timer.stop()
                if state["dialog"]:
                    state["dialog"].reject()
        timer.timeout.connect(drive)
        timer.start(20)
        trigger()
        timer.stop()
        assert not state["error"], state["error"]
        wait(lambda: window.task is None)
        return state["dialog"]

    try:
        repo = root / "teaching-project"
        repo.mkdir(exist_ok=False)
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
        git("init", "-q")
        (repo / "sample.py").write_text('def compute():\n    raise ValueError("teaching bug")\n')
        (repo / "test_sample.py").write_text("from sample import compute\ndef test_compute():\n    assert compute() == 42\n")
        git("add", ".")
        git("-c", "user.name=ReproLab QA", "-c", "user.email=qa@example.invalid", "commit", "-qm", "Before teaching fix")
        before = git("rev-parse", "HEAD")
        (repo / "sample.py").write_text("def compute():\n    return 42\n")
        git("add", ".")
        git("-c", "user.name=ReproLab QA", "-c", "user.email=qa@example.invalid", "commit", "-qm", "After teaching fix")
        after = git("rev-parse", "HEAD")
        store = DesktopStore(root / "state")
        store.settings.update(cases=str(root / "cases"), drafts=str(root / "drafts"),
                              reports=str(root / "reports"), imports=str(root / "imports"),
                              python="", model_config="")
        window = MainWindow(store)
        window.show()
        wait(lambda: window.task is None)
        def repository(dialog):
            dialog.repository_edit.setText(str(repo))
            dialog.refresh_repository()
        case_path = root / "cases/teaching.json"
        def save_case(dialog):
            assert dialog.selected_commits == (before, after)
            dialog.target_edit.setText("test_sample.py::test_compute")
            dialog.title_edit.setText("新建案例向导 · 教学验收")
            dialog.failure_type_edit.setText("ValueError")
            dialog.failure_message_edit.setText("teaching bug")
            dialog.grab().save(str(root / "new-case.png"))
            dialog.save_case(case_path)
        created = modal(window.new_case_button.click, CaseWizard, repository, save_case)
        assert created.result_data["case"] == str(case_path)
        assert window.selected_case()["file"] == str(case_path)
        assert not list((root / "reports").glob("*/report.json"))
        result["checks"].append("case_wizard_saved_pinned_case_without_execution")
        window.run_button.click()
        wait(lambda: window.task is None)
        assert window.last_result["status"] == "verified", window.status_label.text()
        result["checks"].append("created_case_verified_with_selected_runtime")
        def prepare(dialog):
            dialog.issue_edit.setPlainText("compute() 应返回 42；修复前会抛出 teaching bug。")
            dialog.auto_context_check.setChecked(False)
            for i in range(dialog.context_list.count()):
                item = dialog.context_list.item(i)
                if item.text() == "sample.py":
                    item.setCheckState(Qt.CheckState.Checked)
            dialog.grab().save(str(root / "new-draft.png"))
            dialog.save_button.click()
        drafted = modal(window.new_draft_button.click, DraftWizard, repository, prepare)
        folder = Path(drafted.result_data["draft_dir"])
        assert (folder / "prompt.txt").is_file() and not (folder / "response.json").exists()
        assert window.selected_draft()["draft_dir"] == str(folder)
        result["checks"].append("draft_wizard_prepared_context_without_model_or_test")
        window.tabs.setCurrentWidget(window.cases_page)
        window.case_search.setText("教学验收")
        window.select_visible_cases()
        assert window.checked_cases() == [str(case_path)]
        window.case_search.clear()
        # Include a bundled teaching case as a second distinct selection.
        for i in range(window.case_tree.topLevelItemCount()):
            item = window.case_tree.topLevelItem(i)
            if "demo_case" in item.data(0, Qt.ItemDataRole.UserRole)["file"]:
                item.setCheckState(0, Qt.CheckState.Checked)
        batch = modal(window.batch_button.click, BatchDialog, lambda dialog: None, lambda dialog: dialog.start_button.click())
        assert batch.result_data["status"] == "verified" and batch.result_data["total"] == 2
        result["checks"].append("checkbox_search_and_two_case_batch_verified")
        slow = root / "slow"
        for name in ("before", "after"):
            (slow / name).mkdir(parents=True, exist_ok=True)
            (slow / name / "sample.py").write_text("VALUE = 1\n")
        (slow / "regression.py").write_text("import time\ndef test_wait(): time.sleep(30)\n")
        slow_case = slow / "case.json"
        slow_case.write_text(json.dumps({"title":"取消测试样例","before":"before","after":"after","test":"regression.py",
                                        "expected_failure":{"type":"ValueError","message_contains":"never"}},ensure_ascii=False))
        cancellation = BatchDialog(store, [case_path, slow_case], parent=window)
        cancellation.show()
        cancellation.start_button.click()
        wait(lambda: cancellation.items[str(slow_case)].text(1) == "运行中")
        cancellation.cancel_button.click()
        wait(lambda: cancellation.task is None, timeout=10)
        assert cancellation.result_data["status"] == "cancelled"
        assert cancellation.result_data["counts"]["verified"] == 1
        result["checks"].append("cancel_second_case_keeps_first_verified")
        cancellation.grab().save(str(root / "cancelled-batch.png"))
        cancellation.close()
        window.refresh()
        wait(lambda: window.task is None)
        window.grab().save(str(root / "workspace.png"))
        window.close()
        result["ok"] = True
    except Exception:
        result["ok"] = False
        result["error"] = traceback.format_exc()
        if window and window.task:
            window.task.control.cancel()
            window.task.wait(15000)
    (root / "workflow-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
