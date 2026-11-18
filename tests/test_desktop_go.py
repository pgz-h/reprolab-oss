"""Exercise the Go workbench through the desktop controls and Qt worker."""
import json
from threading import Event
import time

import pytest

from test_desktop_ui import window
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from reprolab import desktop_project, go_project
from reprolab.workspace import WORKTREE


def click(button):
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


def open_go_page(window):
    workbench = window.project_workbench
    click(window.navigation_buttons[workbench])
    tab = workbench.tabs.indexOf(workbench.go_page)
    assert workbench.tabs.tabText(tab) == 'Go 项目对比'
    bar = workbench.tabs.tabBar()
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(tab).center())
    assert workbench.tabs.currentWidget() is workbench.go_page
    return workbench


def saved_report(directory):
    directory.mkdir()
    (directory / 'report.html').write_text('<title>Go report</title>', encoding='utf-8')
    entries = [
        {'test_id': name, 'status': status, 'before': {'status': before}, 'after': {'status': after}}
        for name, status, before, after in (
            ('TestFixed', 'improved', 'failed', 'passed'),
            ('TestRegressed', 'regression_detected', 'passed', 'failed'),
            ('TestSkipped', 'inconclusive', 'skipped', 'skipped'),
        )
    ]
    report = {
        'kind': 'project_regression', 'schema_version': 1, 'project_id': 'sample-project',
        'run_id': directory.name,
        'report_dir': str(directory), 'repository': str(directory.parent / 'repo'),
        'title': 'cart · Go 项目回归', 'runner': 'go', 'go_package': 'internal/cart',
        'before_commit': 'a' * 40, 'after_commit': WORKTREE,
        'runtime': {'go': '/other-host/go/bin/go', 'go_version': 'go version'},
        'timeout': 120, 'selected_tests': [entry['test_id'] for entry in entries],
        'status': 'regression_detected', 'counts': {entry['status']: 1 for entry in entries},
        'before': {'status': 'completed'}, 'after': {'status': 'completed'}, 'entries': entries,
    }
    (directory / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    return report


@pytest.mark.parametrize(('after', 'package', 'toolchain'), [
    (WORKTREE, 'internal/cart', '/opt/go/bin/go'),
    ('candidate-tag', '.', None),
])
def test_go_only_runs_on_start_and_passes_form_values(window, tmp_path, monkeypatch, after, package, toolchain):
    calls = []
    report = saved_report(tmp_path / 'run')
    workbench = window.project_workbench

    def run(repository, before, candidate, output, *, package, selected, go, timeout, control):
        calls.append((repository, before, candidate, output, package, selected, go, timeout, control))
        return report

    monkeypatch.setattr(desktop_project, 'run_go_project', run)
    assert open_go_page(window) is workbench
    repository = tmp_path / 'repo'
    workbench.go_repository_edit.setText(str(repository))
    workbench.go_before_edit.setText('baseline-tag')
    workbench.go_workspace_check.setChecked(after == WORKTREE)
    assert workbench.go_after_edit.isEnabled() == (after != WORKTREE)
    workbench.go_after_edit.setText('candidate-tag')
    workbench.go_package_edit.setText(package)
    workbench.go_path_edit.setText(toolchain or '')
    workbench.go_timeout_spin.setValue(4.5)
    QApplication.processEvents()
    assert not calls
    assert workbench.task is None

    click(workbench.go_run_button)
    assert not calls
    assert '请填写至少一个顶层 Go 测试函数' in workbench.go_error_label.text()
    assert workbench.go_tests_edit.hasFocus()

    workbench.go_tests_edit.setPlainText('TestFixed\nTestRegressed TestSkipped')
    QApplication.processEvents()
    assert not calls
    click(workbench.go_run_button)
    task = workbench.task
    assert task is not None
    control = task.control
    wait_for(lambda: workbench.task is None)
    assert calls == [(repository, 'baseline-tag', after, tmp_path / 'reports', package,
                      ['TestFixed', 'TestRegressed', 'TestSkipped'], toolchain, 4.5, control)]
    assert workbench.go_run_button.isEnabled()


def test_go_discovery_fills_editable_selection_and_keeps_it_after_failure(window, tmp_path, monkeypatch):
    workbench = open_go_page(window)
    report = saved_report(tmp_path / 'run')
    workbench.go_repository_edit.setText(str(tmp_path / 'repo'))
    workbench.go_package_edit.setText('internal/cart')
    workbench.go_tests_edit.setPlainText('TestManual')
    compared = []

    def discover(repository, before, after, *, package, go, timeout, control):
        assert (repository, before, after, package) == (tmp_path / 'repo', 'HEAD', WORKTREE, 'internal/cart')
        return {'tests': ['TestFixed', 'TestRegressed']}

    def compare(*args, selected, **kwargs):
        compared.extend(selected)
        return report

    monkeypatch.setattr(desktop_project, 'list_go_tests', discover)
    monkeypatch.setattr(desktop_project, 'run_go_project', compare)
    click(workbench.go_discover_button)
    wait_for(lambda: workbench.task is None)
    assert workbench.go_tests_edit.toPlainText() == 'TestFixed\nTestRegressed'
    assert '已填入 2 项 Go 测试' in workbench.status_label.text()
    assert not compared

    def failed_discovery(*args, **kwargs):
        raise ValueError('Go 发现编译失败')

    workbench.go_tests_edit.setPlainText('TestFixed')
    monkeypatch.setattr(desktop_project, 'list_go_tests', failed_discovery)
    click(workbench.go_discover_button)
    wait_for(lambda: workbench.task is None)
    assert 'Go 发现编译失败' in workbench.go_error_label.text()
    assert workbench.go_tests_edit.toPlainText() == 'TestFixed'
    click(workbench.go_run_button)
    wait_for(lambda: workbench.task is None)
    assert compared == ['TestFixed']


def test_go_shows_report_handles_missing_toolchain_and_cancels_then_retries(window, tmp_path, monkeypatch):
    report = saved_report(tmp_path / 'run')
    retry_report = saved_report(tmp_path / 'retry')
    entered = Event()
    release = Event()

    def slow_run(*args, control, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError('timed out waiting for cancellation in test')
        control.check()

    opened = []
    monkeypatch.setattr(desktop_project.QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    monkeypatch.setattr(desktop_project, 'run_go_project', lambda *args, **kwargs: report)
    workbench = open_go_page(window)
    repository = tmp_path / 'repo'
    workbench.go_repository_edit.setText(str(repository))
    workbench.go_package_edit.setText('internal/cart')
    workbench.go_tests_edit.setPlainText('TestFixed\nTestRegressed\nTestSkipped')
    click(workbench.go_run_button)
    wait_for(lambda: workbench.task is None)

    assert workbench.report is None
    assert '检测到回归' in workbench.go_result_summary.text()
    assert 'internal/cart' in workbench.go_run_context.text()
    tree = workbench.go_results_tree
    assert [tuple(tree.topLevelItem(i).text(column) for column in range(4))
            for i in range(tree.topLevelItemCount())] == [
        ('TestFixed', '已改善', '失败', '通过'),
        ('TestRegressed', '新增失败', '通过', '失败'),
        ('TestSkipped', '受阻 / 无法判断', '跳过', '跳过'),
    ]
    click(workbench.go_open_button)
    assert opened == [str(tmp_path / 'run/report.html')]

    # Use the actual runner's toolchain check with an explicit missing executable.
    monkeypatch.setattr(go_project, 'repository_root', lambda path: path)
    monkeypatch.setattr(desktop_project, 'run_go_project', go_project.run_go_project)
    workbench.go_path_edit.setText(str(tmp_path / 'missing-go'))
    click(workbench.go_run_button)
    wait_for(lambda: workbench.task is None)
    assert '找不到 Go' in workbench.go_error_label.text()
    assert '在「Go 路径」选择可执行文件' in workbench.go_error_label.text()
    assert workbench.go_package_edit.text() == 'internal/cart'
    assert workbench.go_tests_edit.toPlainText().split() == ['TestFixed', 'TestRegressed', 'TestSkipped']
    assert workbench.go_run_button.isEnabled()

    monkeypatch.setattr(desktop_project, 'run_go_project', slow_run)
    workbench.go_path_edit.clear()
    click(workbench.go_run_button)
    try:
        wait_for(entered.is_set)
        assert '正在对比两版 Go 测试' in workbench.status_label.text()
        click(workbench.cancel_button)
        assert '正在取消' in workbench.status_label.text()
        assert not workbench.go_run_button.isEnabled()
    finally:
        release.set()
        wait_for(lambda: workbench.task is None)
    assert 'Go 对比已取消' in workbench.status_label.text()
    assert workbench.go_run_button.isEnabled()
    assert workbench.go_report['report_dir'] == report['report_dir']
    click(workbench.go_open_button)
    assert opened[-1] == str(tmp_path / 'run/report.html')

    monkeypatch.setattr(desktop_project, 'run_go_project', lambda *args, **kwargs: retry_report)
    click(workbench.go_run_button)
    wait_for(lambda: workbench.task is None)
    assert workbench.go_report['report_dir'] == retry_report['report_dir']
    assert workbench.go_error_label.isHidden()
    click(workbench.go_open_button)
    assert opened[-1] == str(tmp_path / 'retry/report.html')
