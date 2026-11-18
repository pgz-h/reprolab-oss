"""Exercise optional tool checks through the real replay controls and Qt worker."""
from pathlib import Path
from threading import Event

import pytest

from test_desktop_go import wait_for
from test_desktop_project_replay import replay_preview, saved_report, start_workbench
from test_desktop_ui import window
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QToolButton

from reprolab import desktop_project
from reprolab.project_history import read_report_snapshot, report_reference
from reprolab.runner import RunCancelled, RunControl


def _replay_workbench(window, tmp_path, monkeypatch, runner):
    directory = tmp_path / 'source'
    source = saved_report(runner, directory)
    replayed = saved_report(runner, tmp_path / 'replayed')
    calls = []

    def replay(directory, output, *, confirmed=False, expected_report, control, **options):
        control.check()
        calls.append((confirmed, expected_report, options))
        return replayed if confirmed else replay_preview(runner, source)

    monkeypatch.setattr(desktop_project, '_checked_report',
                        lambda directory, *, control: read_report_snapshot(directory))
    monkeypatch.setattr(desktop_project, 'replay_project_report', replay)
    bench = start_workbench(window)
    assert bench.open_project_report(directory / 'report.json')
    wait_for(lambda: bench.task is None and getattr(bench, runner + '_report') is not None)
    return bench, directory, replayed, calls


def _check_widgets(dialog):
    assert dialog.objectName() == 'projectReplayDialog'
    toggle = dialog.findChild(QToolButton, 'replayOptionsToggle')
    assert not toggle.isChecked()
    toggle.click()
    check = dialog.findChild(QPushButton, 'replayCheckTools')
    result = dialog.findChild(QLabel, 'replayToolCheckResult')
    start = dialog.findChild(QPushButton, 'replayStart')
    assert check.text() == '检查本机工具'
    return check, result, start


@pytest.mark.parametrize('runner', ['go', 'vitest'])
def test_replay_tool_check_recovers_and_permits_explicit_start_with_version_difference(
    window, tmp_path, monkeypatch, runner,
):
    bench, directory, replayed, calls = _replay_workbench(window, tmp_path, monkeypatch, runner)
    source = getattr(bench, runner + '_report')
    original = (directory / 'report.json').read_bytes()
    tools = ('go',) if runner == 'go' else ('node', 'npm')
    paths = {key: str(tmp_path / ('selected ' + key)) for key in tools}
    resolved = {key: str(tmp_path / ('resolved-' + key)) for key in tools}
    if runner == 'go':
        versions = {'go_version': 'go version go99.1.2 darwin/arm64'}
        missing = '找不到 Go'
        failure = '找不到 Go，请安装工具链或通过 --go 指定路径。'
    else:
        versions = {'node_version': 'v99.11.12', 'npm_version': source['runtime']['npm_version']}
        missing = '找不到 npm'
        failure = '找不到 npm，请安装它或通过 --npm 指定路径。'
    probes = []

    def probe(runner, *, go=None, node=None, npm=None, control=None):
        probes.append((runner, {'go': go, 'node': node, 'npm': npm}, control))
        if len(probes) == 1:
            raise ValueError(failure)
        return {**resolved, **versions}

    def inspect(dialog):
        dialog.show()
        check, result, start = _check_widgets(dialog)
        edits = {key: dialog.findChild(QLineEdit, 'replay' + key.title() + 'Path') for key in tools}
        assert all(not edit.text() for edit in edits.values())
        assert check.isEnabled() and start.isEnabled() and not probes

        check.click()
        wait_for(lambda: check.isEnabled() and missing in result.text())
        assert result.isVisible()
        assert '选择可执行文件' in result.text()
        assert start.isEnabled()
        assert [call[0] for call in calls] == [False]

        for key, edit in edits.items():
            edit.setText(' ' + paths[key] + ' ')
        assert missing not in result.text() or result.isHidden()
        check.click()
        wait_for(lambda: check.isEnabled() and '版本不同' in result.text())
        assert result.isVisible()
        assert all(value in result.text() for value in [*resolved.values(), *versions.values()])
        assert start.isEnabled()
        assert [call[0] for call in calls] == [False]
        assert not list((tmp_path / 'reports').glob('*/report.json'))
        assert (directory / 'report.json').read_bytes() == original
        start.click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, 'probe_replay_tools', probe)
    monkeypatch.setattr(QDialog, 'exec', inspect)
    getattr(bench, runner + '_replay_button').click()
    wait_for(lambda: bench.task is None and getattr(bench, runner + '_report')['run_id'] == replayed['run_id'])
    empty = dict.fromkeys(('go', 'node', 'npm'))
    assert [(name, options) for name, options, _ in probes] == [
        (runner, empty), (runner, {**empty, **paths}),
    ]
    assert all(isinstance(control, RunControl) for _, _, control in probes)
    assert [call[0] for call in calls] == [False, True]
    assert [call[1] for call in calls] == [report_reference(source)] * 2
    assert {key: calls[-1][2][key] for key in tools} == paths


def test_replay_tool_check_ignores_result_when_input_changed_and_restored(window, tmp_path, monkeypatch):
    bench, _, _, calls = _replay_workbench(window, tmp_path, monkeypatch, 'go')
    entered, release = Event(), Event()
    checked_paths = []
    obsolete = 'go version go77.0.0 darwin/arm64'
    fresh = 'go version go88.0.0 darwin/arm64'

    def probe(runner, *, go=None, node=None, npm=None, control=None):
        checked_paths.append(go)
        if len(checked_paths) == 1:
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release tool probe')
            return {'go': '/local/go', 'go_version': obsolete}
        return {'go': '/local/go', 'go_version': fresh}

    completed = []

    def inspect(dialog):
        dialog.show()
        check, result, start = _check_widgets(dialog)
        edit = dialog.findChild(QLineEdit, 'replayGoPath')
        edit.setText('/local/go')
        check.click()
        try:
            wait_for(entered.is_set)
            assert '正在检查本机工具' in result.text()
            assert not start.isEnabled()
            assert edit.isEnabled()
            edit.setText('/other/go')
            edit.setText('/local/go')
        finally:
            release.set()
            wait_for(check.isEnabled)
        assert obsolete not in result.text() or result.isHidden()
        check.click()
        wait_for(lambda: check.isEnabled() and fresh in result.text())
        assert obsolete not in result.text()
        assert start.isEnabled()
        dialog.reject()
        completed.append(True)
        return dialog.result()

    monkeypatch.setattr(desktop_project, 'probe_replay_tools', probe)
    monkeypatch.setattr(QDialog, 'exec', inspect)
    bench.go_replay_button.click()
    wait_for(lambda: completed and not bench.busy)
    assert checked_paths == ['/local/go'] * 2
    assert [call[0] for call in calls] == [False]
    assert bench.go_report['run_id'] == 'source'


def test_closing_replay_with_active_tool_check_cancels_and_reopen_is_clean(window, tmp_path, monkeypatch):
    bench, _, _, calls = _replay_workbench(window, tmp_path, monkeypatch, 'go')
    entered, release = Event(), Event()
    controls = []
    completed = []

    def probe(runner, *, go=None, node=None, npm=None, control=None):
        controls.append(control)
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test did not release tool probe')
        return {'go': '/stale/go', 'go_version': 'go version go77.0.0 darwin/arm64'}

    def inspect(dialog):
        dialog.show()
        check, result, start = _check_widgets(dialog)
        if not completed:
            check.click()
            try:
                wait_for(entered.is_set)
                assert not start.isEnabled()
                dialog.close()
            finally:
                release.set()
            wait_for(lambda: not dialog.isVisible())
        else:
            assert start.isEnabled()
            assert not result.text() or result.isHidden()
            assert '/stale/go' not in result.text()
            dialog.reject()
        completed.append(True)
        return dialog.result()

    monkeypatch.setattr(desktop_project, 'probe_replay_tools', probe)
    monkeypatch.setattr(QDialog, 'exec', inspect)
    bench.go_replay_button.click()
    wait_for(lambda: len(completed) == 1 and not bench.busy)
    assert len(controls) == 1
    with pytest.raises(RunCancelled):
        controls[0].check()
    assert [call[0] for call in calls] == [False]
    assert bench.go_report['run_id'] == 'source'
    bench.go_replay_button.click()
    wait_for(lambda: len(completed) == 2 and not bench.busy)
    assert len(controls) == 1
    assert [call[0] for call in calls] == [False, False]
    assert bench.go_open_button.isEnabled()


def test_go_worktree_recheck_keeps_tool_check_out_of_its_dialog(window, tmp_path, monkeypatch):
    bench, _, _, calls = _replay_workbench(window, tmp_path, monkeypatch, 'go')
    source = bench.go_report
    worktree_calls = []
    dialogs = []

    def recheck(directory, output, *, confirmed=False, **options):
        worktree_calls.append(confirmed)
        return {**replay_preview('go', source), 'repository': source['repository']}

    def inspect(dialog):
        dialogs.append((dialog.objectName(), dialog.findChild(QPushButton, 'replayCheckTools')))
        dialog.reject()
        return dialog.result()

    monkeypatch.setattr(desktop_project, 'recheck_go_project', recheck)
    monkeypatch.setattr(QDialog, 'exec', inspect)
    bench.go_recheck_button.click()
    wait_for(lambda: dialogs and not bench.busy)
    assert dialogs == [('goWorkspaceRecheckDialog', None)]
    assert worktree_calls == [False]
    assert not calls
