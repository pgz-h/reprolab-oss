"""Exercise real Qt widgets and core execution; never call a live model."""
import importlib.util
import os
from pathlib import Path
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_runner import make_case


@pytest.fixture
def window(tmp_path):
    assert importlib.util.find_spec("reprolab.desktop"), "desktop interface is missing"
    from reprolab.desktop import MainWindow
    from reprolab.desktop_store import DesktopStore
    app = QApplication.instance() or QApplication([])
    store = DesktopStore(tmp_path / "state")
    store.settings.update(cases=str(tmp_path / "cases"), drafts=str(tmp_path / "drafts"),
                          reports=str(tmp_path / "reports"), imports=str(tmp_path / "imported"))
    w = MainWindow(store)
    w.show()
    wait_idle(w)
    w.tabs.setCurrentWidget(w.cases_page)
    yield w
    if w.task:
        w.task.control.cancel()
        wait_idle(w)
    w.close()
    app.processEvents()


def wait_idle(window, seconds=20):
    deadline = time.monotonic() + seconds
    while window.task is not None and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
        time.sleep(.005)
    assert window.task is None, "background task did not finish"
    QApplication.processEvents()


def test_real_case_button_runs_both_versions_and_enables_report(window):
    QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
    wait_idle(window)
    result = window.last_result
    assert result["status"] == "verified"
    assert result["before"]["kind"] == "failed"
    assert result["after"]["kind"] == "passed"
    assert window.report_button.isEnabled()
    assert (Path(result["report_dir"]) / "report.html").is_file()
    assert "通过" in window.verdict_label.text()


def test_cancel_is_responsive_and_never_shows_success(window, tmp_path):
    folder = tmp_path / "slow"; folder.mkdir()
    case = make_case(folder, test="import time\ndef test_slow(): time.sleep(30)\n")
    window.add_case_path(case)
    wait_idle(window)
    QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
    QTimer.singleShot(200, window.cancel_button.click)
    ticks = []
    QTimer.singleShot(50, lambda: ticks.append("responsive"))
    wait_idle(window, seconds=5)
    assert ticks == ["responsive"]
    assert window.last_result is None
    assert "取消" in window.verdict_label.text()
    assert window.store.activity[str(case)]["status"] == "cancelled"


def test_restart_remembers_selected_case_and_python(window, tmp_path):
    from reprolab.desktop import MainWindow
    from reprolab.desktop_store import DesktopStore
    folder = tmp_path / "chosen"; folder.mkdir()
    case = make_case(folder)
    window.add_case_path(case)
    wait_idle(window)
    window.python_edit.setText(sys.executable)
    window.save_settings()
    state_dir = window.store.directory
    other = MainWindow(DesktopStore(state_dir))
    wait_idle(other)
    assert other.selected_case()["file"] == str(case)
    assert other.python_edit.text() == sys.executable
    other.close()


def test_import_shows_metadata_and_requires_separate_replay(window, tmp_path):
    from reprolab.evidence_bundle import pack_report
    QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
    wait_idle(window)
    original = window.last_result
    bundle = tmp_path / "proof.zip"
    pack_report(original["report_dir"], bundle)
    before = len(list(Path(window.store.settings["reports"]).glob("*/report.json")))
    window.import_bundle_path(bundle)
    wait_idle(window)
    assert window.imported_case is not None
    assert window.replay_button.isEnabled()
    assert "未执行" in window.import_info.text()
    assert len(list(Path(window.store.settings["reports"]).glob("*/report.json"))) == before


def test_evidence_check_shows_legacy_permission_limit(window):
    from test_source_permissions import _make_legacy_report

    window.run_button.click()
    wait_idle(window)
    report = Path(window.last_result["report_dir"])
    _make_legacy_report(report)
    window.check_button.click()
    wait_idle(window)
    assert "权限" in window.report_info.text()
    assert "未记录" in window.report_info.text()


def test_legacy_ai_plan_shows_missing_original_permission_notice(window, tmp_path):
    import json
    from reprolab.desktop_suite import AISuiteDialog
    from reprolab.repair_suite import prepare_suite, generate_suite
    from test_git_source import prepare_repo
    from test_repair_suite import _plan_response

    repo, before, after, _ = prepare_repo(tmp_path)
    prepared = prepare_suite(repo, before, after, "修复 compute", tmp_path / "suites")
    suite = Path(prepared["directory"])
    state_path = suite / "suite.json"
    state = json.loads(state_path.read_text())
    state.pop("source_mode_sha256")
    state_path.write_text(json.dumps(state))
    response = tmp_path / "response.json"
    response.write_text(json.dumps(_plan_response()))
    generate_suite(suite, response_file=response)
    dialog = AISuiteDialog(window.store, window)
    dialog.load_plan(suite)
    assert "未记录" in dialog.status_label.text()
    assert "权限" in dialog.status_label.text()
    dialog.close()


def test_empty_report_directory_does_not_run_in_working_directory(window):
    window.path_edits["reports"].clear()
    QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
    assert window.task is None
    assert window.last_result is None
    assert "报告目录" in window.status_label.text()


def test_settings_write_failure_does_not_start_run(window, monkeypatch):
    def denied():
        raise PermissionError("settings are read-only")
    monkeypatch.setattr(window.store, "save", denied)
    QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
    assert window.task is None
    assert window.last_result is None
    assert "未保存" in window.status_label.text()


def test_running_imported_case_displays_its_own_title(window, tmp_path):
    folder = tmp_path / "another"; folder.mkdir()
    case = make_case(folder)
    window.start_verification(case)
    wait_idle(window)
    assert window.case_title.text() == "回归验证"
    assert window.last_result["title"] == "回归验证"


def test_cancel_during_report_save_is_not_falsely_acknowledged(window, monkeypatch):
    import threading
    import reprolab.desktop as desktop
    saving = threading.Event()
    real_write = desktop.write_report
    def slow_write(result):
        saving.set()
        time.sleep(.25)
        return real_write(result)
    monkeypatch.setattr(desktop, "write_report", slow_write)
    observed = []
    timer = QTimer()
    def cancel_when_saving():
        if saving.is_set() and not observed:
            window.cancel_button.click()
            observed.append(window.status_label.text())
    timer.timeout.connect(cancel_when_saving)
    timer.start(10)
    window.run_button.click()
    wait_idle(window)
    timer.stop()
    assert observed and "正在取消" not in observed[0]
    assert window.last_result["status"] == "verified"


def test_selection_and_cancel_keep_list_and_details_consistent(window):
    from PySide6.QtCore import Qt
    window.run_button.click()
    wait_idle(window)
    first = window.case_tree.currentItem()
    window.case_tree.setCurrentItem(window.case_tree.topLevelItem(1))
    window.case_tree.setCurrentItem(first)
    assert "通过" in window.verdict_label.text()
    assert first.data(0, Qt.ItemDataRole.UserRole)["status"] == "verified"
    window.run_button.click()
    window.cancel_button.click()
    wait_idle(window)
    assert "取消" in first.text(1)
    window.case_tree.setCurrentItem(window.case_tree.topLevelItem(1))
    window.case_tree.setCurrentItem(first)
    assert "取消" in window.verdict_label.text()


def test_new_report_appears_in_browser_without_manual_refresh(window):
    window.run_button.click()
    wait_idle(window)
    assert window.report_tree.topLevelItemCount() == 1
    assert window.selected_report()["directory"] == window.last_result["report_dir"]


@pytest.mark.parametrize("consent", [False, True])
def test_model_preview_sends_only_after_explicit_consent(window, tmp_path, consent):
    from PySide6.QtWidgets import QDialog, QCheckBox, QPushButton, QPlainTextEdit, QLabel
    from test_drafts import prepare
    from test_ai_api import api_server, api_answer, config
    _, _, _, prepared = prepare(tmp_path)
    folder = Path(prepared["draft_dir"])
    window.store.settings["drafts"] = str(folder.parent)
    window.path_edits["drafts"].setText(str(folder.parent))
    window.refresh()
    wait_idle(window)
    observations = []
    with api_server(api_answer()) as (url, requests):
        window.path_edits["model_config"].setText(str(config(tmp_path, url)))
        def handle_dialog():
            dialog = QApplication.activeModalWidget()
            assert isinstance(dialog, QDialog)
            send = next(b for b in dialog.findChildren(QPushButton) if b.text() == "发送并生成草稿")
            assert not send.isEnabled()
            shown = "\n".join(v.toPlainText() for v in dialog.findChildren(QPlainTextEdit))
            shown += "\n".join(v.text() for v in dialog.findChildren(QLabel))
            assert "test-secret-do-not-save" not in shown
            assert url in shown
            observations.append(shown)
            if consent:
                dialog.findChild(QCheckBox).setChecked(True)
                send.click()
            else:
                dialog.reject()
        QTimer.singleShot(30, handle_dialog)
        window.preview_generate()
        wait_idle(window)
        assert len(requests) == (1 if consent else 0)
    assert observations
    assert (folder / "cases/case.json").exists() == consent
    assert not list(Path(window.store.settings["reports"]).glob("*/report.json"))
    if consent:
        assert window.selected_draft()["generation_status"] == "draft_generated"
