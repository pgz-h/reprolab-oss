from test_desktop_creation import dialogs, select_repo, wait_idle
from test_workflow import project_case


def test_failed_recollection_clears_old_parameter_selection(dialogs, tmp_path):
    repo, _, _, target = project_case(tmp_path)
    dialog = dialogs()
    select_repo(dialog, repo)
    dialog.target_edit.setText(target)
    dialog.collect_button.click()
    wait_idle(dialog)
    assert dialog.tests_list.count() == 1
    dialog.tests_list.setCurrentRow(0)
    assert dialog.target_edit.text()
    dialog.python_edit.setText(str(tmp_path / 'missing-python'))
    dialog.collect_button.click()
    wait_idle(dialog)
    assert dialog.tests_list.count() == 0 and not dialog.target_edit.text()
    assert not dialog.result_data
