"""Exercise the compact replay preview and evidence-based cache recovery."""
import json
from pathlib import Path

import pytest

from test_desktop_go import wait_for
from test_desktop_project_replay import replay_preview, saved_report, start_workbench
from test_desktop_ui import window
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDoubleSpinBox, QLineEdit, QPushButton, QScrollArea, QToolButton, QWidget,
)

from reprolab import desktop_project
from reprolab.project_history import read_report_snapshot, report_reference


def _open(window, monkeypatch, folder):
    monkeypatch.setattr(desktop_project, '_checked_report',
                        lambda directory, *, control: read_report_snapshot(directory))
    bench = start_workbench(window)
    assert bench.open_project_report(folder / 'report.json')
    wait_for(lambda: bench.task is None)
    return bench


def _controls(dialog, *, expanded):
    dialog.show()
    QApplication.processEvents()
    toggle = dialog.findChild(QToolButton, 'replayOptionsToggle')
    panel = dialog.findChild(QWidget, 'replayOptionsPanel')
    check = dialog.findChild(QPushButton, 'replayCheckTools')
    start = dialog.findChild(QPushButton, 'replayStart')
    assert toggle.isChecked() is expanded
    assert panel.isVisible() is expanded
    assert check.isVisible() is expanded
    assert start.isVisible() and start.isEnabled()
    return toggle, panel, start


@pytest.mark.parametrize(('runner', 'tool'), [('go', 'go'), ('vitest', 'node'), ('vitest', 'npm')])
def test_default_replay_is_compact_and_missing_tools_point_to_the_options(window, tmp_path, monkeypatch, runner, tool):
    source = saved_report(runner, tmp_path / 'source')
    calls, dialogs = [], []

    def replay(directory, output, *, expected_report, confirmed, control, **options):
        calls.append((confirmed, expected_report, options))
        if confirmed:
            raise ValueError(f'找不到 {tool}，请安装或通过 --{tool} 指定路径。')
        return replay_preview(runner, source)

    def inspect(dialog):
        _, _, start = _controls(dialog, expanded=False)
        assert not dialog.findChild(QDoubleSpinBox, 'replayTimeout').isVisible()
        dialogs.append(dialog.objectName())
        if len(dialogs) == 1:
            dialog.reject()
        else:
            start.click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, 'replay_project_report', replay)
    monkeypatch.setattr(QDialog, 'exec', inspect)
    bench = _open(window, monkeypatch, tmp_path / 'source')
    original = getattr(bench, runner + '_report')
    button = getattr(bench, runner + '_replay_button')
    button.click()
    wait_for(lambda: len(dialogs) == 1 and not bench.busy)
    assert [call[0] for call in calls] == [False]
    assert not list((tmp_path / 'reports').glob('*/report.json'))
    button.click()
    wait_for(lambda: len(calls) == 3 and not bench.busy)
    assert [call[0] for call in calls] == [False, False, True]
    assert all(call[1] == report_reference(original) for call in calls)
    options = calls[-1][-1]
    assert options['timeout'] == source['timeout'] and options[tool] is None
    if runner == 'vitest':
        assert options == {'node': None, 'npm': None, 'offline': source['offline'],
                           'npm_cache': None, 'timeout': source['timeout'], 'verify_result': True}
    error = getattr(bench, runner + '_error_label')
    assert error.isVisible() and '重放 → 运行选项' in error.text()
    assert ('npm' if tool == 'npm' else tool.title()) + ' 路径' in error.text()
    assert getattr(bench, runner + '_report') is original


@pytest.mark.parametrize(('code', 'recover'), [('ENOTCACHED', True), ('E401', False)])
def test_vitest_replay_recovers_only_explicit_cache_misses_and_preserves_options(
    window, tmp_path, monkeypatch, code, recover,
):
    source = saved_report('vitest', tmp_path / 'source')
    source['before'].update(status='dependencies_failed', phase='dependencies', log_excerpt=f'npm error code {code}\n')
    source['after'].update(status='completed', phase='vitest')
    source['entries'][0].update(status='inconclusive', before={'status': 'not_run'},
                                after={'status': 'passed'}, case_entries=[])
    path = Path(source['report_dir']) / 'report.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    original = path.read_bytes()
    completed = saved_report('vitest', tmp_path / 'completed')
    calls, dialogs = [], []
    cache = str(tmp_path / 'prepared cache')

    def replay(directory, output, *, expected_report, confirmed, control, **options):
        calls.append((confirmed, expected_report, options))
        return completed if confirmed else replay_preview('vitest', source)

    def inspect(dialog):
        toggle, panel, start = _controls(dialog, expanded=recover)
        dialogs.append(dialog.objectName())
        if not recover:
            dialog.reject()
            return dialog.result()
        cache_edit = dialog.findChild(QLineEdit, 'replayNpmCache')
        node = dialog.findChild(QLineEdit, 'replayNodePath')
        offline = dialog.findChild(QCheckBox, 'replayOffline')
        timeout = dialog.findChild(QDoubleSpinBox, 'replayTimeout')
        assert offline.isChecked()
        cache_edit.setText(cache)
        node.setText('/local/node')
        timeout.setValue(48)
        toggle.click()
        assert not panel.isVisible() and start.isVisible()
        toggle.click()
        assert cache_edit.text() == cache and node.text() == '/local/node'
        assert timeout.value() == 48 and offline.isChecked()
        dialog.resize(370, 350)
        QApplication.processEvents()
        scroll = dialog.findChild(QScrollArea, 'replayScroll')
        assert scroll.verticalScrollBar().maximum() > 0
        assert start.isVisible() and start.mapTo(dialog, start.rect().bottomRight()).y() < dialog.height()
        assert scroll.widget().width() <= scroll.viewport().width()
        start.click()
        return dialog.result()

    monkeypatch.setattr(desktop_project, 'replay_project_report', replay)
    monkeypatch.setattr(QDialog, 'exec', inspect)
    bench = _open(window, monkeypatch, path.parent)
    original_report = bench.vitest_report
    diagnostic = bench.vitest_run_diagnostics.text()
    assert '候选' not in diagnostic
    assert bench.vitest_results_tree.topLevelItem(0).text(3) == '通过'
    if recover:
        assert '基线' in diagnostic and '未运行测试' in diagnostic
        assert bench.vitest_replay_button.text() == '调整缓存并重放…'
    else:
        assert 'npm ci 失败' in diagnostic and '未运行测试' not in diagnostic
        assert bench.vitest_replay_button.text() == '重放…'
    bench.vitest_replay_button.click()
    wait_for(lambda: dialogs and len(calls) == (2 if recover else 1) and not bench.busy and bench.replay_dialog is None)
    assert path.read_bytes() == original
    if recover:
        assert [call[0] for call in calls] == [False, True]
        assert calls[-1][1] == report_reference(original_report)
        assert calls[-1][2] == {'node': '/local/node', 'npm': None, 'timeout': 48, 'offline': True,
                                 'npm_cache': cache, 'verify_result': True}
        assert bench.vitest_report['run_id'] == completed['run_id']
        assert bench.vitest_replay_button.text() == '重放…'
    else:
        assert [call[0] for call in calls] == [False]
        assert bench.vitest_report is original_report
