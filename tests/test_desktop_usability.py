"""First-use and project recovery paths, using real widgets and pytest workers."""
from pathlib import Path
import sys
import time

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from test_desktop_ui import window, wait_idle
from test_git_source import git
from test_workflow import make_environment


def wait_project(bench, seconds=30):
    deadline = time.monotonic() + seconds
    while bench.busy and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
        time.sleep(.005)
    assert not bench.busy, bench.status_label.text()
    QApplication.processEvents()


def project(tmp_path, *, missing_dependency=False):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'ReproLab test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / '.gitignore').write_text('.venv/\nvenv/\n')
    (repo / 'component.py').write_text(
        "def value(kind): return {'normal':42, 'fixed':0, 'regression':42}[kind]\n")
    (repo / 'tests').mkdir()
    (repo / 'tests/test_component.py').write_text(
        ('import reprolab_missing_demo_dependency\n' if missing_dependency else '')
        + "import pytest\nfrom component import value\n"
        "@pytest.mark.parametrize('index', range(3))\n"
        "def test_normal(index): assert value('normal') == 42\n"
        "def test_fixed(): assert value('fixed') == 42\n"
        "def test_regression(): assert value('regression') == 42\n")
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'baseline')
    (repo / 'component.py').write_text(
        "def value(kind): return {'normal':42, 'fixed':42, 'regression':0}[kind]\n")
    return repo


def freeze(window, repo):
    bench = window.project_workbench
    wait_project(bench)
    window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo))
    QTest.mouseClick(bench.prepare_button, Qt.MouseButton.LeftButton)
    wait_project(bench)
    assert bench.state is not None, bench.status_label.text()
    assert '第 2 步' in bench.next_step_label.text()
    return bench


def test_fresh_window_starts_with_task_choices_and_no_execution(window):
    from reprolab.desktop import MainWindow
    other = MainWindow(window.store)
    other.show()
    wait_idle(other)
    wait_project(other.project_workbench)
    assert other.tabs.currentWidget() is other.home_page
    assert other.last_result is None
    assert not list(Path(other.store.settings['reports']).glob('*/report.json'))
    QTest.mouseClick(other.home_page.project_button, Qt.MouseButton.LeftButton)
    assert other.tabs.currentWidget() is other.project_workbench
    assert '第 1 步' in other.project_workbench.next_step_label.text()
    other.tabs.setCurrentWidget(other.home_page)
    QTest.mouseClick(other.home_page.reports_button, Qt.MouseButton.LeftButton)
    assert other.tabs.currentWidget() is other.reports_page
    other.close()


def test_home_demo_uses_app_python_and_keeps_the_users_project_environment(window):
    wait_project(window.project_workbench)
    selected = '/example/project-env/bin/python'
    window.python_edit.setText(selected)
    window.tabs.setCurrentWidget(window.home_page)
    QTest.mouseClick(window.home_page.demo_button, Qt.MouseButton.LeftButton)
    wait_idle(window)
    assert window.tabs.currentWidget() is window.cases_page
    assert window.last_result['status'] == 'verified'
    assert window.last_result['runtime']['executable'] == sys.executable
    assert window.python_edit.text() == selected
    assert window.store.settings['python'] == selected


def test_candidate_environments_are_listed_without_starting_or_switching_them(window, tmp_path):
    repo = project(tmp_path)
    candidate = repo / '.venv/bin/python'
    candidate.parent.mkdir(parents=True)
    marker = tmp_path / 'interpreter-started'
    candidate.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
    candidate.chmod(0o755)
    bench = window.project_workbench
    wait_project(bench)
    window.tabs.setCurrentWidget(bench)
    bench.repository_edit.setText(str(repo))
    bench.python_edit.setText(sys.executable)
    bench.read_commits()
    wait_project(bench)
    assert bench.python_combo.findText(str(candidate)) >= 0
    assert bench.python_edit.text() == sys.executable
    assert not marker.exists()
    assert bench.environment_hint.isVisible()
    assert '不会自动切换' in bench.environment_hint.text()


def test_small_window_keeps_project_inputs_and_actions_readable(window):
    bench = window.project_workbench
    wait_project(bench)
    window.tabs.setCurrentWidget(bench)
    window.resize(960, 680)
    QApplication.processEvents()
    for widget in (bench.repository_edit, bench.repository_button, bench.before_combo,
                   bench.python_combo, bench.prepare_button, bench.discover_button,
                   bench.change_search, bench.changes_tree, bench.diff_view):
        assert widget.height() >= widget.minimumSizeHint().height()


def test_filtered_selection_runs_all_checked_tests_and_focuses_the_new_failure(window, tmp_path):
    from reprolab.project_sessions import check_project_report, pack_project_report, unpack_project_report, replay_project_report
    repo = project(tmp_path)
    bench = freeze(window, repo)
    (repo / 'component.py').write_text('def value(kind): return 999\n')
    bench.discover()
    wait_project(bench)
    assert len(bench.selected_test_ids()) == 5
    bench.clear_tests_button.click()
    assert bench.next_step_label.isVisible() and '当前选择为空' in bench.next_step_label.text()
    assert not bench.run_button.isEnabled()
    bench.select_visible_button.click()
    bench.test_search.setText('test_fixed')
    QTest.qWait(200)
    assert '4 项被当前筛选隐藏' in bench.next_step_label.text()
    assert '5 项' in bench.run_button.text()
    QTest.mouseClick(bench.run_button, Qt.MouseButton.LeftButton)
    wait_project(bench)
    report = bench.report
    assert report['counts'] == {'regression_passed': 3, 'regression_detected': 1, 'improved': 1}
    assert len(report['selected_tests']) == 5
    assert bench.results_tree.currentItem().data(0, Qt.ItemDataRole.UserRole)['status'] == 'regression_detected'
    QTest.mouseClick(bench.attention_button, Qt.MouseButton.LeftButton)
    visible = [bench.results_tree.topLevelItem(i) for i in range(bench.results_tree.topLevelItemCount())
               if not bench.results_tree.topLevelItem(i).isHidden()]
    assert len(visible) == 1
    assert visible[0].data(0, Qt.ItemDataRole.UserRole)['status'] == 'regression_detected'
    assert 'test_regression' in bench.result_details.toPlainText()
    bench.tabs.setCurrentIndex(1)
    assert bench.next_step_label.isVisible()
    assert '4 项被当前筛选隐藏' in bench.next_step_label.text()
    bundle = tmp_path / 'evidence.zip'
    pack_project_report(report['report_dir'], bundle)
    imported = unpack_project_report(bundle, tmp_path / 'imported')
    replayed = replay_project_report(imported['report_dir'], tmp_path / 'replay', confirmed=True)
    assert replayed['counts'] == report['counts']
    assert check_project_report(imported['report_dir'])['status'] == 'consistent'
    assert (repo / 'component.py').read_text() == 'def value(kind): return 999\n'


def test_collection_failure_is_visible_and_switching_python_recovers_same_frozen_inputs(window, tmp_path):
    repo = project(tmp_path, missing_dependency=True)
    bench = freeze(window, repo)
    identity = bench.state['source_sha256'].copy()
    bench.discover()
    wait_project(bench)
    assert bench.collection['status'] == 'collection_failed'
    assert not bench.problem_panel.isHidden()
    assert not bench.collection_log.isHidden()
    assert 'reprolab_missing_demo_dependency' in bench.collection_log.toPlainText()
    assert not bench.run_button.isEnabled()
    QTest.mouseClick(bench.problem_copy_button, Qt.MouseButton.LeftButton)
    assert 'reprolab_missing_demo_dependency' in QApplication.clipboard().text()
    executable, purelib = make_environment(tmp_path, True)
    (purelib / 'reprolab_missing_demo_dependency.py').write_text('VALUE = 42\n')
    bench.python_edit.setText(str(executable))
    assert bench.state['source_sha256'] == identity
    assert bench.problem_panel.isHidden()
    bench.discover()
    wait_project(bench)
    assert bench.collection['status'] == 'collected'
    assert bench.run_button.isEnabled()
    assert bench.state['source_sha256'] == identity
    bench.run_selected()
    wait_project(bench)
    assert bench.report['runtime']['executable'] == str(executable)
    assert bench.report['counts']['regression_detected'] == 1


def test_environment_check_uses_the_frozen_after_version(window, tmp_path, monkeypatch):
    import reprolab.desktop_environment as desktop_environment
    bench = freeze(window, project(tmp_path))
    seen = []
    class EnvironmentDialog(QDialog):
        result_data = {}
        runtime_values = {}
        selected_groups = []
        refresh_requested = False
        configuration_changed = False
        def __init__(self, project, *args, **kwargs):
            super().__init__(kwargs.get('parent'))
            seen.append(Path(project))
        def exec(self):
            return 0
    monkeypatch.setattr(desktop_environment, 'ProjectEnvironmentDialog', EnvironmentDialog)
    bench.inspect_environment()
    assert seen == [Path(bench.state['directory']) / 'inputs/after']
    bench.environment_dialog = None
