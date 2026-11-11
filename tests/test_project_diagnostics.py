"""Grouping describes observed symptoms and preserves exact recheck ranges."""
from copy import deepcopy
from pathlib import Path

import pytest

from reprolab.project_diagnostics import group_project_failures, format_diagnostic_group
from reprolab import project_history, project_refresh


def outcome(phase='call', error_type='AssertionError', message='assert 0 == 42'):
    error = {'type': error_type, 'message': message}
    return {'status': 'failed' if phase == 'call' else 'inconclusive', 'exception': error,
            'phases': [{'phase': phase, 'outcome': 'failed', 'exception': error, 'xfail': False}]}


def report(entries):
    return {'kind': 'project_regression', 'schema_version': 1, 'run_id': 'run', 'project_id': 'project',
            'title': 'diagnostics', 'report_dir': '/reports/run', '_report_sha256': 'a' * 64,
            'status': 'attention', 'entries': entries, 'selected_tests': [entry['test_id'] for entry in entries],
            'before': {'status': 'completed', 'worker_record_complete': True},
            'after': {'status': 'completed', 'worker_record_complete': True}}


def entry(name, status='regression_detected', after=None, before=None):
    return {'test_id': 'tests/test_sample.py::' + name, 'status': status,
            'before': before or {'status': 'passed'}, 'after': after or outcome()}


def test_same_observed_exception_groups_tests_but_retains_regression_classifications():
    source = report([entry('new'), entry('existing', 'baseline_failure', before=outcome())])
    original = deepcopy(source)
    result = group_project_failures(source)
    group = next(group for group in result['groups'] if group['side'] == 'after')
    assert group['count'] == 2 and group['status_counts'] == {'regression_detected': 1, 'baseline_failure': 1}
    assert result['attention_test_count'] == 2 and result['group_count'] == 2
    assert '共同根因' in result['notice']
    text = format_diagnostic_group(source, group)
    assert all(identity in text for identity in source['selected_tests'])
    assert '新增失败 1' in text and '原有失败 1' in text
    assert source == original


def test_different_messages_phases_and_missing_details_do_not_merge_independent_failures():
    source = report([entry('a', after=outcome(message='one')), entry('b', after=outcome(message='two')),
                     entry('fixture', 'inconclusive', after=outcome('setup', message='one')),
                     entry('blank_a', after=outcome(message='')), entry('blank_b', after=outcome(message=''))])
    result = group_project_failures(source)
    assert len(result['groups']) == 5
    assert all(group['count'] == 1 for group in result['groups'])
    legacy = report([entry('legacy', after={'status': 'failed', 'exception': {'type': 'ValueError', 'message': 'unknown stage'}})])
    assert group_project_failures(legacy)['groups'][0]['category'] == 'unknown'


def test_collection_problem_does_not_turn_passing_selected_tests_into_failures():
    source = report([entry('passed', 'regression_passed', after={'status': 'passed'})])
    source['after']['collection_errors'] = ['another test file could not be collected']
    result = group_project_failures(source)
    assert result['attention_test_count'] == 0
    assert len(result['groups']) == 1 and result['groups'][0]['test_ids'] == []
    assert result['groups'][0]['scope'] == 'version'
    assert result['recorded_status'] == 'attention'


@pytest.mark.parametrize('flag, category', [('timed_out', 'timeout'), ('cancelled', 'cancelled'), ('worker_record_complete', 'incomplete')])
def test_incomplete_versions_use_version_scope_without_inventing_test_failures(flag, category):
    source = report([entry('a', 'inconclusive', after={'status': 'inconclusive'}),
                     entry('b', 'inconclusive', after={'status': 'inconclusive'})])
    source['after'][flag] = False if flag == 'worker_record_complete' else True
    result = group_project_failures(source)
    assert len(result['groups']) == 1
    assert result['groups'][0]['category'] == category
    assert result['groups'][0]['count'] == 2 and result['groups'][0]['scope'] == 'version'


def test_html_keeps_group_memberships_and_escapes_recorded_messages(tmp_path):
    from reprolab.project_report import write_project_report
    text = '<script>alert("untrusted")</script>'
    source = report([entry('a', after=outcome(message=text)), entry('b', after=outcome(message=text))])
    source['report_dir'] = str(tmp_path)
    path = write_project_report(source)
    html = Path(path).read_text()
    group = group_project_failures(source)['groups'][0]
    assert '按失败现象处理' in html and 'id="diagnostic-filter"' in html
    assert html.count('data-groups="' + group['id'] + '"') == 2
    assert text not in html and '&lt;script&gt;' in html


def test_explicit_recheck_preserves_source_order_and_rejects_unknown_or_empty_ranges(monkeypatch, tmp_path):
    source = report([entry('a'), entry('b'), entry('c')])
    monkeypatch.setattr(project_history, '_checked_report', lambda directory: deepcopy(source))
    calls = []
    def refresh(directory, output, **options):
        calls.append(options)
        return options
    monkeypatch.setattr(project_refresh, 'refresh_project', refresh)
    a, b, _ = source['selected_tests']
    result = project_refresh.refresh_from_report('/reports/run', tmp_path, selected_ids=[b, a])
    assert result['selected_ids'] == [a, b]
    assert result['review_source']['selection_mode'] == 'selected'
    with pytest.raises(ValueError, match='来源报告'):
        project_refresh.refresh_from_report('/reports/run', tmp_path, selected_ids=['tests/test_other.py::other'])
    with pytest.raises(ValueError):
        project_refresh.refresh_from_report('/reports/run', tmp_path, selected_ids=[])
    with pytest.raises(ValueError, match='不能同时'):
        project_refresh.refresh_from_report('/reports/run', tmp_path, scope='attention', selected_ids=[a])
    assert len(calls) == 1
