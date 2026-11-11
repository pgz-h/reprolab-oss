"""Explicit v0.12 release QA using only newly authored local teaching cases."""
import argparse
import json
from pathlib import Path
import sys
import time
import traceback
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.desktop_batch import BatchDialog
from reprolab.retry import plan_retry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    result = {"frozen": bool(getattr(sys, "frozen", False)), "checks": []}
    window = None
    dialogs = []

    def wait(predicate, seconds=45):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            app.processEvents()
            QTest.qWait(15)
            # Let the background Python worker acquire the GIL between Qt polls.
            time.sleep(.005)
        assert predicate(), "UI operation timed out"
        app.processEvents()

    def case(name, after="return 42", test=None):
        folder = root / "fixtures" / name
        folder.mkdir(parents=True, exist_ok=False)
        for label, body in (("before", 'raise ValueError("teaching bug")'), ("after", after)):
            (folder / label).mkdir()
            (folder / label / "sample.py").write_text("def compute():\n    " + body + "\n")
        (folder / "regression.py").write_text(test or "from sample import compute\ndef test_one(): assert compute() == 42\n")
        path = folder / "case.json"
        path.write_text(json.dumps({"title": name, "before": "before", "after": "after", "test": "regression.py",
                                    "expected_failure": {"type": "ValueError", "message_contains": "teaching bug"}}))
        return path

    def run_case(path):
        window.add_case_path(path)
        wait(lambda: window.task is None)
        window.run_button.click()
        wait(lambda: window.task is None)
        return window.last_result

    def batch(paths, plan=None, complete=True):
        dialog = BatchDialog(store, paths, python_executable=args.python, parent=window, retry_plan=plan)
        dialogs.append(dialog)
        dialog.show()
        dialog.start_button.click()
        if complete:
            wait(lambda: dialog.task is None)
            assert dialog.result_data, dialog.status_label.text()
        return dialog

    try:
        store = DesktopStore(root / "state")
        store.settings.update(cases=str(root / "cases"), drafts=str(root / "drafts"), reports=str(root / "reports"),
                              imports=str(root / "imports"), model_config="", python="")
        window = MainWindow(store)
        window.show()
        wait(lambda: window.task is None)
        invalid = run_case(case("预检阻止无效测试", test="def broken(:\n    pass\n"))
        assert invalid["tests_executed"] is False and invalid["preflight"]["status"] == "attention"
        assert not list((root / "reports").glob("*/report.json"))
        result["checks"].append("preflight_blocks_without_execution")
        window.grab().save(str(root / "preflight.png"))
        needs = case("选择环境后恢复验收", test="import reprolab_qa_dependency\nfrom sample import compute\ndef test_one(): assert compute() == reprolab_qa_dependency.VALUE\n")
        missing = run_case(needs)
        assert missing["status"] == "inconclusive" and missing["diagnosis"]["before"]["code"] == "module_not_found"
        assert missing["environment"]["status"] == "recorded"
        result["checks"].append("actual_missing_module_explained")
        window.grab().save(str(root / "missing-dependency.png"))
        window.python_edit.setText(args.python)
        window.run_button.click()
        wait(lambda: window.task is None)
        fixed = window.last_result
        assert fixed["status"] == "verified" and fixed["runtime"]["executable"] == args.python
        assert fixed["environment_comparison"]["status"] == "changed"
        result["checks"].append("selected_venv_recovers_and_records_environment_change")
        window.grab().save(str(root / "environment-recovery.png"))
        good = case("已完成案例")
        bad = case("需要重跑的案例", after='raise ValueError("teaching bug")')
        original = batch([good, bad])
        source = Path(original.summary_dir) / "summary.json"
        old_bytes = source.read_bytes()
        good_report = store.activity[str(good)]["report"]
        (bad.parent / "after/sample.py").write_text("def compute(): return 42\n")
        plan = plan_retry(source)
        assert plan["paths"] == [str(bad)]
        retried = batch(plan["paths"], plan)
        assert retried.result_data["status"] == "verified"
        assert retried.result_data["retry_origin"]["source_summary"] == str(source)
        assert source.read_bytes() == old_bytes and store.activity[str(good)]["report"] == good_report
        result["checks"].append("retry_only_nonverified_preserves_old_evidence")
        retried.grab().save(str(root / "retry.png"))
        retried.close(); original.close()
        slow = case("被取消的案例", test="import time\ndef test_wait(): time.sleep(30)\n")
        paused = batch([good, slow, needs], complete=False)
        wait(lambda: "2 / 3" in paused.status_label.text() and "运行旧版" in paused.status_label.text())
        paused.cancel_button.click()
        wait(lambda: paused.task is None, 15)
        assert paused.result_data["status"] == "cancelled" and paused.result_data["counts"]["verified"] == 1
        source = Path(paused.summary_dir) / "summary.json"
        old_bytes = source.read_bytes()
        (slow.parent / "regression.py").write_text("from sample import compute\ndef test_one(): assert compute() == 42\n")
        plan = plan_retry(source, mode="unfinished")
        assert plan["paths"] == [str(slow), str(needs)]
        resumed = batch(plan["paths"], plan)
        assert resumed.result_data["status"] == "verified" and resumed.result_data["total"] == 2
        assert source.read_bytes() == old_bytes
        result["checks"].append("resume_cancelled_and_pending_only")
        resumed.grab().save(str(root / "resume.png"))
        resumed.close(); paused.close()
        exited = run_case(case("工作进程记录缺失", test="def test_one():\n    import os\n    os._exit(0)\n"))
        assert exited["status"] == "inconclusive" and exited["diagnosis"]["before"]["code"] == "missing_worker_record"
        result["checks"].append("missing_worker_evidence_is_not_zero_tests")
        window.close()
        result["ok"] = True
    except Exception:
        result["ok"] = False
        result["error"] = traceback.format_exc()
        for dialog in dialogs:
            if dialog.task:
                dialog.task.control.cancel()
                dialog.task.wait(15000)
    (root / "diagnostics-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
