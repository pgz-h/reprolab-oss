"""Exercise saved Go and Vitest reports through the Qt workbench."""
from pathlib import Path
import shutil
from threading import Event

import pytest

from test_desktop_go import saved_report as go_report, wait_for
from test_desktop_ui import window
from test_desktop_vitest import saved_report as vitest_report
from PySide6.QtWidgets import QCheckBox, QDialog, QDoubleSpinBox, QLineEdit, QPlainTextEdit, QPushButton

from reprolab import desktop_project
from reprolab.project_history import read_report_snapshot, report_reference
from reprolab.runner import RunControl


def saved_report(runner, directory):
    if runner == 'go':
        return go_report(directory)
    return vitest_report(directory, directory.parent / 'repository')


def start_workbench(window):
    bench = window.project_workbench
    window.navigation_buttons[bench].click()
    wait_for(lambda: bench.task is None)
    return bench


def replay_preview(runner, source):
    preview = {
        'kind': 'project_replay_preview', 'status': 'ready', 'runner': runner,
        'report_dir': source['report_dir'], 'selected_tests': source['selected_tests'],
        'count': len(source['selected_tests']), 'source_runtime': source['runtime'],
        'timeout': source['timeout'],
    }
    if runner == 'go':
        preview['go_package'] = source['go_package']
    else:
        preview['offline'] = source['offline']
    return preview


@pytest.mark.parametrize('runner', ['go', 'vitest'])
@pytest.mark.parametrize('route', ['open', 'import'])
def test_reopens_or_imports_and_replays_only_after_confirmation(window, tmp_path, monkeypatch, runner, route):
    source_dir = tmp_path / 'saved'
    source = saved_report(runner, source_dir)
    original = (source_dir / 'report.json').read_bytes()
    replayed = saved_report(runner, tmp_path / 'replayed')
    checks, imports, calls, opened, dialogs = [], [], [], [], []
    bundle = tmp_path / 'evidence.zip'
    bundle.touch()

    def checked(directory, *, control):
        control.check()
        checks.append((Path(directory), control))
        return read_report_snapshot(directory)

    def unpack(path, destination, *, control):
        control.check()
        imports.append((path, destination, control))
        shutil.copytree(source_dir, destination)
        return {'report_dir': str(destination), 'report_count': 1}

    def replay(directory, output, *, confirmed=False, expected_report, control, **options):
        calls.append((Path(directory), Path(output), confirmed, expected_report, control, options))
        control.check()
        return replayed if confirmed else replay_preview(runner, source)

    def respond(dialog):
        path_fields = {'go': 'replayGoPath', 'node': 'replayNodePath',
                       'npm': 'replayNpmPath', 'npm_cache': 'replayNpmCache'}
        edits = {key: dialog.findChild(QLineEdit, name) for key, name in path_fields.items()}
        offline = dialog.findChild(QCheckBox, 'replayOffline')
        timeout = dialog.findChild(QDoubleSpinBox, 'replayTimeout')
        selection = dialog.findChild(QPlainTextEdit, 'replaySelectedTests')
        dialogs.append({
            'name': dialog.objectName(), 'tools': {key: edit.text() for key, edit in edits.items() if edit},
            'offline': offline.isChecked() if offline else None, 'timeout': timeout.value(),
            'selected': selection.toPlainText().splitlines(), 'read_only': selection.isReadOnly(),
        })
        if len(dialogs) == 1:
            dialog.reject()
        else:
            for key, edit in edits.items():
                if edit:
                    edit.setText(str(tmp_path / ('local-' + key)))
            if offline:
                offline.setChecked(False)
            timeout.setValue(21.5)
            dialog.findChild(QPushButton, 'replayStart').click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, '_checked_report', checked)
    monkeypatch.setattr(desktop_project, 'unpack_project_report', unpack)
    monkeypatch.setattr(desktop_project, 'replay_project_report', replay)
    monkeypatch.setattr(desktop_project.QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    monkeypatch.setattr(QDialog, 'exec', respond)
    bench = start_workbench(window)
    if route == 'open':
        monkeypatch.setattr(desktop_project.QFileDialog, 'getOpenFileName',
                            lambda *args, **kwargs: (str(source_dir / 'report.json'), ''))
        action = next(action for action in bench.project_menu.actions() if action.text() == '打开项目报告…')
        action.trigger()
    else:
        assert bench.import_bundle(bundle)
    wait_for(lambda: bench.task is None)

    assert not calls and not dialogs and not opened
    if route == 'open':
        assert len(checks) == 1 and isinstance(checks[0][1], RunControl)
        assert not imports
        expected_directory = source_dir.resolve()
    else:
        assert len(imports) == 1 and isinstance(imports[0][2], RunControl)
        expected_directory = imports[0][1].resolve()
    report_name = runner + '_report'
    shown = getattr(bench, report_name)
    assert Path(shown['report_dir']) == expected_directory
    assert shown['run_id'] == source['run_id']
    assert bench.tabs.currentWidget() is getattr(bench, runner + '_page')
    assert bench.report is None

    replay_button = getattr(bench, runner + '_replay_button')
    assert replay_button.isEnabled()
    replay_button.click()
    wait_for(lambda: len(dialogs) == 1 and bench.task is None)
    expected = report_reference(shown)
    tools = {'go': ''} if runner == 'go' else {'node': '', 'npm': '', 'npm_cache': ''}
    assert dialogs == [{'name': 'projectReplayDialog', 'tools': tools,
                        'offline': source.get('offline'), 'timeout': source['timeout'],
                        'selected': source['selected_tests'], 'read_only': True}]
    assert [call[2] for call in calls] == [False]
    assert calls[0][3] == expected
    assert getattr(bench, report_name) == shown

    replay_button.click()
    wait_for(lambda: getattr(bench, report_name)['run_id'] == replayed['run_id'] and bench.task is None)
    assert [call[2] for call in calls] == [False, False, True]
    for directory, output, _, reference, control, _ in calls:
        assert directory == expected_directory
        assert output == tmp_path / 'reports'
        assert reference == expected
        assert isinstance(control, RunControl)
    options = calls[-1][-1]
    assert options['timeout'] == 21.5
    for tool in tools:
        assert str(options[tool]) == str(tmp_path / ('local-' + tool))
    if runner == 'vitest':
        assert options['offline'] is False
    assert (source_dir / 'report.json').read_bytes() == original
    getattr(bench, runner + '_open_button').click()
    assert opened == [str(tmp_path / 'replayed/report.html')]


def test_go_replay_can_cancel_and_retry_with_the_same_source(window, tmp_path, monkeypatch):
    source_dir = tmp_path / 'saved'
    source = saved_report('go', source_dir)
    replayed = saved_report('go', tmp_path / 'replayed')
    started, release = Event(), Event()
    controls, references = [], []

    def replay(directory, output, *, confirmed=False, expected_report, control, **options):
        references.append(expected_report)
        if not confirmed:
            return replay_preview('go', source)
        controls.append(control)
        if len(controls) == 1:
            started.set()
            if not release.wait(5):
                raise RuntimeError('timed out waiting for cancellation in test')
            control.check()
        return replayed

    def accept(dialog):
        dialog.findChild(QPushButton, 'replayStart').click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, '_checked_report', lambda directory, *, control: read_report_snapshot(directory))
    monkeypatch.setattr(desktop_project, 'replay_project_report', replay)
    monkeypatch.setattr(QDialog, 'exec', accept)
    bench = start_workbench(window)
    assert bench.open_project_report(source_dir / 'report.json')
    wait_for(lambda: bench.go_report is not None and bench.task is None)
    shown = bench.go_report
    bench.go_replay_button.click()
    try:
        wait_for(started.is_set)
        assert bench.task.control is controls[0]
        assert not bench.go_replay_button.isEnabled()
        bench.cancel_button.click()
    finally:
        release.set()
        wait_for(lambda: bench.task is None)
    assert '取消' in bench.status_label.text()
    assert bench.go_report == shown
    assert bench.go_replay_button.isEnabled()
    bench.go_replay_button.click()
    wait_for(lambda: bench.go_report['run_id'] == replayed['run_id'] and bench.task is None)
    assert references == [report_reference(shown)] * 4
    assert len(controls) == 2 and controls[0] is not controls[1]


def test_desktop_imports_series_and_locates_a_public_failure_identity(window, tmp_path):
    import subprocess
    from reprolab.project_series import pack_series
    from reprolab.project_sessions import prepare_project, run_project
    from reprolab.project_review_summary import review_summary
    from reprolab.workspace import WORKTREE

    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_value.py').write_text(
        'import pytest\nfrom flags import ready\n@pytest.mark.parametrize("n", [1, 2], ids=("private-one", "private-two"))\n'
        'def test_value(n): assert ready\n', encoding='utf-8')
    source = repo / 'flags.py'
    source.write_text('ready = True\n')
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for args in (('init', '-q'), ('add', '.'), ('commit', '-qm', 'baseline')):
        subprocess.run([*git, *args], check=True, capture_output=True)
    source.write_text('ready = False\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    public_id = review_summary(report['report_dir'])['new_failure_tests']['items'][0]['test_id']
    series = tmp_path / 'series'
    pack_series(report['report_dir'], series, locations={}, control=RunControl())
    bench = start_workbench(window)
    from test_desktop_ui import wait_idle
    assert bench.import_bundle(series / 'series.json')
    wait_idle(bench, seconds=15)
    assert bench.report['run_id'] == report['run_id']
    bench.result_search.setText(public_id)
    assert bench.result_count_label.text().startswith('显示 1 / 2 项')
    selected = bench.results_tree.currentItem()
    assert selected is not None and not selected.isHidden()
