"""Check result placement through the Go and Vitest desktop entry points."""
import json
import shutil
from threading import Event

import pytest

from test_desktop_go import wait_for
from test_desktop_project_replay import replay_preview, saved_report, start_workbench
from test_desktop_ui import window
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from reprolab import desktop_project
from reprolab.project_history import read_report_snapshot


def _workbench(window):
    window.resize(960, 680)
    window.activateWindow()
    return start_workbench(window)


def _visible(page, widget, rect=None):
    viewport = page.viewport()
    rect = widget.rect() if rect is None else rect
    return (not rect.isEmpty() and widget.isVisible()
            and viewport.rect().contains(widget.mapTo(viewport, rect.topLeft()))
            and viewport.rect().contains(widget.mapTo(viewport, rect.bottomRight())))


def _result_visible(bench, runner):
    page = getattr(bench, runner + '_page')
    summary = getattr(bench, runner + '_result_summary')
    tree = getattr(bench, runner + '_results_tree')
    actions = [getattr(bench, f'{runner}_{action}_button') for action in ('open', 'replay', 'recheck', 'export')]
    first = tree.topLevelItem(0)
    return (first is not None and all(_visible(page, field) for field in (summary, *actions))
            and _visible(page, tree.viewport(), tree.visualItemRect(first)))


def _fill_form(bench, runner, folder, report):
    page = getattr(bench, runner + '_page')
    bench.tabs.setCurrentWidget(page)
    getattr(bench, runner + '_repository_edit').setText(str(folder))
    options = getattr(bench, runner + '_run_options')
    options.set_expanded(True)
    timeout = getattr(bench, runner + '_timeout_spin')
    timeout.setValue(47)
    scope = bench.go_tests_edit if runner == 'go' else bench.vitest_files_edit
    scope.setPlainText('\n'.join(report['selected_tests']))
    QApplication.processEvents()
    return page, options, timeout, scope


@pytest.mark.parametrize('runner', ['go', 'vitest'])
def test_fresh_comparison_reveals_summary_actions_and_first_result(window, tmp_path, monkeypatch, runner):
    report = saved_report(runner, tmp_path / 'result')
    bench = _workbench(window)
    page, options, timeout, scope = _fill_form(bench, runner, tmp_path / 'repo', report)
    monkeypatch.setattr(desktop_project, f'run_{runner}_project', lambda *args, **kwargs: report)
    run = getattr(bench, runner + '_run_button')
    page.ensureWidgetVisible(run)
    run.click()
    wait_for(lambda: bench.task is None and _result_visible(bench, runner))
    assert options.toggle.isChecked() and timeout.value() == 47
    assert scope.toPlainText().splitlines() == report['selected_tests']
    page.verticalScrollBar().setValue(0)
    assert _visible(page, scope)


@pytest.mark.parametrize(('runner', 'route'), [('go', 'open'), ('vitest', 'import'), ('vitest', 'replay')])
def test_saved_report_routes_reveal_results_from_form_top(window, tmp_path, monkeypatch, runner, route):
    source_dir = tmp_path / 'source'
    source = saved_report(runner, source_dir)
    replayed = saved_report(runner, tmp_path / 'replayed')
    bench = _workbench(window)
    page, options, _, _ = _fill_form(bench, runner, tmp_path / 'repo', source)
    if route == 'replay':
        monkeypatch.setattr(desktop_project, '_checked_report',
                            lambda path, *, control: read_report_snapshot(path))
        assert bench.open_project_report(source_dir / 'report.json')
        wait_for(lambda: bench.task is None and _result_visible(bench, runner))
        monkeypatch.setattr(desktop_project, 'replay_project_report',
                            lambda *args, confirmed, **kwargs: replayed if confirmed else replay_preview(runner, source))

        def confirm(dialog):
            dialog.findChild(QPushButton, 'replayStart').click()
            return dialog.result()

        monkeypatch.setattr(QDialog, 'exec', confirm)
        page.verticalScrollBar().setValue(0)
        getattr(bench, runner + '_replay_button').click()
        expected = replayed
    else:
        # Explicit opening and importing still choose the report's page.
        other_page = bench.vitest_page if runner == 'go' else bench.go_page
        bench.tabs.setCurrentWidget(other_page)
        if route == 'open':
            monkeypatch.setattr(desktop_project, '_checked_report',
                                lambda path, *, control: read_report_snapshot(path))
            assert bench.open_project_report(source_dir / 'report.json')
        else:
            def unpack(path, destination, *, control):
                shutil.copytree(source_dir, destination)
                return {'report_dir': str(destination), 'report_count': 1}

            monkeypatch.setattr(desktop_project, 'unpack_project_report', unpack)
            assert bench.import_bundle(tmp_path / 'source.zip')
        expected = source

    def displayed():
        report = getattr(bench, runner + '_report')
        return (bench.task is None and report and report['run_id'] == expected['run_id']
                and _result_visible(bench, runner))

    wait_for(displayed)
    assert bench.tabs.currentWidget() is page and options.toggle.isChecked()


def test_open_vitest_cache_report_reveals_diagnostic_and_recovery(window, tmp_path, monkeypatch):
    folder = tmp_path / 'cache-report'
    report = saved_report('vitest', folder)
    report['before'].update(status='dependencies_failed', phase='dependencies',
                            log_excerpt='npm error code ENOTCACHED\n')
    (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    bench = _workbench(window)
    page, _, _, _ = _fill_form(bench, 'vitest', tmp_path / 'repo', report)
    monkeypatch.setattr(desktop_project, '_checked_report',
                        lambda path, *, control: read_report_snapshot(path))
    assert bench.open_project_report(folder / 'report.json')
    wait_for(lambda: bench.task is None and _result_visible(bench, 'vitest'))
    assert _visible(page, bench.vitest_run_diagnostics)
    assert '未运行测试' in bench.vitest_run_diagnostics.text()
    assert bench.vitest_replay_button.text() == '调整缓存并重放…'


@pytest.mark.parametrize('route', ['comparison', 'replay'])
def test_background_result_keeps_other_tab_and_keyboard_focus(window, tmp_path, monkeypatch, route):
    source = saved_report('go', tmp_path / 'source')
    result = saved_report('go', tmp_path / 'result')
    bench = _workbench(window)
    page, _, _, _ = _fill_form(bench, 'go', tmp_path / 'repo', source)
    started, release = Event(), Event()

    def finish(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return result

    if route == 'comparison':
        monkeypatch.setattr(desktop_project, 'run_go_project', finish)
        start = bench.go_run_button
    else:
        monkeypatch.setattr(desktop_project, '_checked_report',
                            lambda path, *, control: read_report_snapshot(path))
        assert bench.open_project_report(tmp_path / 'source/report.json')
        wait_for(lambda: bench.task is None and _result_visible(bench, 'go'))

        def replay(*args, confirmed, **kwargs):
            return finish() if confirmed else replay_preview('go', source)

        def confirm(dialog):
            dialog.findChild(QPushButton, 'replayStart').click()
            return dialog.result()

        monkeypatch.setattr(desktop_project, 'replay_project_report', replay)
        monkeypatch.setattr(QDialog, 'exec', confirm)
        start = bench.go_replay_button
    page.verticalScrollBar().setValue(0)
    start.click()
    try:
        wait_for(started.is_set)
        bench.tabs.setCurrentWidget(bench.vitest_page)
        tab_bar = bench.tabs.tabBar()
        tab_bar.setFocus()
        assert tab_bar.hasFocus()
    finally:
        release.set()
        wait_for(lambda: bench.task is None)
    assert bench.go_report['run_id'] == result['run_id']
    assert bench.tabs.currentWidget() is bench.vitest_page and tab_bar.hasFocus()
    assert page.verticalScrollBar().value() == 0


def test_recheck_summary_wins_and_replaced_report_cannot_scroll(window, tmp_path, monkeypatch):
    saved_report('go', tmp_path / 'old')
    report = saved_report('go', tmp_path / 'new')
    bench = _workbench(window)
    page, _, _, _ = _fill_form(bench, 'go', tmp_path / 'repo', report)
    comparison = {'notice': '候选间复查证据'}
    bench._show_saved_project_report(read_report_snapshot(tmp_path / 'old'), comparison=comparison)
    wait_for(lambda: _visible(page, bench.go_recheck_summary))
    scroll = page.verticalScrollBar()
    expected = min(scroll.maximum(), bench.go_recheck_summary.mapTo(page.widget(), QPoint()).y() - 12)
    assert scroll.value() == expected

    callbacks = []
    with monkeypatch.context() as patch:
        patch.setattr(desktop_project.QTimer, 'singleShot', lambda _delay, _parent, callback: callbacks.append(callback))
        bench._show_saved_project_report(read_report_snapshot(tmp_path / 'old'))
        bench._show_saved_project_report(read_report_snapshot(tmp_path / 'new'))
    QApplication.processEvents()
    scroll.setValue(0)
    stale, current = callbacks
    stale()
    assert scroll.value() == 0
    current()
    assert _result_visible(bench, 'go') and not bench.go_recheck_summary.isVisible()
