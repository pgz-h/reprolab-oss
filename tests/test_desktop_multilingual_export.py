"""Exercise current-report evidence export through the Go and Vitest controls."""
from pathlib import Path
from threading import Event

import pytest

from test_desktop_go import wait_for
from test_desktop_project_replay import saved_report, start_workbench
from test_desktop_ui import window
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from reprolab import desktop_project, project_history
from reprolab.project_history import read_report_snapshot, report_reference
from reprolab.runner import RunControl


def _show_report(window, tmp_path, monkeypatch, runner):
    directory = tmp_path / 'source'
    saved_report(runner, directory)
    monkeypatch.setattr(desktop_project, '_checked_report',
                        lambda path, *, control: read_report_snapshot(path))
    bench = start_workbench(window)
    assert bench.open_project_report(directory / 'report.json')
    wait_for(lambda: bench.task is None)
    return bench, directory


@pytest.mark.parametrize('runner', ['go', 'vitest'])
def test_export_uses_displayed_report_and_preserves_result_actions(window, tmp_path, monkeypatch, runner):
    bench, directory = _show_report(window, tmp_path, monkeypatch, runner)
    report = getattr(bench, runner + '_report')
    button = getattr(bench, runner + '_export_button')
    page = getattr(bench, runner + '_page')
    actions = [getattr(bench, f'{runner}_{action}_button')
               for action in ('open', 'replay', 'recheck', 'export', 'summary')]
    for width, height in ((1280, 840), (960, 680)):
        window.resize(width, height)
        QApplication.processEvents()
        page.ensureWidgetVisible(button)
        viewport = page.viewport()
        assert all(action.isVisible() and action.isEnabled()
                   and viewport.rect().contains(QRect(action.mapTo(viewport, action.rect().topLeft()), action.size()))
                   for action in actions)

    destination = tmp_path / 'round trip'
    calls, captions = [], []
    choices = iter(('', str(destination)))

    def choose(_parent, caption, suggested, _filter, **_options):
        captions.append((caption, suggested))
        return next(choices), ''

    def pack(source, output, *, expected_report, control):
        calls.append((source, Path(output), expected_report, control))
        return {'output': output}

    monkeypatch.setattr(desktop_project.QFileDialog, 'getSaveFileName', choose)
    monkeypatch.setattr(desktop_project, 'pack_project_report', pack)
    button.click()
    assert bench.task is None and not calls
    assert getattr(bench, runner + '_report') is report
    button.click()
    wait_for(lambda: bench.task is None)
    assert all('源码' in caption and '日志' in caption for caption, _ in captions)
    assert all(Path(suggestion).parent == directory.parent for _, suggestion in captions)
    [(source, output, reference, control)] = calls
    assert source == directory and output == destination.with_suffix('.zip')
    assert reference == report_reference(report) and isinstance(control, RunControl)
    assert '已导出证据 ZIP' in bench.status_label.text() and str(output) in bench.status_label.text()
    assert getattr(bench, runner + '_report') is report
    assert all(action.isEnabled() for action in actions)


@pytest.mark.parametrize(('failure', 'message'), [
    ('existing', '目标路径已存在'), ('inside', '目录外'), ('changed', '来源身份不符'),
])
def test_export_failure_preserves_the_report_and_can_retry(window, tmp_path, monkeypatch, failure, message):
    bench, directory = _show_report(window, tmp_path, monkeypatch, 'vitest')
    report = bench.vitest_report
    path = directory / 'report.json'
    original = path.read_bytes()
    target = (directory if failure == 'inside' else tmp_path) / 'target.zip'
    if failure == 'existing':
        target.write_bytes(b'existing file')
    monkeypatch.setattr(project_history, '_checked_report',
                        lambda path, *, control: read_report_snapshot(path))

    def choose(*_args, **_options):
        if failure == 'changed':
            path.write_bytes(original + b'\n')
        return str(target), ''

    monkeypatch.setattr(desktop_project.QFileDialog, 'getSaveFileName', choose)
    bench.vitest_export_button.click()
    wait_for(lambda: bench.task is None)
    assert bench.vitest_error_label.isVisible() and message in bench.vitest_error_label.text()
    assert '已导出证据 ZIP' not in bench.status_label.text()
    assert bench.vitest_report is report and bench.vitest_export_button.isEnabled()
    if failure == 'existing':
        assert target.read_bytes() == b'existing file'
    else:
        assert not target.exists()

    path.write_bytes(original)
    retry = tmp_path / 'retry.zip'
    monkeypatch.setattr(desktop_project.QFileDialog, 'getSaveFileName', lambda *_a, **_k: (str(retry), ''))
    monkeypatch.setattr(desktop_project, 'pack_project_report',
                        lambda directory, destination, **_options: {'output': destination})
    bench.vitest_export_button.click()
    wait_for(lambda: bench.task is None)
    assert str(retry) in bench.status_label.text()
    assert bench.vitest_error_label.isHidden() and bench.vitest_report is report


def test_cancel_export_clears_the_worker_and_leaves_the_report_visible(window, tmp_path, monkeypatch):
    bench, _ = _show_report(window, tmp_path, monkeypatch, 'go')
    report = bench.go_report
    target = tmp_path / 'cancel.zip'
    entered, release = Event(), Event()

    def pack(directory, destination, *, expected_report, control):
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test did not release export worker')
        control.check()

    monkeypatch.setattr(desktop_project.QFileDialog, 'getSaveFileName', lambda *_a, **_k: (str(target), ''))
    monkeypatch.setattr(desktop_project, 'pack_project_report', pack)
    bench.go_export_button.click()
    try:
        wait_for(entered.is_set)
        assert not bench.go_export_button.isEnabled()
        assert not bench.go_replay_button.isEnabled()
        bench.cancel_button.click()
        assert '正在取消证据导出' in bench.status_label.text()
    finally:
        release.set()
        wait_for(lambda: bench.task is None)
    assert '导出已取消' in bench.status_label.text()
    assert bench.go_report is report and not target.exists()
    assert bench.go_export_button.isEnabled() and bench.go_open_button.isEnabled()
