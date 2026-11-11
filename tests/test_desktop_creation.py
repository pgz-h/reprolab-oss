"""Real offscreen creation flows; committed fixtures are never imported during reads."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from test_git_source import git
from test_workflow import project_case, make_environment


@pytest.fixture
def dialogs(tmp_path):
    from reprolab.desktop_creation import CaseWizard, DraftWizard
    app = QApplication.instance() or QApplication([])
    settings = dict(cases=str(tmp_path / 'cases'), drafts=str(tmp_path / 'drafts'), python=sys.executable)
    opened = []
    def make(kind='case'):
        dialog = (CaseWizard if kind == 'case' else DraftWizard)(settings)
        opened.append(dialog)
        dialog.show()
        return dialog
    yield make
    for dialog in opened:
        wait_idle(dialog)
        dialog.close()
    app.processEvents()


def wait_idle(dialog, seconds=15):
    deadline = time.monotonic() + seconds
    while (dialog.task is not None or dialog.pending) and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    QApplication.processEvents()
    assert dialog.task is None and not dialog.pending


def select_repo(dialog, repo):
    dialog.repository_edit.setText(str(repo))
    dialog.refresh_repository()
    wait_idle(dialog)


def fill_case(dialog, target):
    dialog.target_edit.setText(target)
    dialog.failure_type_edit.setText('ValueError')
    dialog.failure_message_edit.setText('reported bug')


def test_case_pins_full_commits_preserves_environment_and_really_verifies(dialogs, tmp_path):
    from reprolab.recipes import run_saved_case
    repo, before, after, target = project_case(tmp_path)
    python, _ = make_environment(tmp_path, True)
    dialog = dialogs()
    select_repo(dialog, repo)
    assert dialog.selected_commits == (before, after)
    assert target in [dialog.tests_list.item(i).text() for i in range(dialog.tests_list.count())]
    dialog.search_edit.setText('nonexistent')
    assert all(dialog.tests_list.item(i).isHidden() for i in range(dialog.tests_list.count()))
    dialog.search_edit.clear()
    fill_case(dialog, target)
    dialog.python_edit.setText(str(python))
    dialog.save_case(tmp_path / 'cases/saved.json')
    wait_idle(dialog)
    assert dialog.result() == QDialog.DialogCode.Accepted
    data = json.loads(Path(dialog.result_data['case']).read_text())
    assert data['before_ref'] == before and data['after_ref'] == after
    assert data['python'] == str(python)
    assert not (tmp_path / 'reports').exists()
    result = run_saved_case(dialog.result_data['case'], tmp_path / 'reports')
    assert result['status'] == 'verified'


def test_discovery_and_offline_explicit_context_never_execute_committed_or_dirty_code(dialogs, tmp_path):
    repo, before, _, target = project_case(tmp_path)
    marker = tmp_path / 'executed'
    (repo / 'tests/conftest.py').write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'import marker must stay inactive')
    after = git(repo, 'rev-parse', 'HEAD')
    (repo / 'sample.py').write_text('DIRTY CONTENT MUST NOT BE READ')
    dialog = dialogs('draft')
    select_repo(dialog, repo)
    dialog.before_combo.setEditText(before)
    dialog.load_selection()
    wait_idle(dialog)
    dialog.issue_edit.setPlainText('compute 应返回 42')
    dialog.auto_context_check.setChecked(False)
    item = next(dialog.context_list.item(i) for i in range(dialog.context_list.count())
                if dialog.context_list.item(i).text() == 'sample.py')
    item.setCheckState(Qt.CheckState.Checked)
    dialog.prepare()
    wait_idle(dialog)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_data['status'] == 'prepared'
    folder = Path(dialog.result_data['draft_dir'])
    context = json.loads((folder / 'context.json').read_text())
    assert context['before_commit'] == before and context['after_commit'] == after
    assert [f['path'] for f in context['files']] == ['sample.py']
    assert 'return 42' in (folder / 'prompt.txt').read_text()
    assert 'DIRTY CONTENT' not in (folder / 'prompt.txt').read_text()
    assert not marker.exists() and not (folder / 'cases').exists()


@pytest.mark.parametrize('bad', ['same', 'invalid', 'inside', 'existing', 'empty_message'])
def test_invalid_case_inputs_are_friendly_and_never_overwrite(dialogs, tmp_path, bad):
    repo, before, after, target = project_case(tmp_path)
    dialog = dialogs()
    select_repo(dialog, repo)
    fill_case(dialog, target)
    destination = tmp_path / 'cases/case.json'
    if bad == 'same':
        dialog.before_combo.setEditText(after)
    elif bad == 'invalid':
        dialog.after_combo.setEditText('missing-ref')
    elif bad == 'inside':
        destination = repo / 'case.json'
    elif bad == 'existing':
        destination.parent.mkdir()
        destination.write_text('keep me')
    else:
        dialog.failure_message_edit.clear()
    dialog.save_case(destination)
    wait_idle(dialog)
    assert not dialog.result_data
    assert dialog.status_label.text() and 'Traceback' not in dialog.status_label.text()
    if bad == 'existing':
        assert destination.read_text() == 'keep me'
    else:
        assert not destination.exists()


def test_ref_edit_clears_old_targets_and_refuses_save_until_reread(dialogs, tmp_path):
    repo, before, after, target = project_case(tmp_path)
    dialog = dialogs()
    select_repo(dialog, repo)
    fill_case(dialog, target)
    dialog.after_combo.setEditText(before)
    assert dialog.tests_list.count() == 0 and not dialog.target_edit.text()
    assert dialog.selected_commits is None
    dialog.save_case(tmp_path / 'stale.json')
    assert not dialog.result_data and dialog.task is None


def test_late_background_result_is_discarded_and_close_waits_safely(dialogs, tmp_path, monkeypatch):
    import reprolab.desktop_creation as creation
    repo, _, _, target = project_case(tmp_path)
    dialog = dialogs()
    select_repo(dialog, repo)
    started, release = threading.Event(), threading.Event()
    real = creation.list_project_tests
    def slow(*args):
        started.set()
        assert release.wait(5)
        return real(*args)
    monkeypatch.setattr(creation, 'list_project_tests', slow)
    dialog.load_selection()
    deadline = time.monotonic() + 5
    while not started.is_set() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert started.is_set()
    dialog.repository_edit.setText(str(tmp_path / 'different'))
    assert not dialog.target_edit.text()
    dialog.reject()
    assert dialog.task is not None
    release.set()
    wait_idle(dialog)
    assert dialog.tests_list.count() == 0
    assert not dialog.result_data and not dialog.isVisible()


def test_invalid_repository_and_empty_explicit_context_explain_next_step(dialogs, tmp_path):
    dialog = dialogs('draft')
    select_repo(dialog, tmp_path)
    assert not dialog.selected_commits and '仓库' in dialog.status_label.text()
    repo, _, _, _ = project_case(tmp_path)
    select_repo(dialog, repo)
    dialog.issue_edit.setPlainText('compute 应返回42')
    dialog.auto_context_check.setChecked(False)
    dialog.prepare()
    wait_idle(dialog)
    assert not dialog.result_data
    assert '选择' in dialog.status_label.text()


def test_case_discovery_is_static_and_parameter_target_stays_editable(dialogs, tmp_path):
    repo, _, _, _ = project_case(tmp_path)
    marker = tmp_path / 'imported'
    (repo / 'tests/conftest.py').write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (repo / 'tests/test_params.py').write_text('import pytest\n@pytest.mark.parametrize("value", [1, 2], ids=["one", "two"])\ndef test_param(value):\n    assert value > 0\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'parameterized test and unsafe import')
    dialog = dialogs()
    select_repo(dialog, repo)
    assert not marker.exists()
    target = 'tests/test_params.py::test_param[two]'
    fill_case(dialog, target)
    dialog.save_case(tmp_path / 'parameter.json')
    wait_idle(dialog)
    assert dialog.result_data['test_target'] == target
    assert not marker.exists()


def test_unknown_ref_error_never_restores_old_context(dialogs, tmp_path):
    repo, _, _, _ = project_case(tmp_path)
    dialog = dialogs('draft')
    select_repo(dialog, repo)
    assert dialog.context_list.count() > 0
    dialog.after_combo.setEditText('missing-branch')
    dialog.load_selection()
    wait_idle(dialog)
    assert dialog.context_list.count() == 0 and dialog.selected_commits is None
    assert '未完成' in dialog.status_label.text()


def test_existing_dangling_case_symlink_is_not_followed(dialogs, tmp_path):
    repo, _, _, target = project_case(tmp_path)
    dialog = dialogs()
    select_repo(dialog, repo)
    fill_case(dialog, target)
    link = tmp_path / 'old-case.json'
    destination = tmp_path / 'must-not-create.json'
    link.symlink_to(destination)
    dialog.save_case(link)
    wait_idle(dialog)
    assert link.is_symlink() and not destination.exists()
    assert not dialog.result_data and '已经存在' in dialog.status_label.text()


def test_native_python_picker_keeps_virtualenv_symlink(dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    dialog = dialogs()
    python = tmp_path / 'venv/bin/python'
    observed = []
    def choose(*args, **kwargs):
        observed.append(kwargs['options'])
        return str(python), ''
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', choose)
    dialog.python_button.click()
    assert dialog.python_edit.text() == str(python)
    assert observed == [QFileDialog.Option.DontResolveSymlinks]


def test_parented_dialogs_grow_fields_and_scroll_without_clipping(tmp_path):
    from reprolab.desktop import MainWindow
    from reprolab.desktop_creation import CaseWizard, DraftWizard
    from reprolab.desktop_store import DesktopStore
    from PySide6.QtWidgets import QScrollArea
    app = QApplication.instance() or QApplication([])
    store = DesktopStore(tmp_path / 'state')
    store.settings.update(cases=str(tmp_path / 'cases'), drafts=str(tmp_path / 'drafts'),
                          reports=str(tmp_path / 'reports'), imports=str(tmp_path / 'imports'))
    parent = MainWindow(store)
    from test_desktop_ui import wait_idle as wait_parent
    wait_parent(parent)
    try:
        for cls in (CaseWizard, DraftWizard):
            dialog = cls(store.settings, parent)
            dialog.show()
            app.processEvents()
            assert isinstance(dialog.scroll_area, QScrollArea)
            assert dialog.repository_edit.width() > 280
            assert dialog.before_combo.width() > 500
            assert dialog.python_edit.width() > 400
            dialog._show_selection(('/tmp/repo', 'a' * 40, 'b' * 40,
                                    {'tests': [], 'notes': []} if cls == CaseWizard else []))
            assert 'a' * 40 in dialog.version_label.toolTip()
            assert 'a' * 40 not in dialog.version_label.text()
            dialog.resize(860, 520)
            app.processEvents()
            assert dialog.height() == 520
            assert dialog.scroll_area.verticalScrollBar().maximum() > 0
            assert dialog.buttons.geometry().bottom() <= dialog.height()
            dialog.close()
    finally:
        parent.close()
        app.processEvents()
