"""Desktop v0.11 interactions use real widgets and trusted temporary cases."""
from pathlib import Path
import time

import pytest
pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItem
from PySide6.QtTest import QTest
from test_desktop_ui import window, wait_idle
from test_batch import saved_case


def test_search_hides_nonmatching_cases_and_prevents_running_hidden_selection(window):
    assert hasattr(window, "case_search"), "case search is missing"
    window.case_search.setText("不存在的案例名称")
    assert all(window.case_tree.topLevelItem(i).isHidden() for i in range(window.case_tree.topLevelItemCount()))
    assert not window.run_button.isEnabled()
    window.case_search.clear()
    assert window.run_button.isEnabled()
    window.case_tree.clear()
    for status in ('regression_passed', 'improved', 'regression_detected', 'baseline_failure'):
        item = QTreeWidgetItem([status, status])
        item.setData(0, Qt.ItemDataRole.UserRole, {'title':status, 'file':'/tmp/' + status + '.json',
                     'status':status, 'readable':False, 'error':'teaching', 'source':'snapshot', 'test':''})
        window.case_tree.addTopLevelItem(item)
    def visible_states():
        return {window.case_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)['status']
                for i in range(window.case_tree.topLevelItemCount()) if not window.case_tree.topLevelItem(i).isHidden()}
    window.filter_tree(window.case_tree, '', 'attention')
    assert visible_states() == {'regression_detected', 'baseline_failure'}
    window.filter_tree(window.case_tree, '', 'verified')
    assert visible_states() == {'regression_passed', 'improved'}


def test_checkboxes_count_selection_and_select_only_visible(window):
    assert hasattr(window, "batch_button"), "batch UI is missing"
    window.case_search.setText("中文文件名")
    window.select_visible_cases()
    assert len(window.checked_cases()) == 1
    assert "1" in window.batch_button.text()
    window.case_search.clear()
    window.clear_checked_cases()
    assert not window.checked_cases()
    assert not window.batch_button.isEnabled()


def test_empty_draft_and_report_actions_are_disabled(window):
    assert not window.verify_draft_button.isEnabled()
    assert not window.generate_button.isEnabled()
    assert not window.recover_button.isEnabled()
    assert not window.browse_report_button.isEnabled()
    assert not window.pack_button.isEnabled()


def test_selected_batch_dialog_runs_real_cases_and_keeps_summary(window, tmp_path):
    from reprolab.desktop_batch import BatchDialog
    a = saved_case(tmp_path / "one", "a")
    b = saved_case(tmp_path / "two", "b", after='raise ValueError("the reported bug")')
    dialog = BatchDialog(window.store, [a, b], python_executable="", timeout=15, parent=window)
    dialog.show()
    assert dialog.task is None
    dialog.start_button.click()
    deadline = time.monotonic() + 20
    while dialog.task and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert dialog.task is None
    assert dialog.result_data["completed"] == 2
    assert dialog.result_data["counts"]["verified"] == 1
    assert dialog.result_data["counts"]["still_failing"] == 1
    assert Path(dialog.result_data["summary_dir"], "summary.html").is_file()
    assert dialog.open_button.isEnabled()
    assert window.store.activity[str(a)]["status"] == "verified"
    assert window.store.activity[str(b)]["status"] == "still_failing"
    dialog.close()
    window.refresh()
    wait_idle(window)
    batches = [window.report_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
               for i in range(window.report_tree.topLevelItemCount())]
    assert any(r.get("kind") == "batch" for r in batches)


def test_batch_cancel_preserves_not_run_status_and_finishes(window, tmp_path):
    from reprolab.desktop_batch import BatchDialog
    case = saved_case(tmp_path / "cases", "slow")
    (tmp_path / "cases/fixtures/slow/regression.py").write_text("import time\ndef test_slow(): time.sleep(30)\n")
    dialog = BatchDialog(window.store, [case], python_executable="", timeout=30, parent=window)
    dialog.show()
    dialog.start_button.click()
    QTest.qWait(150)
    dialog.cancel_button.click()
    deadline = time.monotonic() + 5
    while dialog.task and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert dialog.task is None
    assert dialog.result_data["status"] == "cancelled"
    assert dialog.result_data["counts"]["verified"] == 0
    assert "取消" in dialog.status_label.text()
    dialog.close()


def test_created_case_handoff_uses_wizard_python_for_actual_run(window, tmp_path, monkeypatch):
    import reprolab.desktop as desktop
    from test_workflow import project_case, make_environment
    from test_git_source import git
    from reprolab.recipes import create_recipe
    project = tmp_path / "project"; project.mkdir()
    repo, before, after, target = project_case(project)
    executable, purelib = make_environment(tmp_path, True)
    (purelib / "wizard_dependency.py").write_text("VALUE = 42\n")
    test = repo / "tests/test_logic.py"
    test.write_text(test.read_text().replace("    assert compute()", "    from wizard_dependency import VALUE\n    assert VALUE == 42\n    assert compute()"))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "test requires selected environment")
    result = create_recipe(tmp_path / "cases/new.json", repo, before, "HEAD", target,
                           failure_type="ValueError", failure_message="reported bug", python_executable=str(executable))
    class CompletedWizard:
        result_data = result
        def __init__(self, *args): pass
        def exec(self): return desktop.QDialog.DialogCode.Accepted
    monkeypatch.setattr(desktop, "CaseWizard", CompletedWizard)
    window.new_case()
    wait_idle(window)
    assert window.python_edit.text() == str(executable)
    window.run_button.click()
    wait_idle(window)
    assert window.last_result["status"] == "verified"
    assert window.last_result["runtime"]["executable"] == str(executable)


def test_new_draft_custom_folder_is_selected_and_environment_preserved(window, tmp_path, monkeypatch):
    import reprolab.desktop as desktop
    from test_git_source import prepare_repo
    from reprolab.drafts import prepare_draft
    repo, before, after, _ = prepare_repo(tmp_path)
    result = prepare_draft(repo, before, after, issue="compute should return 42",
                           paths=["sample.py"], drafts_root=tmp_path / "custom-drafts")
    class CompletedWizard:
        result_data = result
        def __init__(self, *args): pass
        def exec(self): return desktop.QDialog.DialogCode.Accepted
    monkeypatch.setattr(desktop, "DraftWizard", CompletedWizard)
    window.new_draft()
    wait_idle(window)
    assert window.selected_draft()["draft_dir"] == result["draft_dir"]
    assert window.path_edits["drafts"].text() == str(tmp_path / "custom-drafts")
    assert window.store.settings["drafts"] == str(tmp_path / "custom-drafts")
    assert "compute should return 42" in window.draft_views["prompt.txt"].toPlainText()
