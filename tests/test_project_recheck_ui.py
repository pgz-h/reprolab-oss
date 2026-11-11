"""Deferred widget regressions for pending ranges and report-specific entry points."""
import json
from pathlib import Path
import sys

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from reprolab import desktop_project
from reprolab.runner import RunControl
from test_desktop_ui import window
from test_desktop_usability import wait_project


OLD = 'tests/test_cart.py::test_money_display'
AVAILABLE = 'tests/test_cart.py::test_existing_tax_problem'


def state(tmp_path, *, selected, available):
    folder = tmp_path / 'session'
    folder.mkdir(exist_ok=True)
    return {'schema_version': 1, 'kind': 'project_session', 'project_id': 'session',
            'title': 'review', 'repository': str(tmp_path / 'repo'), 'directory': str(folder),
            'before_commit': 'a' * 40, 'after_commit': '@working-tree', 'runs': [],
            'source_sha256': {'before': 'b' * 64, 'after': 'c' * 64},
            'source_mode_sha256': {'before': 'd' * 64, 'after': 'd' * 64},
            'runtime': {'executable': sys.executable, 'python_version': '3.12', 'pytest_version': '8'},
            'changes': {'files': [], 'total': 0}, 'changed_files': [], 'recommendations': {},
            'selection_request': {'schema_version': 1, 'kind': 'project_selection_request', 'selected_tests': selected},
            'collection': {'status': 'collected', 'tests': available, 'runtime': {'executable': sys.executable},
                           'partial': False, 'truncated': False}}


def test_pending_ids_survive_reopen_and_refresh_but_explicit_clear_remains_empty(window, tmp_path, monkeypatch):
    bench = window.project_workbench
    wait_project(bench)
    data = state(tmp_path, selected=[OLD, AVAILABLE], available=[AVAILABLE])
    bench._apply_state(data)
    assert bench.selected_test_ids() == []
    assert bench._pending_selection == [OLD, AVAILABLE]
    bench._schedule_view_save()
    bench._save_view_state()
    saved = json.loads((Path(data['directory']) / 'desktop-view.json').read_text())
    assert saved['selected_tests'] == []
    assert saved['pending_tests'] == [OLD, AVAILABLE]
    bench._apply_state(data)
    assert bench._pending_selection == [OLD, AVAILABLE]
    assert bench._selection_review['missing_tests'] == [OLD]
    calls = []
    monkeypatch.setattr(desktop_project, 'refresh_project', lambda *args, **kw: calls.append(kw))
    monkeypatch.setattr(bench, '_start', lambda action, *args, **kw: action(RunControl()))
    bench.refresh_workspace()
    assert calls[-1]['selected_ids'] == [OLD, AVAILABLE]
    bench.clear_selected_tests()
    bench._save_view_state()
    bench._apply_state(data)
    assert bench.selected_test_ids() == []
    assert bench._pending_selection is None
    bench.refresh_workspace()
    assert calls[-1]['selected_ids'] == []


def test_search_burst_updates_once_and_bulk_selection_uses_pending_query(window, tmp_path, monkeypatch):
    bench = window.project_workbench
    wait_project(bench)
    bench._apply_state(state(tmp_path, selected=[], available=[OLD, AVAILABLE]))
    updates = []
    original = bench._sync_controls

    def sync():
        updates.append(1)
        original()

    monkeypatch.setattr(bench, '_sync_controls', sync)
    bench.test_search.setText('m')
    bench.test_search.setText('money')
    bench.test_search.setText('money_display')
    assert not updates and bench.test_search_timer.isActive()
    QTest.qWait(200)
    assert len(updates) == 1
    assert bench._visible_test_ids() == {OLD}

    bench.test_search.setText('existing_tax')
    bench.select_visible_tests()
    assert bench.selected_test_ids() == [AVAILABLE]
    assert not bench.test_search_timer.isActive()


def test_manual_check_at_limit_keeps_existing_selection(window, tmp_path):
    bench = window.project_workbench
    wait_project(bench)
    ids = [f'tests/test_cart.py::test_total[{index}]' for index in range(2001)]
    bench._apply_state(state(tmp_path, selected=ids[:-1], available=ids))
    extra = bench.tests_tree.topLevelItem(len(ids) - 1)
    extra.setCheckState(0, Qt.CheckState.Checked)
    assert extra.checkState(0) == Qt.CheckState.Unchecked
    assert bench.selected_test_ids() == ids[:-1]
    assert '最多选择 2000 项' in bench.status_label.text()


def test_run_history_uses_report_status_for_a_run_with_improved_tests(window, tmp_path):
    bench = window.project_workbench
    wait_project(bench)
    data = state(tmp_path, selected=[OLD], available=[OLD])
    data['runs'] = [{'run_id': 'reviewed', 'status': 'regression_passed',
                     'report_dir': str(tmp_path / 'reports' / 'reviewed')}]
    bench._apply_state(data)
    assert bench.history_tree.topLevelItem(0).text(1) == '所选项目回归通过'


def test_report_review_uses_displayed_run_and_shows_scope_counts(window, tmp_path, monkeypatch):
    bench = window.project_workbench
    wait_project(bench)
    bench._apply_state(state(tmp_path, selected=[OLD, AVAILABLE], available=[OLD, AVAILABLE]))
    report = tmp_path / 'chosen-run'
    report.mkdir()
    raw = {'kind': 'project_regression', 'schema_version': 1, 'project_id': 'session', 'run_id': 'chosen',
           'title': 'same title', 'report_dir': str(report), 'status': 'regression_detected',
           'selected_tests': [OLD, AVAILABLE], 'entries': [
               {'test_id': OLD, 'status': 'regression_detected', 'before': {'status': 'passed'}, 'after': {'status': 'failed'}},
               {'test_id': AVAILABLE, 'status': 'regression_passed', 'before': {'status': 'passed'}, 'after': {'status': 'passed'}}]}
    (report / 'report.json').write_text(json.dumps(raw))
    bench._apply_report(raw)
    assert '2 项' in bench.recheck_button.text()
    bench.recheck_scope.setCurrentIndex(bench.recheck_scope.findData('attention'))
    assert '1 项' in bench.recheck_button.text()
    assert window.store.settings['project_report_views']['session']['run_id'] == 'chosen'
    # A newer run in the same session must not replace this explicitly displayed source.
    bench.state['runs'] = [{'run_id': 'newer', 'report_dir': str(tmp_path / 'newer')}]
    calls = []
    monkeypatch.setattr(desktop_project, 'refresh_from_report', lambda *args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(bench, '_start', lambda action, *args, **kw: action(RunControl()))
    bench.recheck_report()
    assert calls[0][0][0] == str(report)
    assert calls[0][1]['scope'] == 'attention'
    assert calls[0][1]['expected_reference']['run_id'] == 'chosen'
