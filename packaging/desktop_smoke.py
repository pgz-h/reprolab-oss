"""Explicit release QA. Uses the shipped Qt widgets and real bundled workers.

Run: ReproLab.app/Contents/MacOS/ReproLab --desktop-smoke /absolute/qa-directory
Optional: --python /project/venv/bin/python, --restart-only, --open-report.
No existing model configuration is opened or used.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--python", default="")
    parser.add_argument("--case", type=Path)
    parser.add_argument("--restart-only", action="store_true")
    parser.add_argument("--open-report", action="store_true")
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    output = {"frozen": bool(getattr(sys, "frozen", False)), "executable": sys.executable, "checks": []}
    window = None

    def wait_idle():
        deadline = time.monotonic() + 45
        while window.task is not None and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)
            # Let the background Python worker acquire the GIL between Qt polls.
            time.sleep(.005)
        if window.task is not None:
            window.task.control.cancel()
            window.task.wait(15000)
            app.processEvents()
            raise AssertionError("Desktop task timed out")
        app.processEvents()

    try:
        store = DesktopStore(root / "state")
        if not args.restart_only:
            store.settings.update(cases=str(root / "cases"), drafts=str(root / "drafts"),
                                  reports=str(root / "reports"), imports=str(root / "imported"),
                                  model_config="", python=args.python)
            store.save()
        window = MainWindow(store)
        window.show()
        wait_idle()
        window.tabs.setCurrentWidget(window.cases_page)
        assert window.case_tree.topLevelItemCount() >= 2
        output["checks"].append("catalog_loaded")
        if args.restart_only:
            assert store.settings["last_case"] == window.selected_case()["file"]
            assert window.report_tree.topLevelItemCount() >= 1
            output["checks"].append("restarted_with_cases_settings_and_report")
        else:
            if args.case:
                window.add_case_path(args.case)
                wait_idle()
            ticks = []
            QTimer.singleShot(100, lambda: ticks.append("responsive"))
            if not args.case and not args.python:
                window.tabs.setCurrentWidget(window.home_page)
                # The project catalog has its own short background read.
                deadline = time.monotonic() + 10
                while window.project_workbench.busy and time.monotonic() < deadline:
                    app.processEvents()
                    QTest.qWait(10)
                QTest.mouseClick(window.home_page.demo_button, Qt.MouseButton.LeftButton)
            else:
                QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
            wait_idle()
            result = window.last_result
            assert result and result["status"] == "verified", (result, window.status_label.text())
            assert result["before"]["kind"] == "failed" and result["after"]["kind"] == "passed"
            assert ticks
            assert Path(result["report_dir"], "report.html").is_file()
            assert window.report_button.isEnabled()
            output.update(status=result["status"], report_dir=result["report_dir"], runtime=result["runtime"],
                          before=result["before"]["kind"], after=result["after"]["kind"])
            output["checks"].extend(["run_button", "ui_responsive", "before_failed_after_passed", "html_report_saved"])
            if args.open_report:
                QTest.mouseClick(window.report_button, Qt.MouseButton.LeftButton)
                assert "已交给系统打开" in window.status_label.text()
                output["checks"].append("report_button_os_open_accepted")
        window.grab().save(str(root / ("restart.png" if args.restart_only else "verification.png")))
        window.close()
        app.processEvents()
        output["ok"] = True
    except Exception:
        output["ok"] = False
        output["error"] = traceback.format_exc()
        if window and window.task:
            window.task.control.cancel()
            window.task.wait(15000)
    filename = "restart-result.json" if args.restart_only else "smoke-result.json"
    (root / filename).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output["ok"] else 2
