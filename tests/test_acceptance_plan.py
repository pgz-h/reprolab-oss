"""Portable plan parsing and CI acceptance against independently saved reports."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from reprolab.acceptance_plan import AcceptancePlan, BaselinePin, baseline_pin, read_acceptance_plan, save_acceptance_plan
from reprolab.project_cli import main
from reprolab.project_demo import create_project_demo
from reprolab.project_sessions import pack_project_report, prepare_project, replay_project_report, run_project
from reprolab.replay_receipt import save_replay_receipt
from reprolab.replay_stability import replay_stability
from reprolab.runner import RunControl
from reprolab.workspace import WORKTREE


def test_frozen_plan_comparison_keeps_target_decision_and_identifies_missing_scope(tmp_path, capsys):
    from test_project_review_audit import freeze, git, write

    repo = tmp_path / 'fixture'
    repo.mkdir()
    git(repo, 'init', '-q')
    fixed, neighbor, extra = (f'test_order.py::{name}' for name in ('test_fixed', 'test_neighbor', 'test_extra'))
    write(repo, 'test_order.py',
          'from model import ready\ndef test_fixed(): assert ready\n'
          'def test_neighbor(): assert False\ndef test_extra(): assert False\n')
    write(repo, 'model.py', 'ready = False\n')
    target = AcceptancePlan('pytest', 'strict', (fixed,))
    proposed = AcceptancePlan('pytest', 'no-new-failures', (fixed,))
    filename = '.ci/plan.json'
    selection = '.ci/selection'
    write(repo, filename, json.dumps(target.record()))
    write(repo, selection, f'{fixed}\n{neighbor}\n')
    freeze(repo)
    write(repo, 'model.py', 'ready = True\n')
    write(repo, filename, json.dumps(proposed.record()))
    write(repo, selection, f'{fixed}\n{neighbor}\n{extra}\n')
    assert main(list(map(str, ('review', repo, '--before', 'HEAD', '--working-tree',
                              '--test', fixed, '--test', neighbor, '--output', tmp_path / 'runs')))) == 2
    report = json.loads(capsys.readouterr().out)
    archive = tmp_path / 'evidence.zip'
    pack_project_report(report['report_dir'], archive)
    args = ['compare-plans', str(archive), '--plan-path', filename, '--format', 'json']
    assert main(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['gate'] == result['target']['gate']
    assert result['target']['source']['revision']['commit'] == report['before_commit']
    assert result['target']['acceptance_plan_sha256'] == target.fingerprint()
    assert result['proposed']['acceptance_plan_sha256'] == proposed.fingerprint()
    assert result['target']['gate']['exit_code'] == 2
    assert result['proposed']['gate']['exit_code'] == 0
    assert result['rule_changes'] == {'policy': {'target': 'strict', 'proposed': 'no-new-failures'}}

    assert main([*args, '--selection-path', selection]) == 2
    scoped = json.loads(capsys.readouterr().out)
    assert scoped['proposed']['gate']['reason'] == 'selection_not_covered'
    assert scoped['proposed']['missing_tests'] == [extra]
    assert scoped['proposed']['rerun_selection'] == [fixed, neighbor, extra]
    assert scoped['target']['gate'] == result['target']['gate']


def test_plan_is_reusable_and_keeps_missing_requirements_visible(tmp_path, capsys):
    state = create_project_demo(tmp_path / 'sessions')
    fixed = 'tests/test_cart.py::test_discount_cannot_make_total_negative'
    stable = 'tests/test_cart.py::test_regular_total[10]'
    source = run_project(state['directory'], tmp_path / 'original', selected_ids=[fixed, stable], confirmed=True)
    source_path = Path(source['report_dir'])
    zip_path = tmp_path / 'report.zip'
    pack_project_report(source_path, zip_path)
    another = run_project(state['directory'], tmp_path / 'other', selected_ids=[stable], confirmed=True)

    plan = AcceptancePlan('pytest', 'strict', (fixed,), True, 2)
    path = tmp_path / 'acceptance.json'
    save_acceptance_plan(plan, path)
    moved = tmp_path / 'moved-plan.json'
    path.rename(moved)
    assert read_acceptance_plan(moved) == plan

    def cli(report, *flags, code):
        assert main(['summary', str(report), '--format', 'json', '--acceptance-plan', str(moved), *map(str, flags)]) == code
        streams = capsys.readouterr()
        return json.loads(streams.out or streams.err)

    blocked = cli(zip_path, code=2)
    assert blocked['gate']['status'] == 'blocked'
    assert blocked['comparison_gate']['status'] == 'passed'
    assert blocked['required_improvements'][0]['status'] == 'passed'
    assert blocked['receipt']['receipt_status'] == 'missing'
    assert blocked['stability']['verification_status'] == 'missing'
    assert blocked['acceptance_plan_sha256'] == plan.fingerprint()
    assert cli(another['report_dir'], code=2)['required_improvements'] == [
        {'test_id': fixed, 'status': 'blocked', 'outcome': None}]
    assert cli(zip_path, '--gate', 'no-new-failures', code=2)['status'] == 'error'

    replay = replay_project_report(source_path, tmp_path / 'replay', confirmed=True, verify_result=True)
    receipt = tmp_path / 'receipt'
    save_replay_receipt(replay, receipt, control=RunControl())
    result = replay_stability(source_path, tmp_path / 'stability', rounds=2, confirmed=True)
    stability = tmp_path / 'moved-stability'
    shutil.move(Path(result['summary_file']).parent, stability)
    passed = cli(zip_path, '--require-receipt', receipt, '--require-stability', stability, code=0)
    assert passed['gate']['status'] == 'passed'
    assert passed['acceptance_plan_sha256'] == blocked['acceptance_plan_sha256']
    assert str(tmp_path) not in json.dumps(passed)

    save_acceptance_plan(AcceptancePlan('go', 'strict', ('TestDiscount',)), path)
    moved.unlink()
    path.rename(moved)
    assert '当前报告使用 pytest' in cli(zip_path, code=2)['error']['message']


def test_plan_preserves_exact_vitest_names_and_refuses_ambiguous_or_incompatible_json(tmp_path):
    plan = AcceptancePlan('vitest', 'no-new-failures', (
        ('apps/shop checkout.test.ts', 'checkout > 同名用例'),
        ('other.test.ts', 'checkout > 同名用例'),
    ))
    target = tmp_path / 'plan.json'
    save_acceptance_plan(plan, target)
    original = target.read_bytes()
    with pytest.raises(ValueError, match='文件已存在'):
        save_acceptance_plan(AcceptancePlan('pytest', 'strict', ()), target)
    assert target.read_bytes() == original
    target.write_text(json.dumps(json.loads(original), sort_keys=True), encoding='utf-8')
    assert read_acceptance_plan(target).fingerprint() == plan.fingerprint()

    for change in ({'schema_version': True}, {'required_tests': ['checkout']},
                   {'require_receipt': 1}, {'min_stability_rounds': 1},
                   {'required_tests': plan.record()['required_tests'][:1] * 2}, {'unexpected': None}):
        target.write_text(json.dumps(plan.record() | change), encoding='utf-8')
        with pytest.raises(ValueError):
            read_acceptance_plan(target)
    target.write_text(original.decode().replace('"policy":', '"policy": "strict", "policy":'), encoding='utf-8')
    with pytest.raises(ValueError, match='重复字段'):
        read_acceptance_plan(target)


def test_pinned_plan_accepts_new_candidates_but_blocks_a_changed_baseline(tmp_path, capsys):
    state = create_project_demo(tmp_path / 'sessions')
    target = 'tests/test_cart.py::test_discount_cannot_make_total_negative'
    original = run_project(state['directory'], tmp_path / 'original', selected_ids=[target], confirmed=True)
    pin = baseline_pin(original)
    plan = AcceptancePlan('pytest', 'strict', (target,), baseline=pin)
    plan_path = tmp_path / 'acceptance.json'
    save_acceptance_plan(plan, plan_path)
    assert read_acceptance_plan(plan_path) == plan
    assert plan.record()['schema_version'] == 2
    assert plan.fingerprint() != AcceptancePlan('pytest', 'strict', (target,)).fingerprint()

    repo = Path(state['repository'])
    cart = repo / 'cart.py'
    candidate = cart.read_text(encoding='utf-8') + '\n# Candidate follow-up\n'
    cart.write_text(candidate, encoding='utf-8')
    updated = prepare_project(repo, state['before_commit'], WORKTREE, tmp_path / 'sessions')
    matching = run_project(updated['directory'], tmp_path / 'matching', selected_ids=[target], confirmed=True)
    assert matching['source_sha256']['after'] != original['source_sha256']['after']
    bundle = tmp_path / 'matching.zip'
    pack_project_report(matching['report_dir'], bundle)

    def check(report, path, code):
        assert main(['summary', str(report), '--acceptance-plan', str(path), '--format', 'json']) == code
        streams = capsys.readouterr()
        assert not streams.err
        return json.loads(streams.out)

    summary = check(bundle, plan_path, 0)
    assert summary['baseline_check'] == {
        'status': 'passed', 'reason': 'matched', 'expected': pin.record(), 'actual': pin.record(),
    }
    assert summary['required_improvements'][0]['outcome'] == 'improved'

    before = Path(state['directory']) / 'inputs' / 'before' / 'cart.py'
    cart.write_text(before.read_text(encoding='utf-8') + '\n# Different baseline\n', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false',
           '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid']
    subprocess.run([*git, 'add', 'cart.py'], check=True, capture_output=True)
    subprocess.run([*git, 'commit', '-qm', 'alternate baseline'], check=True, capture_output=True)
    cart.write_text(candidate, encoding='utf-8')
    updated = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    different = run_project(updated['directory'], tmp_path / 'different', selected_ids=[target], confirmed=True)
    blocked = check(different['report_dir'], plan_path, 2)
    assert blocked['comparison_gate']['status'] == blocked['required_improvements'][0]['status'] == 'passed'
    assert blocked['baseline_check'] == {'status': 'blocked', 'reason': 'different',
                                         'expected': pin.record(), 'actual': baseline_pin(different).record()}

    changed_mode_plan = tmp_path / 'changed-mode.json'
    different_mode = BaselinePin(pin.source_sha256, '0' * 64)
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (target,), baseline=different_mode), changed_mode_plan)
    mode = check(bundle, changed_mode_plan, 2)['baseline_check']
    assert mode['expected']['source_sha256'] == mode['actual']['source_sha256']
    assert mode['expected']['source_mode_sha256'] != mode['actual']['source_mode_sha256']

    payload = plan.record()
    for invalid in (payload | {'schema_version': 1}, payload | {'baseline': None},
                    payload | {'baseline': {'source_sha256': pin.source_sha256}},
                    payload | {'baseline': pin.record() | {'source_mode_sha256': 'invalid'}}):
        changed_mode_plan.write_text(json.dumps(invalid), encoding='utf-8')
        with pytest.raises(ValueError, match='字段|SHA-256'):
            read_acceptance_plan(changed_mode_plan)


def test_pytest_plan_drives_saved_discovery_and_two_version_execution(tmp_path, capsys, monkeypatch):
    from reprolab import project_sessions as sessions

    state = create_project_demo(tmp_path / 'sessions')
    fixed = 'tests/test_cart.py::test_discount_cannot_make_total_negative'
    missing = 'tests/test_cart.py::test_missing'
    source = tmp_path / 'plan.json'
    plan = AcceptancePlan('pytest', 'strict', (fixed,), baseline=baseline_pin(state))
    save_acceptance_plan(plan, source)
    def call(args, code, *, error=False):
        assert main(list(map(str, args))) == code
        streams = capsys.readouterr()
        assert bool(streams.err) == error
        return json.loads(streams.err if error else streams.out)

    check = ['selection-check', state['directory'], '--acceptance-plan']
    assert call([*check, source], 2)['status'] == 'needs_collection'
    collection = sessions.collect_project(state['directory'])
    assert fixed in collection['tests']
    matched = call([*check, source], 0)
    assert matched['matched_tests'] == [fixed] and matched['acceptance_plan_sha256'] == plan.fingerprint()
    assert matched['ready'] is True

    review = ['review', state['repository'], '--before', state['before_commit'], '--working-tree']
    report = call([*review, '--acceptance-plan', source, '--output', tmp_path / 'review'], 0)
    assert report['selected_tests'] == [fixed] and report['entries'][0]['status'] == 'improved'
    assert sessions.check_project_report(report['report_dir'])['status'] == 'consistent'
    assert call(['summary', report['report_dir'], '--acceptance-plan', source, '--format', 'json'], 0)['gate']['status'] == 'passed'
    assert 'acceptance' not in report
    combined = call([*review, '--acceptance-plan', source, '--verify-plan', '--output', tmp_path / 'verified'], 0)
    assert combined['acceptance']['gate']['status'] == 'passed'
    assert combined['acceptance']['acceptance_plan_sha256'] == plan.fingerprint()
    stable = 'tests/test_cart.py::test_regular_total[10]'
    selection = tmp_path / 'regression-tests.txt'
    selection.write_text(f'{stable}\n{fixed}\n', encoding='utf-8')
    markdown = tmp_path / 'accepted.md'
    wider = call([*review, '--acceptance-plan', source, '--verify-plan', '--selection-file', selection,
                  '--summary-file', markdown, '--output', tmp_path / 'wider'], 0)
    assert wider['selected_tests'] == [stable, fixed]
    assert wider['acceptance']['required_improvements'] == [
        {'test_id': fixed, 'status': 'passed', 'outcome': 'improved'}]
    assert Path(wider['report_dir'], 'report.json').is_file()
    assert wider['summary_file'] == str(markdown)
    assert main(['summary', wider['report_dir'], '--acceptance-plan', str(source)]) == 0
    saved_markdown = capsys.readouterr().out
    assert saved_markdown == markdown.read_text(encoding='utf-8')

    run_explicit = ['run', state['directory'], '--acceptance-plan', source, '--verify-plan']
    collision = call([*run_explicit, '--test', fixed, '--summary-file', markdown,
                      '--output', tmp_path / 'summary-collision'], 2)
    assert collision['acceptance']['gate']['status'] == 'passed'
    assert collision['summary_error']['type'] == 'FileExistsError'
    assert 'summary_file' not in collision
    assert sessions.check_project_report(collision['report_dir'])['status'] == 'consistent'
    assert saved_markdown == markdown.read_text(encoding='utf-8')

    blocked_markdown = tmp_path / 'blocked.md'
    omitted = call([*run_explicit, '--test', stable, '--summary-file', blocked_markdown,
                    '--output', tmp_path / 'omitted-goal'], 2)
    assert omitted['selected_tests'] == [stable]
    assert omitted['acceptance']['required_improvements'] == [
        {'test_id': fixed, 'status': 'blocked', 'outcome': None}]
    assert sessions.check_project_report(omitted['report_dir'])['status'] == 'consistent'
    assert omitted['summary_file'] == str(blocked_markdown)
    assert 'Missing comparable result' in blocked_markdown.read_text(encoding='utf-8')
    policy_only = tmp_path / 'policy-only.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'no-new-failures', ()), policy_only)
    existing = 'tests/test_cart.py::test_existing_tax_problem'
    policy_report = call([*review, '--test', existing, '--acceptance-plan', policy_only, '--verify-plan',
                          '--output', tmp_path / 'policy'], 0)
    assert policy_report['acceptance']['comparison_gate']['reason'] == 'existing_failures_allowed'

    pending = tmp_path / 'receipt-required.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (fixed,), require_receipt=True), pending)
    blocked_after_run = call([*review, '--acceptance-plan', pending, '--verify-plan',
                              '--output', tmp_path / 'receipt-pending'], 2)
    assert blocked_after_run['status'] == 'regression_passed'
    assert blocked_after_run['acceptance']['receipt']['receipt_status'] == 'missing'
    assert Path(blocked_after_run['report_dir'], 'report.json').is_file()
    assert call([*review, '--test', fixed, '--verify-plan', '--output', tmp_path / 'no-plan'],
                2, error=True)['status'] == 'error'
    assert not (tmp_path / 'no-plan').exists()
    assert call([*review, '--acceptance-plan', source, '--test', fixed, '--output', tmp_path / 'mixed'],
                2, error=True)['status'] == 'error'
    assert not (tmp_path / 'mixed').exists()
    comparison_markdown = tmp_path / 'comparison.md'
    unplanned = call([*review, '--test', fixed, '--summary-file', comparison_markdown,
                      '--output', tmp_path / 'no-verification'], 0)
    assert 'acceptance' not in unplanned
    assert unplanned['summary_file'] == str(comparison_markdown)
    assert 'ReproLab comparison' in comparison_markdown.read_text(encoding='utf-8')
    unused = tmp_path / 'unused.md'

    missing_plan = tmp_path / 'missing.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (fixed, missing)), missing_plan)
    run = ['run', state['directory'], '--output', tmp_path / 'reports', '--acceptance-plan']
    blocked = call([*run, missing_plan], 2)
    assert blocked['status'] == 'missing_targets' and blocked['missing_tests'] == [missing]
    assert blocked['matched_tests'] == [fixed] and not (tmp_path / 'reports').exists()
    assert Path(blocked['project_directory']) == Path(state['directory']).resolve()

    wrong_plan = tmp_path / 'wrong.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (fixed,), baseline=BaselinePin('0' * 64, '0' * 64)),
                         wrong_plan)

    def unexpected_collection(*_args, **_kwargs):
        raise AssertionError('locked baseline mismatch must stop before discovery')

    monkeypatch.setattr(sessions, 'collect_project', unexpected_collection)
    blocked = call([*review, '--acceptance-plan', wrong_plan, '--output', tmp_path / 'wrong'], 2)
    assert blocked['status'] == 'baseline_mismatch' and Path(blocked['project_directory']).is_dir()
    assert not (tmp_path / 'wrong' / 'reports').exists()
    stopped = call([*review, '--test', stable, '--acceptance-plan', wrong_plan, '--verify-plan',
                    '--summary-file', unused, '--output', tmp_path / 'wrong-explicit'], 2)
    assert stopped['status'] == 'baseline_mismatch' and stopped['requested_tests'] == [stable]
    assert not (tmp_path / 'wrong-explicit' / 'reports').exists()
    assert not unused.exists()
    monkeypatch.setattr(sessions, '_collection', lambda *_args: {**collection, 'partial': True})
    blocked = call([*run, source], 2)
    assert blocked['status'] == 'incomplete_collection' and blocked['matched_tests'] == [fixed]
    assert not (tmp_path / 'reports').exists()
