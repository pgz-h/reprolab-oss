"""Exercise default and adjusted runtime options through the comparison forms."""
import pytest

from test_desktop_go import wait_for
from test_desktop_project_replay import saved_report, start_workbench
from test_desktop_ui import window
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from reprolab import desktop_project


def _comparison(window, tmp_path, runner):
    bench = start_workbench(window)
    page = getattr(bench, runner + '_page')
    bench.tabs.setCurrentWidget(page)
    report = saved_report(runner, tmp_path / 'result')
    getattr(bench, runner + '_repository_edit').setText(str(tmp_path / 'repo'))
    getattr(bench, runner + '_before_edit').setText('base')
    getattr(bench, runner + '_workspace_check').setChecked(False)
    getattr(bench, runner + '_after_edit').setText('candidate')
    if runner == 'go':
        bench.go_package_edit.setText('internal/cart')
        scope = bench.go_tests_edit
    else:
        scope = bench.vitest_files_edit
    scope.setPlainText('\n'.join(report['selected_tests']))
    return bench, page, report, scope


@pytest.mark.parametrize('runner', ['go', 'vitest'])
def test_comparison_uses_defaults_and_retains_adjustments_when_collapsed(window, tmp_path, monkeypatch, runner):
    bench, page, report, scope = _comparison(window, tmp_path, runner)
    run = getattr(bench, runner + '_run_button')
    options = getattr(bench, runner + '_run_options')
    timeout = getattr(bench, runner + '_timeout_spin')
    if runner == 'go':
        paths = {'go': bench.go_path_edit}
        defaults = {'go': None, 'timeout': 120}
    else:
        paths = {'node': bench.vitest_node_edit, 'npm': bench.vitest_npm_edit, 'npm_cache': bench.vitest_cache_edit}
        defaults = {'node': None, 'npm': None, 'npm_cache': None, 'offline': False, 'timeout': 120}

    calls = []

    def compare(*args, **kwargs):
        calls.append((args, kwargs))
        return report

    monkeypatch.setattr(desktop_project, f'run_{runner}_project', compare)
    window.resize(960, 680)
    QApplication.processEvents()
    assert options.panel.isHidden() and options.toggle.text() == '运行选项'
    viewport = page.viewport()
    assert all(viewport.rect().contains(QRect(field.mapTo(viewport, field.rect().topLeft()), field.size()))
               for field in (scope, run, options.toggle))
    assert run.isEnabled() and not calls
    run.click()
    wait_for(lambda: bench.task is None)
    assert {key: calls[0][1][key] for key in defaults} == defaults

    options.toggle.click()
    assert options.panel.isVisible()
    adjusted = {key: str(tmp_path / ('custom ' + key)) for key in paths}
    for key, field in paths.items():
        field.setText(adjusted[key])
    timeout.setValue(47)
    adjusted['timeout'] = 47
    if runner == 'vitest':
        bench.vitest_offline_check.setChecked(True)
        adjusted['offline'] = True
    options.toggle.click()
    assert options.panel.isHidden() and '已调整' in options.toggle.text()
    options.toggle.click()
    assert {key: field.text() for key, field in paths.items()} == {key: adjusted[key] for key in paths}
    assert timeout.value() == 47
    if runner == 'vitest':
        assert bench.vitest_offline_check.isChecked()
    options.toggle.click()
    assert len(calls) == 1
    run.click()
    wait_for(lambda: bench.task is None)
    assert {key: calls[1][1][key] for key in adjusted} == adjusted
    assert calls[0][0] == calls[1][0]
    assert options.panel.isHidden() and '已调整' in options.toggle.text()
    for field in paths.values():
        field.clear()
    timeout.setValue(120)
    if runner == 'vitest':
        bench.vitest_offline_check.setChecked(False)
    assert options.toggle.text() == '运行选项'


@pytest.mark.parametrize(('runner', 'tool', 'message'), [
    ('go', 'go', '找不到 Go，请安装工具链或通过 --go 指定路径。'),
    ('vitest', 'node', '找不到 node，请安装它或通过 --node 指定路径。'),
    ('vitest', 'npm', '找不到 npm，请安装它或通过 --npm 指定路径。'),
])
def test_missing_comparison_tool_expands_and_focuses_its_path_without_changing_scope(
    window, tmp_path, monkeypatch, runner, tool, message,
):
    bench, page, report, scope = _comparison(window, tmp_path, runner)
    options = getattr(bench, runner + '_run_options')
    run = getattr(bench, runner + '_run_button')
    error = getattr(bench, runner + '_error_label')
    if runner == 'go':
        edit = bench.go_path_edit
    else:
        edit = getattr(bench, f'vitest_{tool}_edit')
    calls = []

    def compare(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise ValueError(message)
        return report

    monkeypatch.setattr(desktop_project, f'run_{runner}_project', compare)
    failed = getattr(bench, f'_{runner}_failed')
    failed('无法启动工具。')
    assert options.panel.isHidden()
    for action in ('重放', '复查', '导出'):
        failed(message, action=action)
        assert options.panel.isHidden()

    window.resize(960, 680)
    window.activateWindow()
    bad_path = str(tmp_path / f'missing-{tool}')
    edit.setText(bad_path)
    run.click()
    wait_for(lambda: bench.task is None and edit.hasFocus())
    assert options.panel.isVisible() and error.isVisible()
    assert '找不到' in error.text() and '选择可执行文件' in error.text()
    viewport = page.viewport()
    assert viewport.rect().contains(QRect(edit.mapTo(viewport, edit.rect().topLeft()), edit.size()))
    assert edit.text() == bad_path
    assert scope.toPlainText().splitlines() == report['selected_tests']
    assert getattr(bench, runner + '_report') is None and run.isEnabled()

    new_path = str(tmp_path / f'local-{tool}')
    edit.setText(new_path)
    options.toggle.click()
    run.click()
    wait_for(lambda: bench.task is None)
    assert calls[0][1][tool] == bad_path and calls[1][1][tool] == new_path
    assert calls[0][0] == calls[1][0]
    assert getattr(bench, runner + '_report')['run_id'] == report['run_id']
    assert error.isHidden() and options.panel.isHidden()
