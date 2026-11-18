"""Exercise Vitest's project workbench with the actual Qt task lifecycle."""
from pathlib import Path
import json
import subprocess
from threading import Event
import time

import pytest

from test_desktop_ui import window
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from reprolab import desktop_project, vitest_project
from reprolab.workspace import WORKTREE


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


def open_vitest_page(window):
    bench = window.project_workbench
    QTest.mouseClick(window.navigation_buttons[bench], Qt.MouseButton.LeftButton)
    tab = bench.tabs.indexOf(bench.vitest_page)
    assert 'Vitest' in bench.tabs.tabText(tab)
    bar = bench.tabs.tabBar()
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(tab).center())
    assert bench.tabs.currentWidget() is bench.vitest_page
    return bench


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'sample repo'
    root.mkdir()
    subprocess.run(['git', '-C', str(root), 'init', '--quiet'], check=True, capture_output=True)
    (root / 'tests').mkdir()
    for name in ('order totals.test.ts', 'format.test.js'):
        (root / 'tests' / name).touch()
    return root


def result(test_id, status, before, after, **extra):
    return {'test_id': test_id, 'status': status, 'before': {'status': before},
            'after': {'status': after}, **extra}


def saved_report(directory, repository):
    directory.mkdir()
    (directory / 'report.html').write_text('<title>Vitest report</title>', encoding='utf-8')
    report = {
        'kind': 'project_regression', 'schema_version': 1, 'project_id': 'sample-project',
        'run_id': directory.name,
        'report_dir': str(directory), 'repository': str(repository),
        'title': 'sample repo · Vitest 项目回归', 'runner': 'vitest', 'vitest_report_version': 2,
        'before_commit': 'a' * 40, 'after_commit': WORKTREE,
        'runtime': {'node': '/opt/node/bin/node', 'node_version': 'v22.0.0',
                    'npm': '/opt/node/bin/npm', 'npm_version': '10.0.0'},
        'offline': True, 'timeout': 120, 'vitest_versions': {'before': '4.1.11', 'after': '4.1.11'},
        'status': 'regression_detected', 'counts': {'regression_detected': 1, 'inconclusive': 1},
        'case_counts': {'improved': 1, 'regression_detected': 1},
        'before': {'status': 'completed', 'phase': 'vitest', 'vitest_version': '4.1.11'},
        'after': {'status': 'completed', 'phase': 'vitest', 'vitest_version': '4.1.11'},
        'selected_tests': ['tests/order totals.test.ts', 'tests/format.test.js'],
        'entries': [
            result('tests/order totals.test.ts', 'regression_detected', 'failed', 'failed', case_entries=[
                result('checkout > rounding', 'improved', 'failed', 'passed'),
                result('checkout > discount', 'regression_detected', 'passed', 'failed'),
            ]),
            result('tests/format.test.js', 'inconclusive', 'inconclusive', 'inconclusive',
                   reason='两版实际收集的测试名称不同。', case_entries=[]),
        ],
    }
    (directory / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    return report


@pytest.mark.parametrize(('candidate', 'offline'), [('candidate-tag', True), (WORKTREE, False)])
def test_vitest_only_runs_on_start_and_preserves_relative_file_paths(
    window, tmp_path, monkeypatch, repository, candidate, offline,
):
    calls = []
    report = saved_report(tmp_path / 'run', repository)

    def run(repo, before, after, files, output, *, project_dir, node, npm, timeout, offline, npm_cache, control):
        calls.append((Path(repo), before, after, files, Path(output), project_dir,
                      node, npm, timeout, offline, npm_cache, control))
        return report

    monkeypatch.setattr(desktop_project, 'run_vitest_project', run)
    bench = open_vitest_page(window)
    bench.vitest_repository_edit.setText(str(repository))
    bench.vitest_before_edit.setText('baseline-tag')
    bench.vitest_workspace_check.setChecked(candidate == WORKTREE)
    assert bench.vitest_after_edit.isEnabled() == (candidate != WORKTREE)
    bench.vitest_after_edit.setText('candidate-tag')
    project_dir = 'web client' if offline else '.'
    bench.vitest_project_dir_edit.setText(project_dir)
    if offline:
        (repository / project_dir).mkdir()
        (repository / 'tests').rename(repository / project_dir / 'tests')
    node = str(tmp_path / 'node tools' / 'node') if offline else None
    npm = str(tmp_path / 'node tools' / 'npm') if offline else None
    cache = str(tmp_path / 'npm cache') if offline else None
    bench.vitest_node_edit.setText(node or '')
    bench.vitest_npm_edit.setText(npm or '')
    bench.vitest_offline_check.setChecked(offline)
    bench.vitest_cache_edit.setText(cache or '')
    bench.vitest_timeout_spin.setValue(45)
    QApplication.processEvents()
    assert not calls
    assert bench.task is None

    bench.vitest_run_button.click()
    assert bench.vitest_error_label.isVisible()
    assert not calls
    assert bench.task is None

    chosen = [(Path(project_dir) / 'tests' / name).as_posix()
              for name in ('order totals.test.ts', 'format.test.js')]
    selection = [str(repository / name) for name in chosen]

    def choose_files(_, title, directory, filters):
        assert Path(directory) == repository / project_dir
        return selection, ''

    monkeypatch.setattr(desktop_project.QFileDialog, 'getOpenFileNames', choose_files)
    bench.vitest_files_button.click()
    assert bench.vitest_files_edit.toPlainText().splitlines() == chosen

    outside = tmp_path / 'outside.test.js'
    outside.touch()
    selection.append(str(outside))
    bench.vitest_files_button.click()
    assert bench.vitest_error_label.isVisible()
    assert bench.vitest_files_edit.toPlainText().splitlines() == chosen

    extra = (Path(project_dir) / 'tests/extra check.test.mts').as_posix()
    bench.vitest_files_edit.setPlainText('\n'.join([*chosen, '', extra]))
    QApplication.processEvents()
    assert not calls
    assert bench.task is None
    bench.vitest_run_button.click()
    assert bench.task is not None
    control = bench.task.control
    wait_for(lambda: bench.task is None)
    assert calls == [(repository, 'baseline-tag', candidate, [*chosen, extra],
                      tmp_path / 'reports', project_dir, node, npm, 45, offline, cache, control)]
    assert bench.vitest_run_button.isEnabled()


def test_vitest_discovery_fills_editable_files_and_keeps_them_on_failure(window, tmp_path, monkeypatch, repository):
    bench = open_vitest_page(window)
    bench.vitest_repository_edit.setText(str(repository))
    bench.vitest_offline_check.setChecked(True)
    bench.vitest_files_edit.setPlainText('tests/manual.test.js')
    report = saved_report(tmp_path / 'run', repository)
    discovered = report['selected_tests']
    compared = []

    def discover(repo, before, after, *, project_dir, offline, **options):
        assert (repo, before, after, project_dir, offline) == (repository, 'HEAD', WORKTREE, '.', True)
        return {'files': discovered}

    def run(repo, before, after, files, *args, **options):
        compared.extend(files)
        return report

    monkeypatch.setattr(desktop_project, 'list_vitest_files', discover)
    monkeypatch.setattr(desktop_project, 'run_vitest_project', run)
    bench.vitest_discover_button.click()
    wait_for(lambda: bench.task is None)
    assert bench.vitest_files_edit.toPlainText().splitlines() == discovered
    assert '已填入 2 个 Vitest 测试文件' in bench.status_label.text()
    assert not compared

    def unavailable(*args, **kwargs):
        raise ValueError('Vitest 配置加载失败')

    bench.vitest_files_edit.setPlainText(discovered[0])
    monkeypatch.setattr(desktop_project, 'list_vitest_files', unavailable)
    bench.vitest_discover_button.click()
    wait_for(lambda: bench.task is None)
    assert bench.vitest_files_edit.toPlainText() == discovered[0]
    assert 'Vitest 配置加载失败' in bench.vitest_error_label.text()
    bench.vitest_run_button.click()
    wait_for(lambda: bench.task is None)
    assert compared == discovered[:1]


def test_vitest_displays_mixed_cases_and_recovers_after_error_and_cancel(window, tmp_path, monkeypatch, repository):
    report = saved_report(tmp_path / 'run', repository)
    retry = saved_report(tmp_path / 'retry', repository)
    opened = []
    monkeypatch.setattr(desktop_project.QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    monkeypatch.setattr(desktop_project, 'run_vitest_project', lambda *a, **kw: report)
    bench = open_vitest_page(window)
    bench.vitest_repository_edit.setText(str(repository))
    bench.vitest_files_edit.setPlainText('\n'.join(report['selected_tests']))
    bench.vitest_run_button.click()
    wait_for(lambda: bench.task is None)

    summary = bench.vitest_result_summary.text()
    assert all(text in summary for text in ('检测到回归', '文件', '用例', '已改善', '新增失败'))
    tree = bench.vitest_results_tree
    assert tree.topLevelItemCount() == 2
    mixed = tree.topLevelItem(0)
    assert mixed.text(0) == report['selected_tests'][0]
    assert mixed.text(1) == '已改善 1 · 新增失败 1'
    assert not mixed.text(2) and not mixed.text(3)
    assert mixed.isExpanded() and mixed.childCount() == 2
    assert [tuple(mixed.child(row).text(column) for column in range(4)) for row in range(2)] == [
        ('checkout > rounding', '已改善', '失败', '通过'),
        ('checkout > discount', '新增失败', '通过', '失败'),
    ]
    blocked = tree.topLevelItem(1)
    assert blocked.text(1) == '受阻 / 无法判断'
    assert blocked.childCount() == 0
    bench.vitest_open_button.click()
    assert opened == [str(tmp_path / 'run/report.html')]

    monkeypatch.setattr(desktop_project, 'run_vitest_project', vitest_project.run_vitest_project)
    bench.vitest_node_edit.setText(str(tmp_path / 'missing-node'))
    bench.vitest_run_button.click()
    wait_for(lambda: bench.task is None)
    assert '找不到 node' in bench.vitest_error_label.text()
    assert 'Vitest 对比未完成' in bench.status_label.text()
    assert bench.vitest_run_button.isEnabled()
    assert bench.vitest_files_edit.toPlainText().splitlines() == report['selected_tests']

    entered, release = Event(), Event()

    def slow_run(*args, control, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError('timed out waiting for cancellation in test')
        control.check()

    monkeypatch.setattr(desktop_project, 'run_vitest_project', slow_run)
    bench.vitest_node_edit.clear()
    bench.vitest_run_button.click()
    try:
        wait_for(entered.is_set)
        assert '正在对比两版 Vitest' in bench.status_label.text()
        bench.cancel_button.click()
        assert '正在取消' in bench.status_label.text()
        assert not bench.vitest_run_button.isEnabled()
    finally:
        release.set()
        wait_for(lambda: bench.task is None)

    assert 'Vitest 对比已取消' in bench.status_label.text()
    assert bench.vitest_run_button.isEnabled()
    assert bench.vitest_report['report_dir'] == report['report_dir']
    bench.vitest_open_button.click()
    assert opened[-1] == str(tmp_path / 'run/report.html')

    monkeypatch.setattr(desktop_project, 'run_vitest_project', lambda *a, **kw: retry)
    bench.vitest_run_button.click()
    wait_for(lambda: bench.task is None)
    assert bench.vitest_report['report_dir'] == retry['report_dir']
    assert bench.vitest_error_label.isHidden()
    bench.vitest_open_button.click()
    assert opened[-1] == str(tmp_path / 'retry/report.html')
