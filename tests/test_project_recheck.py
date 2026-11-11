"""Report-driven review regressions; execution is deferred until authorized."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from reprolab import project_history as history
from reprolab import project_refresh as refresh
from reprolab.project_selections import read_selection, reconcile_selection


DISPLAY = 'tests/test_cart.py::test_money_display'
TAX = 'tests/test_cart.py::test_existing_tax_problem'
SKIP = 'tests/test_cart.py::test_optional_payment_service'


def entry(identity, status, *, old='passed', new='passed'):
    return {'test_id': identity, 'status': status,
            'before': {'status': old}, 'after': {'status': new}}


def saved_report(run, entries, *, fingerprint='a' * 64):
    return {'kind': 'project_regression', 'schema_version': 1,
            'run_id': run, 'project_id': 'same-session', 'title': 'same title',
            'report_dir': '/reports/' + run, 'project_directory': '/sessions/original',
            '_report_sha256': fingerprint, 'entries': entries,
            'selected_tests': [item['test_id'] for item in entries],
            'before_commit': 'f' * 40, 'timeout': 120,
            'source_sha256': {'before': 'b' * 64, 'after': 'c' * 64},
            'source_mode_sha256': {'before': 'd' * 64, 'after': 'd' * 64},
            'shared_tests': {'tests/test_cart.py': 'e' * 64}}


def compare(monkeypatch, before, after):
    reports = {'left': before, 'right': after}
    monkeypatch.setattr(history, '_checked_report', lambda path: deepcopy(reports[str(path)]))
    return history.compare_project_reports('left', 'right')


def test_same_test_recovery_survives_normal_workspace_changes_and_keeps_other_states(monkeypatch):
    omitted = 'tests/test_cart.py::test_regular_total[10]'
    not_run = 'tests/test_cart.py::test_regular_total[20]'
    before = saved_report('first', [entry(DISPLAY, 'regression_detected', new='failed'),
        entry(TAX, 'baseline_failure', old='failed', new='failed'),
        entry(SKIP, 'inconclusive', old='skipped', new='skipped'),
        entry(omitted, 'regression_passed'), entry(not_run, 'regression_passed')])
    after = saved_report('second', [entry(DISPLAY, 'regression_passed'),
        entry(TAX, 'baseline_failure', old='failed', new='failed'),
        entry(SKIP, 'inconclusive', old='skipped', new='skipped'),
        entry(not_run, 'inconclusive', old='not_run', new='not_run')])
    after['source_sha256']['after'] = '0' * 64
    result = compare(monkeypatch, before, after)
    changes = {row['test_id']: row['change'] for row in result['entries']}
    assert result['same_check_resolved'] == 0
    assert result['state_counts']['became_passed'] == 1
    assert changes == {DISPLAY: 'conditions_changed', TAX: 'still_failed', SKIP: 'blocked',
                       omitted: 'only_before', not_run: 'not_run'}
    assert result['scope']['same_sources'] is False
    assert result['scope']['same_baseline'] is True
    assert result['scope']['same_recorded_environment'] is None


@pytest.mark.parametrize('changed', [True, False])
def test_changed_or_missing_test_fingerprints_do_not_count_as_same_check_recovery(monkeypatch, changed):
    before = saved_report('first', [entry(DISPLAY, 'regression_detected', new='failed')])
    after = saved_report('second', [entry(DISPLAY, 'regression_passed')])
    if changed:
        after['shared_tests']['tests/test_cart.py'] = '0' * 64
    else:
        del after['shared_tests']
        del after['before_commit']
        del after['timeout']
    before['entries'].append(entry(SKIP, 'inconclusive', old='skipped', new='skipped'))
    after['entries'].append(entry(SKIP, 'inconclusive', old='skipped', new='skipped'))
    result = compare(monkeypatch, before, after)
    assert result['execution_counts']['blocked'] == 1
    assert '本轮受阻 1 项' in result['summary']
    row = next(row for row in result['entries'] if row['test_id'] == DISPLAY)
    assert result['same_check_resolved'] == 0
    assert row['change'] == ('test_changed' if changed else 'test_unknown')
    assert row['after']['status'] == 'regression_passed'
    assert row['after_execution'] == 'passed'
    if not changed:
        assert result['scope']['same_baseline'] is None
        assert result['scope']['same_timeout'] is None


def test_environment_and_baseline_differences_are_independent_conditions(monkeypatch):
    before = saved_report('first', [entry(DISPLAY, 'regression_detected', new='failed')])
    after = saved_report('second', [entry(DISPLAY, 'regression_passed')])
    for report in (before, after):
        report['runtime'] = {'executable': '/trusted/python', 'python_version': '3.12.0', 'pytest_version': '8.4.0'}
        report['environment'] = {'status': 'recorded', 'platform': 'Darwin', 'machine': 'arm64',
                                 'distributions': {'pytest': '8.4.0'}}
        for side in ('before', 'after'):
            report[side] = {'python_version': '3.12.0', 'pytest_version': '8.4.0'}
    after['environment']['distributions']['another-package'] = '2'
    after['before_commit'] = '1' * 40
    result = compare(monkeypatch, before, after)
    assert result['scope']['same_baseline'] is False
    assert result['scope']['same_recorded_environment'] is False
    assert any(note.startswith('基线不同') for note in result['notices'])
    assert any(note.startswith('记录的环境不同') for note in result['notices'])


def test_source_relocation_requires_exact_run_and_hash_and_preserves_evidence_checks(monkeypatch, tmp_path):
    old = saved_report('chosen', [entry(DISPLAY, 'regression_detected', new='failed')])
    reference = history.report_reference(old)
    moved = tmp_path / 'moved'
    moved.mkdir()
    checked_report = history._checked_report
    monkeypatch.setattr(history, '_checked_report', lambda path: {**old, 'report_dir': str(path)})
    assert history.resolve_report_reference(reference, moved)['run_id'] == 'chosen'
    replacement = {**old, 'run_id': 'newer', '_report_sha256': 'f' * 64}
    monkeypatch.setattr(history, '_checked_report', lambda path: replacement)
    with pytest.raises(ValueError, match='身份不符'):
        history.resolve_report_reference(reference, moved)
    with pytest.raises(ValueError, match='本次未找到'):
        history.resolve_report_reference(reference, tmp_path / 'absent')
    monkeypatch.setattr(history, '_checked_report', checked_report)
    monkeypatch.setattr(history, 'read_report_snapshot', lambda path: old)
    monkeypatch.setattr(history, 'check_project_report', lambda path: {'status': 'inconsistent', 'issues': ['before.log']})
    with pytest.raises(ValueError, match='证据不一致'):
        history._checked_report(moved)


def test_report_review_selects_the_chosen_run_not_a_newer_run_in_the_same_session(monkeypatch, tmp_path):
    old = saved_report('chosen', [entry(DISPLAY, 'regression_detected', new='failed'),
        entry(TAX, 'baseline_failure', old='failed', new='failed'), entry(SKIP, 'inconclusive'),
        entry('tests/test_cart.py::test_regular_total[10]', 'regression_passed')])
    monkeypatch.setattr(history, '_checked_report', lambda path: old)
    calls = []
    def freeze(directory, output, **options):
        calls.append((directory, options))
        return options
    monkeypatch.setattr(refresh, 'refresh_project', freeze)
    result = refresh.refresh_from_report('chosen', tmp_path, scope='attention',
                                       expected_reference=history.report_reference(old))
    assert result['selected_ids'] == [DISPLAY, TAX, SKIP]
    assert result['review_source']['run_id'] == 'chosen'
    assert result['review_source']['project_id'] == old['project_id']
    assert calls[0][0] == Path(old['report_dir']) / 'evidence/project'
    wrong = {**history.report_reference(old), 'run_id': 'newest'}
    with pytest.raises(ValueError, match='身份不符'):
        refresh.refresh_from_report('chosen', tmp_path, expected_reference=wrong)


def test_cli_empty_ranges_are_distinct_and_run_rejects_them_before_collection(tmp_path, monkeypatch, capsys):
    from reprolab import project_cli, project_sessions
    path = tmp_path / 'empty.json'
    path.write_text('[]')
    assert read_selection(path, allow_empty=True) == []
    with pytest.raises(ValueError, match='1–2000'):
        read_selection(path)
    assert reconcile_selection(None, [])['range_state'] == 'unspecified'
    assert reconcile_selection([], [])['range_state'] == 'empty'
    assert reconcile_selection([DISPLAY], [], collection_complete=False)['range_state'] == 'pending'
    monkeypatch.setattr(refresh, 'refresh_project', lambda *args, **kw: {'selected': kw['selected_ids']})
    assert project_cli.main(['refresh', str(tmp_path), '--output', str(tmp_path / 'new'), '--selection-file', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['selected'] == []
    monkeypatch.setattr(refresh, 'review_project_selection', lambda *args, **kw: {'ready': False, 'selected': kw['selected_ids']})
    assert project_cli.main(['selection-check', str(tmp_path), '--selection-file', str(path)]) == 2
    assert json.loads(capsys.readouterr().out)['selected'] == []
    monkeypatch.setattr(project_sessions, 'read_project', lambda path: {'selection_request': {
        'schema_version': 1, 'kind': 'project_selection_request', 'selected_tests': []}})
    def forbidden(*args, **kw):
        pytest.fail('An empty run must not start discovery or a project interpreter')
    monkeypatch.setattr(project_sessions, 'choose_runtime', forbidden)
    monkeypatch.setattr(project_sessions, 'collect_project', forbidden)
    with pytest.raises(ValueError, match='主动清空'):
        project_sessions.run_project(tmp_path, tmp_path / 'reports', confirmed=True)


@pytest.mark.parametrize('scenario, expected_change', [('fix-money-display.patch', 'resolved'),
                                                    ('change-money-test.patch', 'test_changed')])
def test_cart_end_to_end_review_scenarios(tmp_path, scenario, expected_change):
    """Deferred real flow: requires explicit permission to run project tests."""
    import subprocess
    from reprolab.project_demo import create_project_demo
    from reprolab.project_sessions import collect_project, run_project, read_project, check_project_report
    state = create_project_demo(tmp_path / 'sessions')
    collect_project(state['directory'])
    original = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    original_bytes = (Path(original['report_dir']) / 'report.json').read_bytes()
    repo = Path(state['repository'])
    # Advancing HEAD must not advance the baseline of this selected source run.
    subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-c', 'user.name=ReproLab test',
                    '-c', 'user.email=test@example.invalid', '-C', str(repo),
                    'commit', '--allow-empty', '-m', 'advance head'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'apply', '--unidiff-zero', str(repo / 'recheck-scenarios' / scenario)], check=True)
    follow = refresh.refresh_from_report(original['report_dir'], tmp_path / 'sessions')
    assert follow['before_commit'] == state['before_commit']
    assert follow['review_source']['run_id'] == original['run_id']
    collect_project(follow['directory'])
    current = run_project(follow['directory'], tmp_path / 'reports', confirmed=True)
    result = history.compare_with_source(current['report_dir'])
    money = next(row for row in result['entries'] if row['test_id'] == DISPLAY)
    assert money['change'] == expected_change
    assert money['after']['status'] == 'regression_passed'
    assert result['same_check_resolved'] == (1 if expected_change == 'resolved' else 0)
    assert (Path(original['report_dir']) / 'report.json').read_bytes() == original_bytes
    assert read_project(state['directory'])['project_id'] == state['project_id']
    assert check_project_report(original['report_dir'])['status'] == 'consistent'
    if expected_change == 'resolved':
        import shutil
        legacy = tmp_path / 'legacy-report'
        shutil.copytree(original['report_dir'], legacy)
        older = json.loads((legacy / 'report.json').read_text())
        for key in ('shared_tests', 'timeout', 'before_commit', 'environment'):
            older.pop(key, None)
        (legacy / 'report.json').write_text(json.dumps(older))
        assert check_project_report(legacy)['status'] == 'consistent'
        old_comparison = history.compare_project_reports(legacy, current['report_dir'])
        assert old_comparison['same_check_resolved'] == 0
        assert old_comparison['counts']['test_unknown'] == 9
        (legacy / 'before.log').write_text('modified log')
        with pytest.raises(ValueError, match='证据不一致'):
            history.compare_project_reports(legacy, current['report_dir'])
