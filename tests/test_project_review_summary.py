"""PR summaries and CI gates use the saved execution evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from reprolab.project_cli import main
from reprolab.project_review_summary import render_review_markdown, review_summary


@pytest.fixture(scope='module')
def demo(tmp_path_factory):
    root = tmp_path_factory.mktemp('review-demo')
    from reprolab.project_demo import create_project_demo
    from reprolab.project_sessions import run_project

    session = create_project_demo(root / 'sessions')
    return run_project(session['directory'], root / 'reports', confirmed=True)


def test_demo_command_executes_and_reports_all_five_outcomes(tmp_path, capsys):
    assert main(['demo', '--output', str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['counts'] == {
        'regression_passed': 5, 'improved': 1, 'regression_detected': 1,
        'baseline_failure': 1, 'inconclusive': 1,
    }
    assert Path(report['report_dir'], 'report.html').is_file()


def test_demo_handoff_can_check_received_summary_without_running_project(tmp_path, capsys, monkeypatch):
    from reprolab import project_sessions

    assert main(['demo', '--review-handoff', '--output', str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    handoff = Path(report['handoff_dir'])
    assert {path.name for path in handoff.iterdir()} == {'project.zip', 'acceptance-plan.json', 'review-summary.md'}
    assert report['acceptance']['required_improvements'][0]['status'] == 'passed'
    bundle = handoff / 'project.zip'
    markdown = handoff / 'review-summary.md'
    plan = handoff / 'acceptance-plan.json'
    args = ['check-summary', str(bundle), str(markdown), '--acceptance-plan', str(plan)]

    def unexpected_execution(*args, **kwargs):
        raise AssertionError('checking the summary must not run project code')

    monkeypatch.setattr(project_sessions, 'run_project', unexpected_execution)
    monkeypatch.setattr(project_sessions, 'replay_project_report', unexpected_execution)
    contents = markdown.read_bytes()
    markdown.write_bytes(contents.replace(b'\n', b'\r\n'))
    assert main(args) == 0
    check = json.loads(capsys.readouterr().out)
    assert check['status'] == 'consistent'
    assert check['report_sha256'] == report['acceptance']['report_sha256']
    assert check['acceptance_plan_sha256'] == report['acceptance']['acceptance_plan_sha256']
    assert check['gate']['exit_code'] == 1

    markdown.write_bytes(contents.replace(b'CI gate**', b'Done**', 1))
    assert main(args) == 2
    check = json.loads(capsys.readouterr().out)
    assert check['status'] == 'different' and check['gate']['exit_code'] == 1

    markdown.write_bytes(contents)
    assert main(args[:3]) == 2  # Omitting the plan changes the canonical summary.
    assert json.loads(capsys.readouterr().out)['status'] == 'different'


def test_default_markdown_and_explicit_detail_modes(demo, capsys):
    directory = Path(demo['report_dir'])
    assert main(['summary', str(directory / 'report.json')]) == 0
    markdown = capsys.readouterr().out
    for text in ('New failures detected', '9 selected tests', '| Blocked | 1 |', 'Report SHA-256',
                 'Working-tree snapshot'):
        assert text in markdown
    assert 'test_money_display' in markdown
    for private in (demo['repository'], str(directory), demo['issue']):
        assert private not in markdown

    assert main(['summary', str(directory), '--format', 'json', '--include-test-ids',
                 '--gate', 'no-new-failures']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['gate']['reason'] == 'new_failures'
    assert result['verdict'] == 'regression_detected'
    assert result['counts'] == demo['counts']
    assert result['revisions']['after']['sha256'] == demo['source_sha256']['after']
    assert result['details']['omitted'] == 0
    assert [item['status'] for item in result['details']['items']] == [
        'regression_detected', 'inconclusive', 'baseline_failure', 'improved',
    ]


@pytest.mark.parametrize(('selection', 'status', 'message'), [
    (None, 1, 'New failure 1'),
    (('test_discount_cannot_make_total_negative',), 0, 'Improved 1'),
    (('test_optional_payment_service',), 2, 'Blocked 1'),
])
def test_brief_reads_checked_zip_offline(demo, tmp_path, capsys, monkeypatch, selection, status, message):
    import hashlib
    from reprolab import project_sessions

    if selection:
        selected = ['tests/test_cart.py::' + name for name in selection]
        report = project_sessions.run_project(demo['project_directory'], tmp_path / 'runs',
                                              selected_ids=selected, confirmed=True)
    else:
        report = demo
    bundle = tmp_path / "actual evidence's.zip"
    project_sessions.pack_project_report(report['report_dir'], bundle)
    if status == 0:
        other = tmp_path / 'evidence.zip'
        project_sessions.pack_project_report(demo['report_dir'], other)
        monkeypatch.chdir(tmp_path)


    def unexpected_execution(*args, **kwargs):
        pytest.fail('the brief must only read saved evidence')

    monkeypatch.setattr(project_sessions, 'run_project', unexpected_execution)
    monkeypatch.setattr(project_sessions, 'replay_project_report', unexpected_execution)
    assert main(['summary', str(bundle), '--brief']) == status
    streams = capsys.readouterr()
    markdown = streams.out
    assert not streams.err
    assert f'Gate {("Passed", "Failed", "Blocked")[status]}' in markdown
    assert message in markdown
    assert 'reprolab project unpack --help' in markdown
    assert 'same ZIP you just checked' in markdown
    assert 'evidence.zip' not in markdown and bundle.name not in markdown
    assert hashlib.sha256(Path(report['report_dir'], 'report.json').read_bytes()).hexdigest() in markdown
    assert 'test_cart.py' not in markdown and str(tmp_path) not in markdown
    if selection:
        assert '8 discovered outside the selection' in markdown

    if status == 0:
        assert main(['summary', bundle.name, '--brief']) == 0
        assert capsys.readouterr().out == markdown
        assert main(['summary', other.name, '--brief']) == 1
        distinct = capsys.readouterr().out
        assert 'New failure 1' in distinct and distinct != markdown
        for source in (report['report_dir'], Path(report['report_dir'], 'report.json')):
            assert main(['summary', str(source), '--brief']) == 0
            trace = capsys.readouterr().out
            assert 'same report directory' in trace and 'project unpack' not in trace

        from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

        plan = tmp_path / 'plan.json'
        save_acceptance_plan(AcceptancePlan('pytest', 'strict', (), approved_selection=tuple(demo['selected_tests'])), plan)
        assert main(['summary', str(bundle), '--brief', '--language', 'zh', '--acceptance-plan', str(plan)]) == 2
        assert '已批准范围缺少 8 项' in capsys.readouterr().out


@pytest.mark.parametrize(('language', 'changes'), [
    ('en', [('decision brief', 'updated brief'), ('9 selected tests', '8 selected tests'),
            ('Gate Failed', 'Gate Passed'), ('New failure 1', 'New failure 0')]),
    ('zh', [('决策摘要', '更新摘要'), ('选择 9 项测试', '选择 8 项测试'),
            ('门禁 失败', '门禁 通过'), ('新增失败 1', '新增失败 0')]),
])
def test_brief_handoff_checks_text_and_gate_independently(demo, tmp_path, capsys, language, changes):
    from reprolab.project_sessions import pack_project_report, run_project

    archive = tmp_path / 'actual evidence.zip'
    wrong = tmp_path / 'evidence.zip'
    pack_project_report(demo['report_dir'], archive)
    other = run_project(demo['project_directory'], tmp_path / 'other', confirmed=True)
    pack_project_report(other['report_dir'], wrong)
    assert other['counts'] == demo['counts']
    options = ['--brief', '--language', language]
    assert main(['summary', str(archive), *options]) == 1
    original = capsys.readouterr().out
    markup = tmp_path / 'brief.md'

    def check(source, *arguments, code):
        assert main(['check-summary', str(source), str(markup), *arguments]) == code
        result = json.loads(capsys.readouterr().out)
        assert result['coverage'] == 'rendered_aggregate_markdown'
        assert result['gate']['exit_code'] == 1
        return result

    markup.write_bytes(original.replace('\n', '\r\n').encode())
    matched = check(archive, *options, code=0)
    assert matched['language'] == language and matched['status'] == 'consistent'
    assert check(demo['report_dir'], *options, code=0)['status'] == 'consistent'
    assert check(wrong, *options, code=2)['status'] == 'different'
    assert check(archive, '--brief', '--language', 'zh' if language == 'en' else 'en',
                 code=2)['status'] == 'different'
    for value, replacement in [*changes, (matched['report_sha256'], '0' * 64)]:
        assert value in original
        markup.write_text(original.replace(value, replacement, 1), encoding='utf-8')
        assert check(archive, *options, code=2)['status'] == 'different'


def test_brief_match_only_establishes_displayed_aggregates(demo, tmp_path, capsys):
    from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

    plans = (tmp_path / 'sender-plan.json', tmp_path / 'recipient-plan.json')
    for path, selected in zip(plans, (demo['selected_tests'][:1], demo['selected_tests'][1:2])):
        save_acceptance_plan(AcceptancePlan('pytest', 'strict', (), approved_selection=tuple(selected)), path)
    assert plans[0].read_bytes() != plans[1].read_bytes()
    assert main(['summary', demo['report_dir'], '--brief', '--acceptance-plan', str(plans[0])]) == 1
    markdown = tmp_path / 'brief.md'
    markdown.write_text(capsys.readouterr().out, encoding='utf-8')
    assert main(['check-summary', demo['report_dir'], str(markdown), '--brief',
                 '--acceptance-plan', str(plans[1])]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'consistent' and result['gate']['exit_code'] == 1
    assert result['coverage'] == 'rendered_aggregate_markdown'
    assert 'acceptance_plan_sha256' not in result


def test_brief_rejects_invalid_evidence_with_no_decision(tmp_path, capsys):
    bundle = tmp_path / 'invalid.zip'
    bundle.write_bytes(b'this is not evidence')
    assert main(['summary', str(bundle), '--brief']) == 2
    streams = capsys.readouterr()
    assert not streams.out
    assert json.loads(streams.err)['status'] == 'error'


def test_gate_allows_only_complete_existing_failures(demo, tmp_path, capsys):
    from reprolab.project_sessions import run_project

    def report_for(*names):
        return run_project(demo['project_directory'], tmp_path / 'reports',
                           selected_ids=['tests/test_cart.py::' + name for name in names],
                           confirmed=True)['report_dir']

    existing = report_for('test_existing_tax_problem', 'test_discount_cannot_make_total_negative')
    assert main(['summary', existing, '--format', 'json', '--gate', 'strict']) == 2
    strict = json.loads(capsys.readouterr().out)
    assert strict['gate']['reason'] == 'existing_failures'
    assert main(['summary', existing, '--format', 'json', '--gate', 'no-new-failures']) == 0
    allowed = json.loads(capsys.readouterr().out)
    assert allowed['gate']['reason'] == 'existing_failures_allowed'
    assert allowed['verdict'] == strict['verdict'] == 'attention'

    blocked = report_for('test_existing_tax_problem', 'test_optional_payment_service')
    for policy in ('strict', 'no-new-failures'):
        assert main(['summary', blocked, '--format', 'json', '--gate', policy]) == 2
        result = json.loads(capsys.readouterr().out)
        assert result['gate']['reason'] == 'blocked_or_incomplete'
        assert result['counts'] == {'baseline_failure': 1, 'inconclusive': 1}


@pytest.mark.parametrize(('partial', 'truncated', 'expected'), [
    (True, False, 'partial'), (False, True, 'truncated'), (True, True, 'partial_truncated'),
])
def test_run_scope_distinguishes_incomplete_discovery_and_empty_or_missing_hints(partial, truncated, expected):
    from reprolab.project_run_scope import pytest_run_scope

    identity = 'test_total.py::test_total'
    report = {
        'selected_tests': [identity], 'before': {'status': 'cancelled', 'progress': {
            'status': 'recorded', 'started': 1, 'completed': 0, 'session_finished': False, 'tail_ignored': True,
        }}, 'after': {'status': 'not_run'},
    }
    record = {'tests': [identity], 'count': 3, 'partial': partial, 'truncated': truncated}
    scope = pytest_run_scope(report, collection=record)
    assert scope['discovery'] == {'status': expected, 'count': 1, 'accuracy': 'at_least'}
    assert scope['recommendations']['status'] == 'unknown'
    assert scope['recommendations']['omitted_count'] is None
    assert scope['execution'] == {
        'before': {'status': 'cancelled', 'accuracy': 'at_least', 'started': 1, 'finished': 0, 'not_started': None},
        'after': {'status': 'not_run', 'accuracy': 'exact', 'started': 0, 'finished': 0, 'not_started': 1},
    }
    empty = pytest_run_scope(report, collection={**record, 'recommendations': {}})
    assert empty['recommendations'] == {'status': 'recorded', 'count': 0, 'selected_count': 0, 'omitted_count': 0}
    unknown = pytest_run_scope({'selected_tests': [identity], 'before': {'status': 'completed'}})
    assert unknown['discovery']['count'] is None and unknown['execution']['before']['finished'] is None


def test_cancelled_run_keeps_selection_and_records_candidate_never_started(demo, tmp_path, capsys):
    from reprolab.project_sessions import pack_project_report, run_project
    from reprolab.runner import RunControl

    selected = [demo['selected_tests'][0]]
    control = RunControl(progress=lambda phase, _payload: control.cancel() if phase == 'project_after' else None)
    run = run_project(demo['project_directory'], tmp_path / 'cancelled', selected_ids=selected,
                      confirmed=True, control=control)
    assert run['status'] == 'cancelled'
    evidence = tmp_path / 'cancelled.zip'
    pack_project_report(run['report_dir'], evidence)
    assert main(['summary', str(evidence), '--gate', 'no-new-failures', '--format', 'json']) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary['verdict'] == 'cancelled'
    scope = summary['run_scope']
    assert scope['discovery']['status'] == 'complete' and scope['selected_count'] == 1
    assert scope['execution']['before']['finished'] == 1
    assert scope['execution']['after'] == {'status': 'not_run', 'accuracy': 'exact', 'started': 0,
                                           'finished': 0, 'not_started': 1}


@pytest.mark.parametrize(('condition', 'reason'), [
    ('timeout', 'execution_unverified'), ('cancelled', 'execution_unverified'),
    ('partial', 'execution_unverified'), ('different_input', 'pytest_input_changed'),
    ('same_snapshot', 'same_snapshot_divergence'), ('scope', 'pytest_import_origin'),
    ('missing_shared', 'fixed_inputs_unverified'), ('missing_phase', None),
])
def test_call_failure_with_failed_cleanup_needs_comparable_complete_evidence(condition, reason):
    from reprolab.project_comparison import comparison_view

    identity = 'test_total.py::test_total'
    before = {'status': 'passed', 'phases': [
        {'phase': phase, 'outcome': 'passed', 'xfail': False} for phase in ('setup', 'call', 'teardown')
    ]}
    after = {'status': 'inconclusive', 'phases': [
        {'phase': 'setup', 'outcome': 'passed', 'xfail': False},
        {'phase': 'call', 'outcome': 'failed', 'xfail': False, 'exception': {'type': 'AssertionError'}},
        {'phase': 'teardown', 'outcome': 'failed', 'xfail': False, 'exception': {'type': 'RuntimeError'}},
    ]}
    entry = {'test_id': identity, 'status': 'inconclusive', 'before': before, 'after': after}
    run = {'status': 'completed', 'worker_record_complete': True, 'exit_code': 1, 'execution_order': [identity]}
    report = {'runner': 'pytest', 'status': 'attention', 'entries': [entry], 'counts': {'inconclusive': 1},
              'before': dict(run, exit_code=0), 'after': dict(run), 'shared_tests': {'test_total.py': 'a' * 64},
              'source_sha256': {'before': 'b' * 64, 'after': 'c' * 64},
              'source_mode_sha256': {'before': 'a' * 64, 'after': 'a' * 64}}
    scope = None
    if condition == 'timeout':
        report['after']['status'] = 'timed_out'
    elif condition == 'cancelled':
        report['status'] = 'cancelled'
    elif condition == 'partial':
        report['after']['worker_record_complete'] = False
    elif condition == 'different_input':
        before['input_digests'], after['input_digests'] = {'input': 'b' * 64}, {'input': 'c' * 64}
    elif condition == 'same_snapshot':
        report['source_sha256']['after'] = report['source_sha256']['before']
    elif condition == 'scope':
        scope = {'reason': 'pytest_import_origin'}
    elif condition == 'missing_shared':
        report.pop('shared_tests')
    elif condition == 'missing_phase':
        after['phases'].pop()
    evaluated, decisions = comparison_view(report, scope)
    assert evaluated is report
    assert decisions == ({identity: {'status': 'blocked', 'reason': reason}} if reason else {})


def test_required_targets_and_receipt_use_saved_zip_without_running_tests(demo, tmp_path, capsys, monkeypatch):
    from reprolab import project_sessions

    prefix = 'tests/test_cart.py::'
    fixed = prefix + 'test_discount_cannot_make_total_negative'
    existing = prefix + 'test_existing_tax_problem'
    skipped = prefix + 'test_optional_payment_service'
    passing = next(name for name in demo['selected_tests'] if name.startswith(prefix + 'test_regular_total['))
    source = project_sessions.run_project(demo['project_directory'], tmp_path / 'runs',
                                          selected_ids=[fixed, passing, existing], confirmed=True)
    bundle = tmp_path / 'source.zip'
    project_sessions.pack_project_report(source['report_dir'], bundle)
    receipt = tmp_path / 'receipt'
    assert main(['replay', str(bundle), '--confirm-run', '--receipt-dir', str(receipt),
                 '--output', str(tmp_path / 'replays')]) == 0
    capsys.readouterr()

    def unexpected_execution(*args, **kwargs):
        raise AssertionError('acceptance summary must only read saved evidence')

    monkeypatch.setattr(project_sessions, 'replay_project_report', unexpected_execution)

    def summary(*arguments, code=0, evidence=bundle):
        assert main(['summary', str(evidence), '--format', 'json', *map(str, arguments)]) == code
        output = capsys.readouterr()
        assert not output.err
        return json.loads(output.out)

    gate = ('--gate', 'no-new-failures')
    required = ('--require-improvement', fixed, '--require-receipt', receipt)
    accepted = summary(*gate, *required)
    assert accepted['gate']['status'] == 'passed'
    assert accepted['comparison_gate']['reason'] == 'existing_failures_allowed'
    assert accepted['required_improvements'] == [{'test_id': fixed, 'status': 'passed', 'outcome': 'improved'}]
    assert accepted['receipt']['verification_status'] == 'verified'
    assert accepted['receipt']['counts'] == {'matched': 3, 'different': 0, 'incomplete': 0}
    assert str(tmp_path) not in json.dumps(accepted)
    assert summary(*required, code=2)['comparison_gate']['reason'] == 'existing_failures'

    rejected = summary(*gate, '--require-improvement', fixed, '--require-improvement', passing, code=1)
    assert rejected['required_improvements'][1]['outcome'] == 'regression_passed'
    assert rejected['required_improvements'][1]['status'] == 'failed'
    blocked = summary(*gate, '--require-improvement', skipped, code=2)
    assert blocked['required_improvements'] == [{'test_id': skipped, 'status': 'blocked', 'outcome': None}]
    skipped_result = summary('--require-improvement', skipped, code=1, evidence=demo['report_dir'])
    assert skipped_result['required_improvements'][0]['outcome'] == 'inconclusive'
    assert skipped_result['required_improvements'][0]['status'] == 'blocked'
    assert skipped_result['comparison_gate']['reason'] == 'new_failures'

    assert main(['summary', str(bundle), *gate, '--require-improvement', fixed,
                 '--require-receipt', str(receipt)]) == 0
    markdown = capsys.readouterr().out
    assert fixed in markdown and 'Required replay receipt · Passed' in markdown
    assert accepted['receipt']['replay_report_sha256'] in markdown
    assert str(tmp_path) not in markdown
    received = tmp_path / 'review-summary.md'
    received.write_text(markdown, encoding='utf-8')
    checked = ['check-summary', str(bundle), str(received), *gate, *map(str, required)]
    assert main(checked) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'consistent'

    (receipt / 'replay-check.json').write_text('{"status": "verified"}', encoding='utf-8')
    assert main(checked) == 2
    verify = json.loads(capsys.readouterr().out)
    assert verify['status'] == 'different' and verify['gate']['status'] == 'blocked'
    tampered = summary(*gate, *required, code=2)
    assert tampered['receipt']['verification_status'] == 'verified'
    assert tampered['receipt']['status'] == 'blocked'
    assert tampered['receipt']['issues'] == [{'part': 'replay-check.json', 'reason': 'different'}]


def test_tampered_report_produces_no_shareable_summary(demo, tmp_path, capsys):
    from shutil import copytree

    copied = tmp_path / 'copied'
    copytree(demo['report_dir'], copied)
    path = copied / 'report.json'
    contents = json.loads(path.read_text())
    contents['counts']['regression_detected'] = 0
    path.write_text(json.dumps(contents))
    assert main(['summary', str(copied), '--gate', 'no-new-failures']) == 2
    stdout, stderr = capsys.readouterr()
    assert not stdout
    assert json.loads(stderr)['status'] == 'error'


def test_vitest_case_summary_keeps_blocked_files_and_bounds_names(monkeypatch):
    from reprolab import project_review_summary

    weird_name = 'odd``\n<script>@reviewer</script>\x00'
    cases = [{'test_id': weird_name, 'status': 'regression_detected'}] + [
        {'test_id': 'case-' + str(number), 'status': 'baseline_failure'} for number in range(24)
    ]
    report = {
        'runner': 'vitest', 'vitest_report_version': 2, 'status': 'regression_detected',
        'counts': {'regression_detected': 1, 'inconclusive': 1},
        'case_counts': {'regression_detected': 1, 'baseline_failure': 24},
        'selected_tests': ['suite.test.ts', 'blocked.test.ts'],
        'entries': [
            {'test_id': 'suite.test.ts', 'status': 'regression_detected', 'case_entries': cases},
            {'test_id': 'blocked.test.ts', 'status': 'inconclusive', 'case_entries': []},
        ],
        'before_commit': 'a' * 40, 'after_commit': 'b' * 40,
        'source_sha256': {'before': 'c' * 64, 'after': 'd' * 64},
        '_report_sha256': 'f' * 64,
        'before': {'status': 'completed'}, 'after': {'status': 'completed'},
    }
    monkeypatch.setattr(project_review_summary, '_checked_report', lambda *_a, **_kw: report)
    summary = review_summary('example', include_test_ids=True, policy='no-new-failures')
    assert summary['details']['omitted'] == 6
    assert summary['details']['items'][1] == {'test_id': 'blocked.test.ts', 'status': 'inconclusive'}
    markdown = render_review_markdown(summary)
    assert '| Blocked | 1 | 0 |' in markdown and '| Existing failure | 0 | 24 |' in markdown
    assert '``` odd`` <script>@reviewer</script>  ```' in markdown
    assert weird_name not in markdown

    report['status'] = 'attention'
    report['counts'] = {'baseline_failure': 1}
    report['case_counts'] = {'baseline_failure': 1}
    report['vitest_report_version'] = 1
    legacy = review_summary('example', policy='no-new-failures')
    assert 'case_counts' not in legacy and legacy['gate']['exit_code'] == 2
    report['vitest_report_version'] = 2
    assert review_summary('example', policy='no-new-failures')['gate']['exit_code'] == 0


def test_vitest_requires_exact_file_and_case_even_when_the_file_improved(monkeypatch, capsys):
    from reprolab import project_review_summary

    file = 'apps/store cart.test.ts'
    improved = 'cart > accepts a discount'
    passing = 'cart > rounds totals'
    entry = {'test_id': file, 'status': 'improved', 'case_entries': [
        {'test_id': improved, 'status': 'improved'}, {'test_id': passing, 'status': 'regression_passed'},
    ]}
    report = {'runner': 'vitest', 'vitest_report_version': 2, 'status': 'regression_passed', 'entries': [entry],
              'counts': {'improved': 1}, 'case_counts': {'improved': 1, 'regression_passed': 1},
              'report_dir': 'example',
              'selected_tests': [file], '_report_sha256': 'a' * 64,
              'source_sha256': {'before': 'b' * 64, 'after': 'c' * 64},
              'before': {'status': 'completed'}, 'after': {'status': 'completed'}}
    monkeypatch.setattr(project_review_summary, '_checked_report', lambda *_a, **_kw: report)

    def summary(*args, code=0):
        assert main(['summary', 'example', '--format', 'json', *args]) == code
        out, err = capsys.readouterr()
        return json.loads(out or err)

    expected = {'file': file, 'test_id': improved, 'status': 'passed', 'outcome': 'improved'}
    assert summary('--require-vitest-case', file, improved)['required_improvements'] == [expected]
    assert summary('--require-vitest-case', file, passing, code=1)['required_improvements'][0]['status'] == 'failed'
    assert summary('--require-vitest-case', 'different.test.ts', improved, code=2)['gate']['status'] == 'blocked'
    assert summary('--require-improvement', file, code=2)['status'] == 'error'
    duplicate = ('--require-vitest-case', file, improved)
    assert summary(*duplicate, *duplicate, code=2)['status'] == 'error'

    report['vitest_report_version'] = 1
    entry.pop('case_entries')
    assert summary(*duplicate, code=2)['required_improvements'] == [
        {**expected, 'status': 'blocked', 'outcome': None}]


@pytest.mark.parametrize('warms_to_pass', [True, False])
def test_same_snapshot_change_is_observed_without_certifying_a_patch(tmp_path, capsys, warms_to_pass):
    from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan
    from test_pytest_profile import make_project

    repo = make_project(tmp_path / 'repo')
    marker = tmp_path / 'external-marker'
    (repo / 'tests/test_calc.py').write_text(
        'from pathlib import Path\n'
        'def test_answer():\n'
        f'    marker = Path({str(marker)!r})\n'
        '    seen = marker.exists()\n'
        '    marker.touch()\n'
        f'    assert seen is {warms_to_pass!r}\n', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
           '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid']
    subprocess.run([*git, 'add', 'tests/test_calc.py'], check=True, capture_output=True)
    subprocess.run([*git, 'commit', '-qm', 'external-state example'], check=True, capture_output=True)
    target = 'tests/test_calc.py::test_answer'
    plan = tmp_path / 'acceptance-plan.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (target,)), plan)
    args = ['review', str(repo), '--before', 'HEAD', '--after', 'HEAD', '--test', target,
            '--acceptance-plan', str(plan), '--verify-plan', '--output', str(tmp_path / 'review')]
    code = main(args)
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report['changed_files'] == []
    observed = 'improved' if warms_to_pass else 'regression_detected'
    assert report['entries'][0]['status'] == observed
    summary = report['acceptance']
    assert summary['comparison_gate']['reason'] == 'same_snapshot_divergence'
    assert summary['required_improvements'] == [
        {'test_id': target, 'status': 'blocked', 'outcome': observed,
         'reason': 'same_snapshot_divergence'}]
    assert main(['check', report['report_dir']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'consistent'
    assert main(['summary', report['report_dir'], '--gate', 'strict']) == 2
    markdown = capsys.readouterr().out
    assert 'Identical frozen snapshots' in markdown
    assert 'CI gate** · Blocked' in markdown


@pytest.mark.parametrize('committed', [False, True])
def test_excluded_tracked_change_cannot_greenlight_identical_snapshots(tmp_path, capsys, committed):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git = ['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
           '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid']

    def commit(message):
        subprocess.run([*git, 'add', '-f', '.env.example', 'test_limit.py'], check=True, capture_output=True)
        subprocess.run([*git, 'commit', '-qm', message], check=True, capture_output=True)

    template = repo / '.env.example'
    template.write_text('100', encoding='utf-8')
    (repo / 'test_limit.py').write_text(
        'from pathlib import Path\n'
        'def test_limit():\n'
        '    template = Path(__file__).with_name(".env.example")\n'
        '    value = int(template.read_text()) if template.exists() else 100\n'
        '    assert value == 100\n', encoding='utf-8')
    subprocess.run([*git, 'init', '-q'], check=True, capture_output=True)
    commit('baseline')
    base = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
    template.write_text('10', encoding='utf-8')
    if committed:
        commit('candidate')
    native = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_limit.py'],
                            cwd=repo, capture_output=True)
    assert native.returncode == 1
    candidate = ['--after', 'HEAD'] if committed else ['--working-tree']
    code = main(['review', str(repo), '--before', base, *candidate,
                 '--test', 'test_limit.py::test_limit', '--output', str(tmp_path / 'review')])
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report['excluded_tracked_changes'] == 1 and report['changed_files'] == []
    assert report['comparison_verdict'] == 'attention'
    assert report['comparison_scope']['reason'] == 'excluded_changes_only'
    assert report['counts'] == {'regression_passed': 1}
    source = Path(report['report_dir'])
    assert not (source / 'evidence/project/inputs/after/.env.example').exists()
    assert '本次改动未纳入快照' in (source / 'report.html').read_text(encoding='utf-8')
    assert main(['summary', str(source), '--gate', 'strict', '--format', 'json']) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary['gate']['reason'] == 'excluded_changes_only'


def test_unavailable_optional_evidence_does_not_hide_confirmed_regression(demo, tmp_path, capsys):
    from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

    plan_file = tmp_path / 'acceptance-plan.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (), True, 2), plan_file)
    options = ['--acceptance-plan', str(plan_file), '--require-receipt', str(tmp_path / 'missing-receipt'),
               '--require-stability', str(tmp_path / 'missing-stability')]
    assert main(['summary', demo['report_dir'], *options, '--format', 'json']) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary['comparison_gate']['reason'] == 'new_failures'
    assert summary['gate']['status'] == 'failed'
    assert summary['receipt']['status'] == summary['stability']['status'] == 'blocked'
    assert main(['summary', demo['report_dir'], *options]) == 1
    markdown = capsys.readouterr().out
    assert 'CI gate** · Failed' in markdown
    assert 'Required replay stability · Blocked' in markdown
    assert 'Required replay receipt · Blocked' in markdown


@pytest.mark.parametrize('interruption', ['cancel', 'collision', None])
def test_pack_publishes_only_the_completed_zip(demo, tmp_path, monkeypatch, interruption):
    from zipfile import ZipFile
    from reprolab.project_archive import pack_project_report
    from reprolab.runner import RunCancelled, RunControl

    target = tmp_path / 'review.zip'
    control = RunControl()
    original_write = ZipFile.write
    writes = []

    def write(archive, path, name):
        if not writes:
            assert not target.exists()
            if interruption == 'cancel':
                control.cancel()
            elif interruption == 'collision':
                target.write_bytes(b'another operation')
        writes.append(name)
        return original_write(archive, path, name)

    monkeypatch.setattr(ZipFile, 'write', write)
    if interruption == 'cancel':
        with pytest.raises(RunCancelled):
            pack_project_report(demo['report_dir'], target, control=control)
        assert not target.exists()
    elif interruption == 'collision':
        with pytest.raises(ValueError, match='已存在'):
            pack_project_report(demo['report_dir'], target, control=control)
        assert target.read_bytes() == b'another operation'
    else:
        pack_project_report(demo['report_dir'], target, control=control)
        assert review_summary(target)['report_sha256']
    assert not list(tmp_path.glob('.project-pack-*'))


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux/macOS fixture uses Windows-incompatible names')
def test_project_zip_path_preflight_warns_at_freeze_and_leaves_test_gate_independent(tmp_path, capsys):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'test_ok.py').write_text('def test_ok(): pass\n', encoding='utf-8')
    (repo / 'CON.txt').write_text('synthetic device name', encoding='utf-8')
    (repo / 'Meeting:notes.txt').write_text('synthetic archive path', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
           '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid']
    for args in (('init', '-q'), ('add', '.'), ('commit', '-qm', 'fixture')):
        subprocess.run([*git, *args], check=True, capture_output=True)
    prepared = ['prepare', str(repo), '--before', 'HEAD', '--after', 'HEAD', '--output', str(tmp_path / 'sessions')]
    assert main(prepared) == 0
    state = json.loads(capsys.readouterr().out)
    check = state['archive_preflight']
    assert check['status'] == 'blocked' and check['blocked_count'] == check['portability_count'] == 2
    assert {(issue['path'], issue['reason']) for issue in check['issues']} == {
        ('CON.txt', 'windows_name'), ('Meeting:notes.txt', 'archive_path')}
    assert main(['run', state['directory'], '--test', 'test_ok.py::test_ok',
                 '--output', str(tmp_path / 'reports')]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['archive_preflight'] == check
    assert 'Meeting:notes.txt' in (Path(report['report_dir']) / 'report.html').read_text(encoding='utf-8')
    assert review_summary(report['report_dir'], policy='strict')['gate']['exit_code'] == 0
    assert main(['pack', report['report_dir'], '--output', str(tmp_path / 'blocked.zip')]) == 2
    assert 'Meeting:notes.txt' in capsys.readouterr().err
