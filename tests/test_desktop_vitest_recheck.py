"""Exercise Vitest worktree rechecks through the project report and Qt worker."""
import json
from pathlib import Path
import shutil
from threading import Event

from test_desktop_project_replay import start_workbench
from test_desktop_ui import window
from test_desktop_vitest import saved_report, wait_for
from PySide6.QtWidgets import QCheckBox, QDialog, QDoubleSpinBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton

from reprolab import desktop_project
from reprolab.project_history import read_report_snapshot, report_reference
from reprolab.runner import RunControl


def preview(source):
    return {
        'kind': 'vitest_workspace_recheck_preview', 'status': 'ready', 'runner': 'vitest',
        'report_dir': source['report_dir'], 'repository': source['repository'],
        'selected_tests': source['selected_tests'], 'count': len(source['selected_tests']),
        'source_runtime': source['runtime'], 'timeout': source['timeout'], 'offline': source['offline'],
    }


def comparison(source):
    mixed, blocked = source['selected_tests']
    counts = {'resolved': 1, 'new_failure': 1, 'not_reviewed': 1}
    return {
        'changes': [],
        'counts': counts,
        'entries': [
            {'test_id': mixed, 'previous': 'failed', 'current': 'failed', 'changes': [], 'counts': counts,
             'case_entries': [
                 {'test_id': 'checkout > rounding', 'status': 'resolved', 'previous': 'failed', 'current': 'passed'},
                 {'test_id': 'checkout > discount', 'status': 'new_failure', 'previous': 'passed', 'current': 'failed'},
                 {'test_id': 'checkout > retries', 'status': 'not_reviewed', 'previous': 'failed',
                  'current': 'retry_passed'},
             ]},
            {'test_id': blocked, 'previous': 'inconclusive', 'current': 'inconclusive',
             'changes': ['上一轮报告缺少可核对的逐用例证据'], 'counts': {}, 'case_entries': []},
        ],
    }


def test_vitest_recheck_import_previews_full_scope_and_waits_for_confirmation(window, tmp_path, monkeypatch):
    source_dir = tmp_path / 'source'
    source = saved_report(source_dir, tmp_path / 'original repo')
    source_json = (source_dir / 'report.json').read_bytes()
    archive = tmp_path / 'vitest.zip'
    archive.touch()
    local_repository = tmp_path / 'local checkout'
    result = saved_report(tmp_path / 'new report', local_repository)
    result_file = Path(result['report_dir']) / 'report.json'
    local_node = str(tmp_path / 'node tools' / 'node')
    local_npm = str(tmp_path / 'node tools' / 'npm')
    cache = str(tmp_path / 'npm cache')
    checks, calls, dialogs, compared, opened = [], [], [], [], []

    def unpack(path, destination, *, control):
        control.check()
        assert path == archive
        shutil.copytree(source_dir, destination)
        return {'report_dir': str(destination), 'report_count': 1}

    def checked(directory, *, control):
        control.check()
        checks.append((Path(directory), control))
        return read_report_snapshot(directory)

    def recheck(directory, output, *, expected_report, confirmed=False, control, **options):
        control.check()
        calls.append((Path(directory), Path(output), expected_report, confirmed, control, options))
        if confirmed:
            return {'report': result, 'comparison': comparison(source)}
        return preview(read_report_snapshot(directory))

    def compare(original, current):
        compared.append((original['run_id'], current['run_id']))
        return comparison(original)

    def respond(dialog):
        fields = {key: dialog.findChild(QLineEdit, 'vitestRecheck' + name) for key, name in
                  [('repository', 'Repository'), ('node', 'NodePath'), ('npm', 'NpmPath'), ('cache', 'NpmCache')]}
        selected = dialog.findChild(QPlainTextEdit, 'vitestRecheckSelectedTests')
        offline = dialog.findChild(QCheckBox, 'vitestRecheckOffline')
        timeout = dialog.findChild(QDoubleSpinBox, 'vitestRecheckTimeout')
        dialogs.append((dialog.objectName(), {key: edit.text() for key, edit in fields.items()},
                        selected.toPlainText().splitlines(), selected.isReadOnly(), offline.isChecked(),
                        timeout.value(), '\n'.join(label.text() for label in dialog.findChildren(QLabel))))
        if len(dialogs) == 1:
            dialog.reject()
        else:
            next(button for button in dialog.findChildren(QPushButton) if button.text() == '选择 Git 仓库…').click()
            assert fields['repository'].text() == str(local_repository)
            fields['node'].setText(local_node)
            fields['npm'].setText(local_npm)
            fields['cache'].setText(cache)
            offline.setChecked(False)
            timeout.setValue(51.5)
            dialog.findChild(QPushButton, 'vitestRecheckStart').click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, 'unpack_project_report', unpack)
    monkeypatch.setattr(desktop_project, '_checked_report', checked)
    monkeypatch.setattr(desktop_project, 'recheck_vitest_project', recheck)
    monkeypatch.setattr(desktop_project, 'compare_vitest_recheck', compare)
    monkeypatch.setattr(desktop_project.QFileDialog, 'getExistingDirectory', lambda *args: str(local_repository))
    monkeypatch.setattr(desktop_project.QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    monkeypatch.setattr(QDialog, 'exec', respond)
    bench = start_workbench(window)
    assert bench.import_bundle(archive)
    wait_for(lambda: bench.vitest_report is not None and bench.task is None)
    shown = bench.vitest_report
    reference = report_reference(shown)
    imported = Path(shown['report_dir'])
    result['review_source'] = {**reference, 'selection_mode': 'all', 'selected_tests': shown['selected_tests']}
    result_file.write_text(json.dumps(result), encoding='utf-8')
    assert bench.tabs.currentWidget() is bench.vitest_page
    assert bench.vitest_recheck_button.text() == '用当前工作区复查…'
    assert bench.vitest_recheck_button.isEnabled()
    assert not calls and not dialogs

    bench.vitest_recheck_button.click()
    wait_for(lambda: len(dialogs) == 1 and bench.task is None)
    assert [call[3] for call in calls] == [False]
    assert bench.vitest_report == shown
    assert not bench.vitest_recheck_panel.isVisible()

    bench.vitest_recheck_button.click()
    wait_for(lambda: bench.vitest_report['run_id'] == result['run_id'] and bench.task is None)
    assert [call[3] for call in calls] == [False, False, True]
    for directory, output, expected, _, control, _ in calls:
        assert (directory, output, expected) == (imported, tmp_path / 'reports', reference)
        assert isinstance(control, RunControl)
    assert calls[-1][-1] == {
        'repository': str(local_repository), 'node': local_node, 'npm': local_npm,
        'npm_cache': cache, 'offline': False, 'timeout': 51.5,
    }
    for name, paths, selected, read_only, offline, timeout, labels in dialogs:
        assert name == 'vitestWorkspaceRecheckDialog'
        assert paths == {'repository': shown['repository'], 'node': '', 'npm': '', 'cache': ''}
        assert selected == shown['selected_tests'] and read_only
        assert offline is True and timeout == shown['timeout']
        assert shown['before_commit'] in labels
        assert str(imported) in labels

    assert bench.vitest_recheck_panel.isVisible()
    tree = bench.vitest_recheck_tree
    first = tree.topLevelItem(0)
    assert first.text(0) == shown['selected_tests'][0] and first.isExpanded()
    assert [tuple(first.child(i).text(column) for column in range(2)) for i in range(first.childCount())] == [
        ('checkout > rounding', '已解决'), ('checkout > discount', '新增失败'), ('checkout > retries', '未能复查'),
    ]
    second = tree.topLevelItem(1)
    assert second.text(0) == shown['selected_tests'][1] and second.childCount() == 0
    assert '未能复查' in second.text(1)
    bench.vitest_open_button.click()
    assert opened == [str(result_file.with_name('report.html'))]
    assert (imported / 'report.json').read_bytes() == source_json

    assert bench.open_project_report(imported / 'report.json')
    wait_for(lambda: bench.vitest_report['run_id'] == source['run_id'] and bench.task is None)
    assert not bench.vitest_recheck_panel.isVisible()
    assert bench.open_project_report(result_file)
    wait_for(lambda: bench.vitest_report['run_id'] == result['run_id'] and bench.task is None)
    assert compared == [(source['run_id'], result['run_id'])]
    assert [directory for directory, _ in checks[-2:]] == [result_file.parent, imported]
    assert checks[-2][1] is checks[-1][1]
    assert [call[3] for call in calls] == [False, False, True]
    assert bench.vitest_recheck_panel.isVisible()


def test_vitest_recheck_can_cancel_and_retry_without_replacing_source(window, tmp_path, monkeypatch):
    source_dir = tmp_path / 'source'
    source = saved_report(source_dir, tmp_path / 'repo')
    result = saved_report(tmp_path / 'next-run', tmp_path / 'repo')
    started, release = Event(), Event()
    references, attempts = [], []

    def recheck(directory, output, *, expected_report, confirmed=False, control, **options):
        references.append(expected_report)
        if not confirmed:
            return preview(source)
        attempts.append((control, options))
        if len(attempts) == 1:
            started.set()
            if not release.wait(5):
                raise RuntimeError('timed out waiting for cancellation in test')
            control.check()
        return {'report': result, 'comparison': comparison(source)}

    def accept(dialog):
        dialog.findChild(QPushButton, 'vitestRecheckStart').click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, '_checked_report', lambda directory, *, control: read_report_snapshot(directory))
    monkeypatch.setattr(desktop_project, 'recheck_vitest_project', recheck)
    monkeypatch.setattr(QDialog, 'exec', accept)
    bench = start_workbench(window)
    assert bench.open_project_report(source_dir / 'report.json')
    wait_for(lambda: bench.vitest_report is not None and bench.task is None)
    shown = bench.vitest_report

    bench.vitest_recheck_button.click()
    try:
        wait_for(started.is_set)
        assert bench.task.control is attempts[0][0]
        assert not bench.vitest_recheck_button.isEnabled()
        bench.cancel_button.click()
    finally:
        release.set()
        wait_for(lambda: bench.task is None)
    assert '取消' in bench.status_label.text()
    assert bench.vitest_report == shown
    assert bench.vitest_recheck_button.isEnabled()

    bench.vitest_recheck_button.click()
    wait_for(lambda: bench.vitest_report['run_id'] == result['run_id'] and bench.task is None)
    assert references == [report_reference(shown)] * 4
    assert len(attempts) == 2 and attempts[0][0] is not attempts[1][0]
    for _, options in attempts:
        assert options == {'repository': source['repository'], 'node': None, 'npm': None,
                           'npm_cache': None, 'offline': source['offline'], 'timeout': source['timeout']}
    assert bench.vitest_recheck_panel.isVisible()
