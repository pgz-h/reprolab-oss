"""Exercise Go worktree rechecks through the project report and Qt worker."""
import json
from pathlib import Path
from threading import Event

from test_desktop_go import saved_report, wait_for
from test_desktop_project_replay import start_workbench
from test_desktop_ui import window
from PySide6.QtWidgets import QDialog, QDoubleSpinBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton

from reprolab import desktop_project
from reprolab.project_history import read_report_snapshot, report_reference
from reprolab.runner import RunControl


def preview(source):
    return {'kind': 'go_recheck_preview', 'status': 'ready', 'runner': 'go',
            'report_dir': source['report_dir'], 'repository': source['repository'],
            'before_commit': source['before_commit'], 'selected_tests': source['selected_tests'],
            'count': len(source['selected_tests']), 'go_package': source['go_package'],
            'source_runtime': source['runtime'], 'timeout': source['timeout']}


def comparison(*, changes=()):
    conditions = 'conditions_changed' if changes else None
    entries = [
        {'test_id': 'TestFixed', 'status': conditions or 'still_passed', 'previous': 'passed', 'current': 'passed'},
        {'test_id': 'TestRegressed', 'status': conditions or 'resolved', 'previous': 'failed', 'current': 'passed'},
        {'test_id': 'TestSkipped', 'status': 'not_reviewed', 'previous': 'skipped', 'current': 'missing'},
    ]
    counts = {}
    for entry in entries:
        status = entry['status']
        counts[status] = counts.get(status, 0) + 1
    return {'changes': list(changes), 'entries': entries, 'counts': counts}


def test_go_recheck_preview_uses_source_scope_and_waits_for_confirmation(window, tmp_path, monkeypatch):
    source_dir = tmp_path / 'source'
    source = saved_report(source_dir)
    original = (source_dir / 'report.json').read_bytes()
    result = saved_report(tmp_path / 'next-run')
    result_path = Path(result['report_dir']) / 'report.json'
    result['review_source'] = report_reference(read_report_snapshot(source_dir))
    result_path.write_text(json.dumps(result), encoding='utf-8')
    local_repository = tmp_path / 'local checkout'
    local_go = tmp_path / 'toolchain' / 'go'
    checks, calls, dialogs, opened, compared, browsed = [], [], [], [], [], []

    def checked(directory, *, control):
        checks.append((Path(directory), control))
        return read_report_snapshot(directory)

    def compare(source, current):
        compared.append((source['run_id'], current['run_id']))
        return comparison()

    def recheck(directory, output, *, expected_report, confirmed=False, control, **options):
        calls.append((Path(directory), Path(output), expected_report, confirmed, control, options))
        if confirmed:
            return {'report': result, 'comparison': comparison()}
        return preview(source)

    def respond(dialog):
        repository = dialog.findChild(QLineEdit, 'goRecheckRepository')
        tool = dialog.findChild(QLineEdit, 'goRecheckGoPath')
        timeout = dialog.findChild(QDoubleSpinBox, 'goRecheckTimeout')
        selected = dialog.findChild(QPlainTextEdit, 'goRecheckSelectedTests')
        labels = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
        dialogs.append((dialog.objectName(), repository.text(), tool.text(), timeout.value(),
                        selected.toPlainText().splitlines(), selected.isReadOnly(), labels))
        if len(dialogs) == 1:
            dialog.reject()
        else:
            choose = next(button for button in dialog.findChildren(QPushButton) if button.text() == '选择 Git 仓库…')
            choose.click()
            assert repository.text() == str(local_repository)
            tool.setText(str(local_go))
            timeout.setValue(31.5)
            dialog.findChild(QPushButton, 'goRecheckStart').click()
        return dialog.result()

    def choose_repository(_parent, _title, directory):
        browsed.append(directory)
        return str(local_repository)

    monkeypatch.setattr(desktop_project, '_checked_report', checked)
    monkeypatch.setattr(desktop_project, 'compare_go_recheck', compare)
    monkeypatch.setattr(desktop_project, 'recheck_go_project', recheck)
    monkeypatch.setattr(desktop_project.QFileDialog, 'getExistingDirectory', choose_repository)
    monkeypatch.setattr(desktop_project.QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    monkeypatch.setattr(QDialog, 'exec', respond)
    bench = start_workbench(window)
    assert bench.open_project_report(source_dir / 'report.json')
    wait_for(lambda: bench.go_report is not None and bench.task is None)
    shown = bench.go_report
    reference = report_reference(shown)
    assert bench.tabs.currentWidget() is bench.go_page
    assert bench.go_recheck_button.text() == '用当前工作区复查…'
    assert bench.go_recheck_button.isEnabled()
    assert len(checks) == 1 and checks[0][0] == source_dir and isinstance(checks[0][1], RunControl)
    assert not calls and not dialogs

    bench.go_recheck_button.click()
    wait_for(lambda: len(dialogs) == 1 and bench.task is None)
    assert [call[3] for call in calls] == [False]
    assert bench.go_report == shown
    assert not bench.go_recheck_panel.isVisible()

    bench.go_recheck_button.click()
    wait_for(lambda: bench.go_report['run_id'] == result['run_id'] and bench.task is None)
    assert [call[3] for call in calls] == [False, False, True]
    for directory, output, expected, _, control, _ in calls:
        assert (directory, output, expected) == (source_dir, tmp_path / 'reports', reference)
        assert isinstance(control, RunControl)
    for dialog_name, repository, tool, timeout, selected, read_only, labels in dialogs:
        assert dialog_name == 'goWorkspaceRecheckDialog'
        assert repository == source['repository']
        assert tool == ''
        assert timeout == source['timeout']
        assert selected == source['selected_tests'] and read_only
        assert source['before_commit'] in labels
        assert source['go_package'] in labels
    options = calls[-1][-1]
    assert Path(options['repository']) == local_repository
    assert Path(options['go']) == local_go
    assert options['timeout'] == 31.5
    assert browsed == [source['repository']]

    assert bench.go_recheck_panel.isVisible()
    tree = bench.go_recheck_tree
    assert [tuple(tree.topLevelItem(i).text(column) for column in range(4))
            for i in range(tree.topLevelItemCount())] == [
        ('TestFixed', '继续通过', '通过', '通过'),
        ('TestRegressed', '已解决', '失败', '通过'),
        ('TestSkipped', '未能复查', '跳过', '缺失'),
    ]
    bench.go_open_button.click()
    assert opened == [str(tmp_path / 'next-run/report.html')]
    assert (source_dir / 'report.json').read_bytes() == original

    assert bench.open_project_report(source_dir / 'report.json')
    wait_for(lambda: bench.go_report['run_id'] == source['run_id'] and bench.task is None)
    assert not bench.go_recheck_panel.isVisible()
    assert bench.go_recheck_button.isEnabled()

    assert bench.open_project_report(result_path)
    wait_for(lambda: bench.go_report['run_id'] == result['run_id'] and bench.task is None)
    assert compared == [(source['run_id'], result['run_id'])]
    assert [directory for directory, _ in checks[-2:]] == [result_path.parent, source_dir]
    assert checks[-2][1] is checks[-1][1]
    assert [call[3] for call in calls] == [False, False, True]
    assert bench.go_recheck_panel.isVisible()


def test_go_recheck_can_cancel_and_retry_without_replacing_source(window, tmp_path, monkeypatch):
    source_dir = tmp_path / 'source'
    source = saved_report(source_dir)
    result = saved_report(tmp_path / 'next-run')
    started, release = Event(), Event()
    references, attempts = [], []
    changes = ['共享测试文件内容已变化', '每版限时已变化']

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
        return {'report': result, 'comparison': comparison(changes=changes)}

    def accept(dialog):
        dialog.findChild(QPushButton, 'goRecheckStart').click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, '_checked_report', lambda directory, *, control: read_report_snapshot(directory))
    monkeypatch.setattr(desktop_project, 'recheck_go_project', recheck)
    monkeypatch.setattr(QDialog, 'exec', accept)
    bench = start_workbench(window)
    assert bench.open_project_report(source_dir / 'report.json')
    wait_for(lambda: bench.go_report is not None and bench.task is None)
    shown = bench.go_report

    bench.go_recheck_button.click()
    try:
        wait_for(started.is_set)
        assert bench.task.control is attempts[0][0]
        assert not bench.go_recheck_button.isEnabled()
        bench.cancel_button.click()
    finally:
        release.set()
        wait_for(lambda: bench.task is None)
    assert '取消' in bench.status_label.text()
    assert bench.go_report == shown
    assert bench.go_recheck_button.isEnabled()

    bench.go_recheck_button.click()
    wait_for(lambda: bench.go_report['run_id'] == result['run_id'] and bench.task is None)
    assert references == [report_reference(shown)] * 4
    assert len(attempts) == 2 and attempts[0][0] is not attempts[1][0]
    for _, options in attempts:
        assert Path(options['repository']) == Path(source['repository'])
        assert options.get('go') is None and options['timeout'] == source['timeout']
    assert bench.go_recheck_panel.isVisible()
    assert all(change in bench.go_recheck_summary.text() for change in changes)
    tree = bench.go_recheck_tree
    assert [tree.topLevelItem(row).text(1) for row in range(tree.topLevelItemCount())] == [
        '检查条件变化', '检查条件变化', '未能复查',
    ]
