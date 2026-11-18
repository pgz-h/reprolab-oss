"""Maintainer-facing review checks over frozen, portable inputs."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from reprolab.project_cli import main


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    return root


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def freeze(root):
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'baseline')


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')


@pytest.fixture
def cli(capsys):
    def run(*args, code=0):
        status = main(list(map(str, args)))
        output = capsys.readouterr()
        assert status == code, (output.err, output.out[-1800:])
        return json.loads(output.out)
    return run


def review(cli, repo, output, *args, code=0):
    return cli('review', repo, '--before', 'HEAD', '--working-tree', '--python', sys.executable,
               '--output', output, *args, code=code)


def bundle(cli, report, path):
    cli('pack', report['report_dir'], '--output', path)
    return path


@pytest.mark.parametrize(('change', 'reason'), [
    ('remove', 'source_file_absent'),
    ('rename', 'possible_rename'),
    ('rename_function', 'possible_rename'),
    ('partial', None),
])
def test_automatic_scope_compares_native_inventories(repo, cli, tmp_path, change, reason):
    write(repo, 'test_charge.py', 'def test_charge():\n    assert True\n')
    write(repo, 'test_refund.py', ('import missing_baseline_dependency\n' if change == 'partial' else '')
          + 'def test_refund():\n    assert True\n')
    freeze(repo)
    if change == 'rename':
        (repo / 'test_refund.py').rename(repo / 'test_moved.py')
    elif change == 'rename_function':
        write(repo, 'test_refund.py', 'def test_repayment():\n    assert True\n')
    else:
        (repo / 'test_refund.py').unlink()

    result = review(cli, repo, tmp_path / 'runs', code=2)
    assert result['counts'] == {'regression_passed': 2 if change.startswith('rename') else 1}
    evidence = bundle(cli, result, tmp_path / 'report.zip')
    summary = cli('summary', evidence, '--format', 'json', '--include-test-ids', '--gate', 'strict', code=2)
    audit = summary['discovery_audit']
    assert summary['gate']['reason'] == 'pytest_discovery_scope'
    if reason:
        assert audit['baseline_only'][0]['test_id'] == 'test_refund.py::test_refund'
        assert audit['baseline_only'][0]['reason'] == reason
    else:
        assert audit['status'] == 'incomplete'
        assert audit['baseline_only'] == []


def test_same_red_failure_type_change_blocks_permissive_gate(repo, cli, tmp_path):
    write(repo, 'refund.py', 'def refund(amount):\n    return amount - 1\n')
    write(repo, 'test_refund.py', 'from refund import refund\ndef test_refund():\n    assert refund(5) == 5\n')
    freeze(repo)
    write(repo, 'refund.py', 'def refund(amount):\n    return amount + "USD"\n')
    result = review(cli, repo, tmp_path / 'runs', '--test', 'test_refund.py::test_refund', code=2)
    assert result['counts'] == {'baseline_failure': 1}
    evidence = bundle(cli, result, tmp_path / 'report.zip')
    summary = cli('summary', evidence, '--format', 'json', '--include-test-ids', '--gate', 'no-new-failures', code=2)
    assert summary['gate']['reason'] == 'failure_shape_changed'
    assert summary['failure_changes'][0]['before'] == [{'phase': 'call', 'type': 'AssertionError'}]
    assert summary['failure_changes'][0]['after'] == [{'phase': 'call', 'type': 'TypeError'}]


def test_promoted_plan_gates_scope_and_keeps_passing_after_merge(repo, cli, tmp_path):
    from reprolab.acceptance_plan import AcceptancePlan, read_acceptance_plan, save_acceptance_plan

    fixed = 'test_refund.py::test_refund'
    other = 'test_refund.py::test_zero'
    write(repo, 'refund.py', 'def refund(amount):\n    return amount - 1 if amount else 0\n')
    write(repo, 'test_refund.py',
          'from refund import refund\ndef test_refund():\n    assert refund(5) == 5\n'
          'def test_zero():\n    assert refund(0) == 0\n')
    freeze(repo)
    write(repo, 'refund.py', 'def refund(amount):\n    return amount\n')
    initial = review(cli, repo, tmp_path / 'initial', '--test', fixed, '--test', other)
    evidence = bundle(cli, initial, tmp_path / 'initial.zip')
    repair = tmp_path / 'repair.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (fixed,)), repair)
    protected = tmp_path / 'protected.json'
    cli('protect-plan', evidence, repair, '--output', protected)
    plan = read_acceptance_plan(protected)
    assert plan.targets == () and plan.required_passes == (fixed,)
    assert plan.approved_selection == (fixed, other)

    freeze(repo)  # The fix is now the baseline.
    after_merge = review(cli, repo, tmp_path / 'merged', '--acceptance-plan', protected, '--verify-plan')
    assert after_merge['selected_tests'] == [fixed, other]
    assert after_merge['acceptance']['required_passes'][0]['status'] == 'passed'
    narrowed = review(cli, repo, tmp_path / 'narrowed', '--test', fixed, '--acceptance-plan',
                      protected, '--verify-plan', code=2)
    assert narrowed['acceptance']['required_passes'][0]['status'] == 'passed'
    assert narrowed['acceptance']['approved_scope']['missing_tests'] == [other]

    write(repo, 'refund.py', 'def refund(amount):\n    return amount - 1 if amount else 0\n')
    broken = review(cli, repo, tmp_path / 'broken', '--acceptance-plan', protected, '--verify-plan', code=1)
    assert broken['acceptance']['required_passes'][0]['status'] == 'failed'


def test_baseline_fixture_replay_exposes_shrunk_input(repo, cli, tmp_path):
    test = 'test_amount.py::test_amount'
    write(repo, 'amount.py', 'def total(amount):\n    return amount - 10\n')
    write(repo, 'conftest.py', 'import pytest\n@pytest.fixture\ndef amount(): return 5\n')
    write(repo, 'test_amount.py', 'from amount import total\ndef test_amount(amount):\n    assert total(amount) >= 0\n')
    freeze(repo)
    write(repo, 'conftest.py', 'import pytest\n@pytest.fixture\ndef amount(): return 20\n')
    result = review(cli, repo, tmp_path / 'normal', '--test', test, code=2)
    assert result['counts'] == {'improved': 1}
    assert result['comparison_scope']['reason'] == 'pytest_fixture_changed'
    evidence = bundle(cli, result, tmp_path / 'original.zip')
    args = ('fixture-control', evidence, '--file', 'conftest.py', '--output', tmp_path / 'diagnosis')
    preview = cli(*args)
    assert preview['fixture_control']['files'][0]['path'] == 'conftest.py'
    repeated = cli(*args, '--confirm-run')
    assert repeated['counts'] == {'baseline_failure': 1}
    assert repeated['fixture_control']['source_report_sha256'] == preview['fixture_control']['source_report_sha256']
    diagnosed = bundle(cli, repeated, tmp_path / 'diagnosed.zip')
    report = cli('summary', diagnosed, '--format', 'json', '--include-test-ids', '--gate', 'no-new-failures', code=2)
    assert report['gate']['reason'] == 'pytest_fixture_control'
    assert cli('replay', diagnosed, '--confirm-run', '--verify-result',
               '--output', tmp_path / 'replay')['replay_verification']['status'] == 'verified'


def test_cancelled_baseline_discovery_keeps_confirmed_regression(repo, tmp_path):
    from reprolab.project_sessions import check_project_report, prepare_project, run_project
    from reprolab.runner import RunControl

    write(repo, 'price.py', 'PRICE = 5\n')
    write(repo, 'test_price.py', 'from price import PRICE\ndef test_price(): assert PRICE == 5\n')
    freeze(repo)
    write(repo, 'price.py', 'PRICE = 6\n')
    state = prepare_project(repo, 'HEAD', '@working-tree', tmp_path / 'sessions')

    def progress(phase, payload):
        if phase == 'project_progress' and payload['side'] == 'baseline-discovery':
            control.cancel()

    control = RunControl(progress=progress)
    report = run_project(state['directory'], tmp_path / 'runs', confirmed=True, control=control)
    assert report['status'] == 'regression_detected'
    assert report['discovery_audit']['status'] == 'incomplete'
    assert check_project_report(report['report_dir'])['status'] == 'consistent'


@pytest.mark.parametrize(('case', 'outcome', 'code'), [
    ('spurious', 'candidate_failed', 0),
    ('actual', 'candidate_passed', 0),
    ('unused', 'incomplete', 2),
])
def test_captured_failure_input_is_replayed_in_both_workers(repo, cli, capsys, tmp_path, case, outcome, code):
    test = 'test_balance.py::test_balance'
    write(repo, 'service.py', 'def balance(amount): return amount - 10\n')
    write(repo, 'generator.py', 'new = False\ndef sample(): return {"amount": 5, "items": [1]}\n')
    write(repo, 'test_balance.py', 'from service import balance\nfrom generator import new, sample\n'
          'def test_balance(reprolab_input):\n'
          f'    value = sample() if new and {case == "unused"!r} else reprolab_input("transaction", sample)\n'
          '    value["items"].append(2)\n'
          '    assert balance(value["amount"]) >= 0\n')
    freeze(repo)
    write(repo, 'generator.py', 'new = True\ndef sample(): return {"amount": 20, "items": [1]}\n')
    if case == 'actual':
        write(repo, 'service.py', 'def balance(amount): return max(0, amount - 10)\n')
    original = review(cli, repo, tmp_path / 'original', '--test', test, code=2)
    assert original['counts'] == {'improved': 1}
    assert original['comparison_scope']['reason'] == 'pytest_input_changed'
    raw = json.loads(Path(original['report_dir'], 'before.events.json').read_text())
    assert raw['input_samples']['values'][test]['transaction'] == {'amount': 5, 'items': [1]}
    source = bundle(cli, original, tmp_path / 'original.zip')
    accepted = cli('summary', source, '--require-improvement', test, '--format', 'json', code=2)
    assert accepted['required_improvements'][0]['reason'] == 'pytest_input_changed'

    output = tmp_path / 'control'
    args = ('input-control', source, '--output', output)
    preview = cli(*args)
    assert preview['input_control']['targets'][0]['test_id'] == test
    assert not output.exists()
    rerun = cli(*args, '--confirm-run', code=code)
    assert rerun['input_diagnosis']['entries'][0]['pattern'] == outcome
    evidence = bundle(cli, rerun, tmp_path / 'control.zip')
    if case != 'unused':
        raw = json.loads(Path(rerun['report_dir'], 'after.events.json').read_text())
        assert raw['input_samples']['values'][test]['transaction'] == {'amount': 5, 'items': [1]}
    else:
        assert rerun['entries'][0]['after']['input_error'] == 'replay_mismatch'
    checked = cli('check-input-control', source, evidence, code=code)
    assert checked == rerun['input_diagnosis']
    cli('summary', evidence, '--gate', 'no-new-failures', '--format', 'json', code=2)

    if case != 'actual':
        from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

        repair = tmp_path / 'repair-plan.json'
        output = tmp_path / 'cannot-promote.json'
        save_acceptance_plan(AcceptancePlan('pytest', 'strict', (test,)), repair)
        denied = cli('protect-plan', source, repair, '--input-control', evidence,
                     '--output', output, code=1 if case == 'spurious' else 2)
        assert denied['input_promotion']['entries'][0]['pattern'] == outcome
        assert not output.exists()

    if case == 'spurious':
        proof = cli('replay', evidence, '--confirm-run', '--verify-result', '--output', tmp_path / 'replay')
        assert proof['replay_verification']['inputs']['recorded_inputs'] is True
        assert proof['replay_verification']['status'] == 'verified'

        other = review(cli, repo, tmp_path / 'other', '--test', test, code=2)
        assert main(['check-input-control', other['report_dir'], str(evidence)]) == 2
        assert '与来源报告或基线样本不一致' in capsys.readouterr().err


def test_missing_requested_input_is_blocked_even_when_test_catches_error(repo, cli, tmp_path):
    test = 'test_capture.py::test_capture'
    write(repo, 'generator.py', 'new = False\n')
    write(repo, 'test_capture.py', 'from generator import new\n'
          'def test_capture(reprolab_input):\n'
          '    try:\n'
          '        reprolab_input("new" if new else "old", lambda: 5)\n'
          '    except ValueError:\n'
          '        pass\n'
          '    assert new\n')
    freeze(repo)
    write(repo, 'generator.py', 'new = True\n')
    original = review(cli, repo, tmp_path / 'original', '--test', test, code=2)
    source = bundle(cli, original, tmp_path / 'original.zip')
    rerun = cli('input-control', source, '--confirm-run', '--output', tmp_path / 'control', code=2)
    row = rerun['entries'][0]
    assert row['after']['status'] == 'inconclusive'
    assert row['after']['input_error'] == 'unrecorded_key'


def test_unsupported_captured_value_is_evidence_blocked(repo, cli, tmp_path):
    test = 'test_bad.py::test_bad'
    write(repo, 'test_bad.py',
          'def test_bad(reprolab_input):\n    reprolab_input("payload", lambda: (5, 6))\n')
    freeze(repo)
    report = review(cli, repo, tmp_path / 'runs', '--test', test, code=2)
    assert report['entries'][0]['after']['observed_status'] == 'failed'
    assert report['entries'][0]['after']['input_error'] == 'invalid_value'
    assert cli('check', report['report_dir'])['status'] == 'consistent'


def test_same_outcome_replay_detects_different_recorded_business_inputs(repo, cli, tmp_path, monkeypatch):
    test = 'test_configured.py::test_configured'
    write(repo, 'pyproject.toml', '[tool.reprolab.pytest]\npass_env = ["EXAMPLE_VALUE"]\n')
    write(repo, 'test_configured.py', 'import os\ndef test_configured(reprolab_input):\n'
          '    value = reprolab_input("configured", lambda: int(os.environ["EXAMPLE_VALUE"]))\n'
          '    assert value > 0\n')
    freeze(repo)
    monkeypatch.setenv('EXAMPLE_VALUE', '5')
    initial = review(cli, repo, tmp_path / 'initial', '--test', test)
    source = bundle(cli, initial, tmp_path / 'source.zip')
    monkeypatch.setenv('EXAMPLE_VALUE', '20')
    replay = cli('replay', source, '--confirm-run', '--verify-result', '--output', tmp_path / 'replay', code=2)
    assert replay['counts'] == initial['counts'] == {'regression_passed': 1}
    proof = replay['replay_verification']
    assert proof['status'] == 'inputs_changed'
    assert proof['inputs']['recorded_inputs'] is False


def test_worker_does_not_shadow_project_input_samples_module(repo, cli, tmp_path):
    write(repo, '_input_samples.py', 'def native_sample(): return 5\n')
    write(repo, 'test_native.py', 'from _input_samples import native_sample\n'
          'def test_native(reprolab_input):\n'
          '    assert reprolab_input("amount", native_sample) == 5\n')
    freeze(repo)
    report = review(cli, repo, tmp_path / 'runs', '--test', 'test_native.py::test_native')
    assert report['counts'] == {'regression_passed': 1}
    assert report['after']['module_origins']['_input_samples'] == ['snapshot:_input_samples.py']


def test_protected_input_plan_reuses_verified_failure_sample_after_merge(repo, cli, tmp_path):
    from reprolab.acceptance_plan import AcceptancePlan, read_acceptance_plan, save_acceptance_plan

    test = 'test_balance.py::test_balance'
    write(repo, 'service.py', 'def balance(amount): return amount - 10\n')
    write(repo, 'generator.py', 'def sample(): return 5\n')
    write(repo, 'test_balance.py', 'from service import balance\nfrom generator import sample\n'
          'def test_balance(reprolab_input):\n    assert balance(reprolab_input("amount", sample)) >= 0\n')
    freeze(repo)
    write(repo, 'generator.py', 'def sample(): return 20\n')
    write(repo, 'service.py', 'def balance(amount): return max(0, amount - 10)\n')
    initial = review(cli, repo, tmp_path / 'initial', '--test', test, code=2)
    source = bundle(cli, initial, tmp_path / 'source.zip')
    control = cli('input-control', source, '--confirm-run', '--output', tmp_path / 'controlled')
    controlled = bundle(cli, control, tmp_path / 'controlled.zip')
    repair = tmp_path / 'repair.json'
    protection = tmp_path / 'protection.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (test,)), repair)
    cli('protect-plan', source, repair, '--input-control', controlled, '--output', protection)
    plan = read_acceptance_plan(protection)
    assert plan.record()['schema_version'] == 4
    assert plan.required_passes == (test,)
    assert plan.protected_inputs['values'] == {test: {'amount': 5}}

    freeze(repo)
    common = ('--acceptance-plan', protection, '--verify-plan')
    markdown = tmp_path / 'protected.md'
    passed = review(cli, repo, tmp_path / 'protected', *common, '--summary-file', markdown)
    assert passed['acceptance']['protected_inputs']['status'] == 'passed'
    archive = bundle(cli, passed, tmp_path / 'protected.zip')
    assert cli('check-summary', archive, markdown, '--acceptance-plan', protection)['status'] == 'consistent'
    assert cli('summary', archive, '--acceptance-plan', protection, '--format', 'json')['gate']['status'] == 'passed'
    wrong = tmp_path / 'changed-plan.json'
    data = plan.record()
    data['protected_inputs']['values'][test]['amount'] = 20
    wrong.write_text(json.dumps(data), encoding='utf-8')
    assert cli('summary', archive, '--acceptance-plan', wrong, '--format', 'json', code=2
               )['protected_inputs']['status'] == 'blocked'
    cli('summary', archive, '--gate', 'strict', '--format', 'json', code=2)

    write(repo, 'service.py', 'def balance(amount): return amount - 10\n')
    natural = review(cli, repo, tmp_path / 'natural', '--test', test)
    assert natural['counts'] == {'regression_passed': 1}
    assert cli('summary', natural['report_dir'], '--acceptance-plan', protection, '--format', 'json', code=2
               )['protected_inputs']['status'] == 'blocked'
    guarded = review(cli, repo, tmp_path / 'guarded', *common, code=1)
    assert guarded['acceptance']['protected_inputs']['entries'][0]['status'] == 'failed'
    assert guarded['counts'] == {'regression_detected': 1}

    write(repo, 'service.py', 'def balance(amount): return max(0, amount - 10)\n')
    write(repo, 'test_balance.py', 'def test_balance(): assert True\n')
    unused = review(cli, repo, tmp_path / 'unused', *common, code=2)
    assert unused['acceptance']['protected_inputs']['entries'][0]['status'] == 'blocked'


def test_input_protection_promotion_keeps_an_unrelated_confirmed_regression(repo, cli, tmp_path):
    from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

    repaired = 'test_service.py::test_balance'
    regressed = 'test_service.py::test_receipt'
    write(repo, 'service.py', 'def balance(amount): return amount - 10\nPRICE = 5\n')
    write(repo, 'generator.py', 'def sample(): return 5\n')
    write(repo, 'test_service.py', 'from service import balance, PRICE\nfrom generator import sample\n'
          'def test_balance(reprolab_input):\n    assert balance(reprolab_input("amount", sample)) >= 0\n'
          'def test_receipt(): assert PRICE == 5\n')
    freeze(repo)
    write(repo, 'generator.py', 'def sample(): return 20\n')
    write(repo, 'service.py', 'def balance(amount): return max(0, amount - 10)\nPRICE = 6\n')
    orig = review(cli, repo, tmp_path / 'original', '--test', repaired, '--test', regressed, code=1)
    source = bundle(cli, orig, tmp_path / 'original.zip')
    controlled = cli('input-control', source, '--confirm-run', '--output', tmp_path / 'controlled', code=1)
    assert controlled['input_diagnosis']['entries'][0]['pattern'] == 'candidate_passed'

    plan = tmp_path / 'repair.json'
    output = tmp_path / 'protection.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (repaired,)), plan)
    result = cli('protect-plan', source, plan, '--input-control', controlled['report_dir'],
                 '--output', output, code=1)
    assert result['comparison_gate']['reason'] == 'new_failures'
    assert not output.exists()


@pytest.mark.parametrize(('revision', 'expected', 'changed'), [
    ('direct', 2, ['tests/test_balance.py']),
    ('helper', 2, ['tests/helpers.py']),
    ('equivalent', 0, []),
    ('refactor', 2, ['tests/test_balance.py']),
])
def test_test_contract_exposes_assertion_changes(repo, cli, tmp_path, revision, expected, changed):
    from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

    target = 'tests/test_balance.py::test_balance'
    helper = 'def valid(amount): return amount >= 0\n'
    check = ('from service import balance\nfrom tests.helpers import valid\n'
             'def test_balance(reprolab_input):\n'
             '    assert valid(balance(reprolab_input("amount", lambda: 20)))\n')
    write(repo, 'service.py', 'def balance(amount): return max(0, amount - 10)\n')
    write(repo, 'tests/test_balance.py', check)
    write(repo, 'tests/__init__.py', '')
    write(repo, 'tests/helpers.py', helper)
    freeze(repo)
    prior = tmp_path / 'v4.json'
    plan = tmp_path / 'v5.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (), required_passes=(target,),
                                        approved_selection=(target,), protected_inputs={
        'source_report_sha256': 'a' * 64, 'control_report_sha256': 'b' * 64, 'values': {target: {'amount': 5}},
    }), prior)
    original = review(cli, repo, tmp_path / 'original', '--acceptance-plan', prior, '--verify-plan')
    archive = bundle(cli, original, tmp_path / 'original.zip')
    cli('pin-test-contract', archive, prior, '--output', plan)
    if revision == 'direct':
        write(repo, 'tests/test_balance.py', check.replace('assert valid(balance', 'assert (balance')
              .replace('lambda: 20)))', 'lambda: 20))) is not None'))
    elif revision == 'helper':
        write(repo, 'tests/helpers.py', helper.replace('amount >= 0', 'amount is not None'))
    elif revision == 'equivalent':
        write(repo, 'tests/test_balance.py', check.replace('assert valid(', 'assert (valid(')
              .replace('lambda: 20)))', 'lambda: 20))))  # equivalent'))
    else:
        write(repo, 'tests/test_balance.py', check.replace('    assert valid(balance(reprolab_input("amount", lambda: 20)))',
              '    result = balance(reprolab_input("amount", lambda: 20))\n    assert valid(result)'))
    if revision in ('direct', 'helper'):
        write(repo, 'service.py', 'def balance(amount): return amount - 10\n')
        ordinary = review(cli, repo, tmp_path / 'false-green', '--acceptance-plan', prior, '--verify-plan')
        assert ordinary['counts'] == {'regression_passed': 1}
    if revision == 'direct':
        weak = bundle(cli, ordinary, tmp_path / 'false-green.zip')
        control = cli('test-source-control', weak, '--confirm-run', '--output', tmp_path / 'source-control', code=1)
        old = bundle(cli, control, tmp_path / 'source-control.zip')
        proof = cli('check-test-source-control', weak, old, code=1)
        assert proof['entries'][0]['candidate_tests'] == {'before': 'passed', 'after': 'passed'}
        assert proof['entries'][0]['baseline_tests'] == {'before': 'passed', 'after': 'failed'}
    markdown = tmp_path / 'guarded.md'
    guarded = review(cli, repo, tmp_path / 'guarded', '--acceptance-plan', plan, '--verify-plan',
                     '--summary-file', markdown, code=expected)
    contract = guarded['acceptance']['test_contract']
    assert [item['path'] for item in contract['changes']] == changed
    assert guarded['acceptance']['protected_inputs']['status'] == 'passed'
    replay = bundle(cli, guarded, tmp_path / 'guarded.zip')
    check_summary = cli('check-summary', replay, markdown, '--acceptance-plan', plan)
    assert check_summary['gate']['exit_code'] == expected


def test_pytest_same_id_different_parameters_are_not_a_confirmed_fix(repo, cli, tmp_path):
    target = 'test_amount.py::test_amount[same]'
    write(repo, 'amount.py', 'AMOUNT = 5\ndef balance(amount): return amount - 10\n')
    write(repo, 'test_amount.py', 'import pytest\nfrom amount import AMOUNT, balance\n'
          '@pytest.mark.parametrize("amount", [AMOUNT], ids=["same"])\n'
          'def test_amount(amount): assert balance(amount) >= 0\n')
    freeze(repo)
    write(repo, 'amount.py', 'AMOUNT = 20\ndef balance(amount): return amount - 10\n')
    result = review(cli, repo, tmp_path / 'reports', '--test', target, code=2)
    assert result['counts'] == {'improved': 1}
    assert result['comparison_scope']['reason'] == 'pytest_parameter_changed'
    evidence = bundle(cli, result, tmp_path / 'report.zip')
    summary = cli('summary', evidence, '--require-improvement', target, '--include-test-ids', '--format', 'json', code=2)
    assert summary['required_improvements'][0]['reason'] == 'pytest_parameter_changed'
    assert summary['parameter_changes'] == [{'test_id': target, 'status': 'improved', 'names': ['amount']}]


def test_pr_default_reports_distinct_long_failure_ids_without_parameter_values(repo, cli, tmp_path, capsys):
    predicate = 'test_value.py::test_value['
    values = ['private-' + 'x' * 280 + str(i) for i in (1, 2)]
    write(repo, 'value.py', 'OK = True\n')
    write(repo, 'test_value.py', 'import pytest\nfrom value import OK\n'
          f'@pytest.mark.parametrize("val", {values!r})\n'
          'def test_value(val): assert OK\n')
    freeze(repo)
    write(repo, 'value.py', 'OK = False\n')
    report = review(cli, repo, tmp_path / 'runs', code=1)
    source = bundle(cli, report, tmp_path / 'report.zip')
    summary = cli('summary', source, '--format', 'json', '--gate', 'strict', code=1)
    labels = [row['test_id'] for row in summary['new_failure_tests']['items']]
    assert len(labels) == len(set(labels)) == 2
    assert all(label.startswith(predicate + 'sha256:') and 'private' not in label for label in labels)
    detailed = cli('summary', source, '--format', 'json', '--include-test-ids', '--gate', 'strict', code=1)
    assert len({item['test_id'] for item in detailed['details']['items']}) == 2

    path = tmp_path / 'review.md'
    assert main(['summary', str(source), '--gate', 'strict']) == 1
    path.write_text(capsys.readouterr().out, encoding='utf-8')
    facts = tmp_path / 'facts.json'
    facts.write_text(json.dumps(summary), encoding='utf-8')
    args = ('check-summary', source, path, '--gate', 'strict', '--facts-file', facts)
    assert cli(*args)['facts_status'] == 'consistent'
    path.write_text(path.read_text().replace('Newly failing tests', 'Edited heading'), encoding='utf-8')
    changed = cli(*args, code=2)
    assert changed['facts_status'] == 'consistent' and changed['markdown_status'] == 'different'
    summary['counts']['regression_detected'] = 0
    facts.write_text(json.dumps(summary), encoding='utf-8')
    assert cli(*args, code=2)['facts_status'] == 'different'


def test_expected_revisions_block_stale_green_report_and_keep_red(repo, cli, tmp_path):
    write(repo, 'value.py', 'OK = True\n')
    write(repo, 'test_value.py', 'from value import OK\ndef test_value(): assert OK\n')
    freeze(repo)
    passing = review(cli, repo, tmp_path / 'passed', '--test', 'test_value.py::test_value')
    base = passing['before_commit']
    current = 'sha256:' + passing['source_sha256']['after']
    assert cli('summary', passing['report_dir'], '--format', 'json', '--expect-before', base,
               '--expect-after', current)['gate']['status'] == 'passed'
    write(repo, 'value.py', 'OK = False\n')
    failed = review(cli, repo, tmp_path / 'failed', '--test', 'test_value.py::test_value', code=1)
    changed = 'sha256:' + failed['source_sha256']['after']
    stale = cli('summary', passing['report_dir'], '--format', 'json', '--expect-after', changed, code=2)
    assert not stale['expected_bindings']['after']['matches']
    mismatch = cli('summary', failed['report_dir'], '--format', 'json', '--expect-after', current, code=1)
    assert not mismatch['expected_bindings']['after']['matches']


def test_replay_records_failure_type_changes_for_same_red_outcomes(repo, cli, tmp_path, monkeypatch):
    test = 'test_failure.py::test_failure'
    write(repo, 'pyproject.toml', '[tool.reprolab.pytest]\npass_env = ["EXAMPLE_FAILURE_TYPE"]\n')
    write(repo, 'test_failure.py', 'import os\ndef test_failure():\n'
          '    if os.environ["EXAMPLE_FAILURE_TYPE"] == "type": raise TypeError("unexpected")\n'
          '    raise AssertionError("original")\n')
    freeze(repo)
    monkeypatch.setenv('EXAMPLE_FAILURE_TYPE', 'assertion')
    original = review(cli, repo, tmp_path / 'original', '--test', test, code=2)
    bundle_path = bundle(cli, original, tmp_path / 'original.zip')
    monkeypatch.setenv('EXAMPLE_FAILURE_TYPE', 'type')
    replay = cli('replay', bundle_path, '--confirm-run', '--verify-result',
                 '--output', tmp_path / 'replayed', code=1)
    proof = replay['replay_verification']
    assert proof['status'] == 'different'
    assert proof['entries'][0]['reason'] == 'failure_shape_changed'
    assert proof['entries'][0]['failure_changes']['after']['replay'][0]['type'] == 'TypeError'


def test_diff_prioritizes_removed_paths_and_explains_line_endings(repo, cli, tmp_path):
    write(repo, 'zz_removed.txt', 'critical\n')
    write(repo, 'line.txt', 'first\nsecond\n')
    write(repo, 'test_pass.py', 'def test_pass(): assert True\n')
    freeze(repo)
    for i in range(25):
        write(repo, f'aaa{i:02}.txt', 'new\n')
    (repo / 'zz_removed.txt').unlink()
    (repo / 'line.txt').write_bytes(b'first\r\nsecond\r\n')
    report = review(cli, repo, tmp_path / 'runs', '--test', 'test_pass.py::test_pass')
    changes = report['changes']
    assert changes['total'] == 27 and changes['truncated']
    assert changes['files'][0]['path'] == 'zz_removed.txt'
    assert changes['files'][1]['path'] == 'line.txt'
    assert '逐行文本相同' in changes['files'][1]['note']


def test_recommendation_uses_qualified_imports_and_actual_discovery(repo, cli, tmp_path):
    write(repo, 'left/__init__.py', '')
    write(repo, 'right/__init__.py', '')
    write(repo, 'left/calc.py', 'VALUE = 1\n')
    write(repo, 'right/calc.py', 'VALUE = 1\n')
    write(repo, 'pytest.ini', '[pytest]\npython_files = test_*.py check_*.py\n')
    write(repo, 'test_calc.py', 'from right.calc import VALUE\ndef test_unrelated(): assert VALUE == 1\n')
    write(repo, 'check_math.py', 'from left.calc import VALUE as observed\ndef test_related(): assert observed >= 1\n')
    freeze(repo)
    write(repo, 'left/calc.py', 'VALUE = 2\n')
    session = cli('prepare', repo, '--before', 'HEAD', '--working-tree', '--output', tmp_path / 'sessions')
    assert session['recommendations'] == {}
    discovery = cli('collect', session['directory'])
    assert discovery['tests'] == ['check_math.py::test_related', 'test_calc.py::test_unrelated']
    assert set(discovery['recommendations']) == {'check_math.py::test_related'}
    assert 'left.calc' in discovery['recommendations']['check_math.py::test_related']


def test_projects_restore_lost_run_cache_and_find_moved_reports(repo, cli, tmp_path):
    import shutil

    write(repo, 'test_ok.py', 'def test_ok(): assert True\n')
    freeze(repo)
    report = review(cli, repo, tmp_path / 'original', '--test', 'test_ok.py::test_ok')
    source = Path(report['report_dir'])
    folder = tmp_path / 'moved'
    folder.mkdir()
    destination = folder / source.name
    shutil.move(source, destination)
    session = Path(report['project_directory'])
    index = session / 'project.json'
    original = index.read_bytes()
    listed = cli('runs', session, '--reports', folder)
    assert index.read_bytes() == original
    assert listed['missing_cached_reports'] == 1 and not listed['index_rebuilt']
    assert listed['runs'][0]['report_dir'] == str(destination)
    assert cli('runs', session, '--reports', folder, '--rebuild')['index_rebuilt']
    assert json.loads(index.read_bytes())['runs'][0]['report_dir'] == str(destination)


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
def test_pytest_timeout_summary_names_last_observed_phase(repo, cli, tmp_path, phase):
    write(repo, 'service.py', 'NAUGHTY = False\n')
    write(repo, 'test_time.py', 'import pytest\nimport time\nfrom service import NAUGHTY\n'
          '@pytest.fixture\ndef phases():\n'
          f'    if NAUGHTY and {phase == "setup"!r}: time.sleep(30)\n'
          '    yield\n'
          f'    if NAUGHTY and {phase == "teardown"!r}: time.sleep(30)\n'
          'def test_time(phases):\n'
          f'    if NAUGHTY and {phase == "call"!r}: time.sleep(30)\n')
    freeze(repo)
    write(repo, 'service.py', 'NAUGHTY = True\n')
    report = review(cli, repo, tmp_path / 'runs', '--test', 'test_time.py::test_time', '--timeout', 1.5, code=2)
    archive = bundle(cli, report, tmp_path / 'time.zip')
    summary = cli('summary', archive, '--gate', 'strict', '--format', 'json', '--include-test-ids', code=2)
    assert summary['pytest_timeouts']['after']['last_phase'] == phase


def test_pytest_preview_does_not_load_external_project_code(repo, cli, tmp_path):
    marker = tmp_path / 'side-effect'
    write(repo, 'test_plain.py', f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'
          'def test_plain(): assert True\n')
    freeze(repo)
    original = review(cli, repo, tmp_path / 'runs', '--test', 'test_plain.py::test_plain')
    assert marker.exists()
    marker.unlink()
    archive = bundle(cli, original, tmp_path / 'evidence.zip')
    cli('summary', archive, '--format', 'json')
    cli('replay', archive)
    cli('unpack', archive, tmp_path / 'imported')
    assert not marker.exists()
    cli('replay', archive, '--confirm-run', '--output', tmp_path / 'replay')
    assert marker.exists()


def test_worktree_includes_unignored_untracked_inputs_but_commit_does_not(repo, cli, tmp_path):
    write(repo, '.gitignore', 'local-only.txt\n')
    write(repo, 'test_plain.py', 'def test_plain(): assert True\n')
    freeze(repo)
    write(repo, 'untracked.txt', 'new input\n')
    write(repo, 'local-only.txt', 'ignored input\n')
    worktree = cli('prepare', repo, '--before', 'HEAD', '--working-tree', '--output', tmp_path / 'worktree')
    committed = cli('prepare', repo, '--before', 'HEAD', '--after', 'HEAD', '--output', tmp_path / 'committed')
    assert [item['path'] for item in worktree['changed_files']] == ['untracked.txt']
    assert committed['changed_files'] == []
    frozen = Path(worktree['directory'], 'inputs/after')
    assert (frozen / 'untracked.txt').read_text() == 'new input\n'
    assert not (frozen / 'local-only.txt').exists()


def test_locate_regression_checks_nonmonotonic_history_and_keeps_frozen_runs(repo, cli, tmp_path):
    target = 'test_api.py::test_api'
    write(repo, 'test_api.py', 'from api import OK\ndef test_api(): assert OK\n')
    commits = []
    for value in (True, False, True, False):
        write(repo, 'api.py', f'OK = {value!r}\n')
        freeze(repo)
        commits.append(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip())
    proof = cli('locate-regression', repo, '--before', commits[0], '--after', commits[-1], '--test', target,
                '--max-commits', 4, '--output', tmp_path / 'search', '--python', sys.executable)
    assert proof['first_regression'] == {'before': commits[0], 'after': commits[1]}
    assert [row['status'] for row in proof['observations']] == ['passed', 'failed', 'passed', 'failed']
    for row in proof['observations']:
        assert cli('check', row['report_dir'])['status'] == 'consistent'


def test_regression_location_with_changed_test_cannot_claim_first_failure(repo, cli, tmp_path):
    target = 'test_api.py::test_api'
    write(repo, 'api.py', 'OK = True\n')
    write(repo, 'test_api.py', 'from api import OK\ndef test_api(): assert OK\n')
    freeze(repo)
    write(repo, 'api.py', 'OK = False\n')
    write(repo, 'test_api.py', 'from api import OK\ndef test_api(): assert OK is True\n')
    freeze(repo)
    proof = cli('locate-regression', repo, '--before', 'HEAD^', '--after', 'HEAD', '--test', target,
                '--output', tmp_path / 'search', '--python', sys.executable, code=2)
    assert proof['first_regression'] is None
    assert proof['observations'][1]['reason'] == 'test_inputs_changed_or_unconfirmed'


def test_public_summary_omits_private_field_names_and_full_evidence(tmp_path, repo, cli, capsys):
    test = 'test_private_place.py::test_private_result'
    write(repo, 'value.py', 'VALUE = True\n')
    write(repo, 'test_private_place.py', 'from value import VALUE\ndef test_private_result(): assert VALUE\n')
    freeze(repo)
    write(repo, 'value.py', 'VALUE = False\n')
    run = review(cli, repo, tmp_path / 'reports', '--test', test, code=1)
    source = bundle(cli, run, tmp_path / 'source.zip')
    args = ['summary', str(source), '--public-summary', '--require-improvement', test]
    assert main(args) == 1
    markdown = tmp_path / 'public.md'
    markdown.write_text(capsys.readouterr().out, encoding='utf-8')
    summary = cli(*args, '--format', 'json', code=1)
    assert summary['kind'] == 'project_review_public_summary'
    assert set(summary) == {'kind', 'schema_version', 'runner', 'verdict', 'counts',
                            'selected_count', 'report_sha256', 'gate'}
    assert 'private' not in markdown.read_text()
    assert 'private' not in json.dumps(summary)
    check = cli('check-summary', source, markdown, '--public-summary', '--require-improvement', test)
    assert check['status'] == 'consistent' and check['gate']['exit_code'] == 1


def test_shards_require_every_item_once_and_preserve_confirmed_failure(repo, cli, tmp_path):
    first, second = 'test_pair.py::test_first', 'test_pair.py::test_second'
    write(repo, 'service.py', 'READY = True\n')
    write(repo, 'test_pair.py', 'from service import READY\ndef test_first(): assert True\n'
          'def test_second(): assert READY\n')
    freeze(repo)
    selection = tmp_path / 'selection.txt'
    selection.write_text(first + '\n' + second + '\n', encoding='utf-8')

    def shard(name, selected, code=0):
        result = review(cli, repo, tmp_path / name, '--test', selected, code=code)
        return bundle(cli, result, tmp_path / (name + '.zip'))

    left, right = shard('first', first), shard('second', second)
    args = ('check-shards', '--selection-file', selection)
    assert cli(*args, left, right)['status'] == 'passed'
    missing = cli(*args, left, code=2)
    assert missing['missing_tests'] == [second]
    duplicate = cli(*args, left, right, left, code=2)
    assert duplicate['duplicate_tests'] == [first]
    write(repo, 'service.py', 'READY = False\n')
    red = shard('red', second, code=1)
    mismatch = cli(*args, left, red, code=2)
    assert mismatch['issues'][0]['reason'] == 'source_changed'
    failed = cli(*args, red, code=1)
    assert failed['missing_tests'] == [first] and failed['status'] == 'failed'


def test_dated_waiver_cannot_hide_changed_failure_with_same_test_and_type(repo, cli, tmp_path):
    target = 'test_ledger.py::test_ledger'
    write(repo, 'ledger.py', 'MESSAGE = "known negative rounding"\n')
    write(repo, 'test_ledger.py', 'from ledger import MESSAGE\ndef test_ledger(): raise AssertionError(MESSAGE)\n')
    freeze(repo)
    known = review(cli, repo, tmp_path / 'known', '--test', target, code=2)
    proof = cli('summary', known['report_dir'], '--gate', 'no-new-failures', '--include-test-ids', '--format', 'json')
    sample = proof['existing_failure_fingerprints'][0]
    assert sample['before'] == sample['after']
    waiver = tmp_path / 'waiver.json'
    waiver.write_text(json.dumps({'kind': 'project_failure_waivers', 'schema_version': 1, 'entries': [
        {'test_id': target, 'failure_sha256': sample['before'], 'expires_on': '2026-12-01'},
    ]}), encoding='utf-8')
    args = ('--gate', 'no-new-failures', '--failure-waivers', waiver, '--format', 'json')
    active = cli('summary', known['report_dir'], *args, '--waiver-date', '2026-11-30')
    assert active['failure_waivers']['entries'][0]['reason'] == 'matched'
    expired = cli('summary', known['report_dir'], *args, '--waiver-date', '2026-12-01', code=2)
    assert expired['failure_waivers']['entries'][0]['reason'] == 'expired'
    write(repo, 'ledger.py', 'MESSAGE = "new missing refund"\n')
    different = review(cli, repo, tmp_path / 'different', '--test', target, code=2)
    assert cli('summary', different['report_dir'], '--gate', 'no-new-failures', '--format', 'json')['gate']['status'] == 'passed'
    checked = cli('summary', different['report_dir'], *args, '--waiver-date', '2026-11-30', code=2)
    assert checked['failure_waivers']['entries'][0]['reason'] == 'fingerprint_changed'


def test_replay_records_time_zone_difference_even_with_same_outcome(repo, cli, tmp_path, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('This fixture changes the POSIX process time-zone setting.')
    target = 'test_timezone.py::test_zone'
    write(repo, 'pyproject.toml', '[tool.reprolab.pytest]\npass_env = ["TZ"]\n')
    write(repo, 'test_timezone.py', 'import time\ndef test_zone(): assert isinstance(time.timezone, int)\n')
    freeze(repo)
    monkeypatch.setenv('TZ', 'UTC0')
    first = review(cli, repo, tmp_path / 'first', '--test', target)
    archive = bundle(cli, first, tmp_path / 'original.zip')
    monkeypatch.setenv('TZ', 'PST8')
    second = cli('replay', archive, '--confirm-run', '--verify-result', '--output', tmp_path / 'replayed')
    assert second['replay_verification']['conditions']['locale_timezone'] is False
    assert first['before']['runtime_context']['utc_offset_seconds'] == 0
    assert second['before']['runtime_context']['utc_offset_seconds'] == -28800


def test_attestation_binds_report_identity_and_preserves_review_decision(repo, cli, tmp_path):
    import shutil

    if shutil.which('openssl') is None:
        pytest.skip('OpenSSL is required for detached signatures.')
    write(repo, 'test_example.py', 'def test_example(): assert True\n')
    freeze(repo)
    first = review(cli, repo, tmp_path / 'first', '--test', 'test_example.py::test_example')
    report = bundle(cli, first, tmp_path / 'first.zip')
    second = review(cli, repo, tmp_path / 'second', '--test', 'test_example.py::test_example')
    private, public = tmp_path / 'private.pem', tmp_path / 'public.pem'
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                    '-out', str(private)], check=True, capture_output=True)
    subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True, capture_output=True)
    signed = tmp_path / 'signed.json'
    assert cli('attest', report, '--key', private, '--actor', 'maintainer', '--role', 'reviewer',
               '--decision', 'approved', '--reason', 'Reviewed the frozen evidence', '--output', signed)['status'] == 'signed'
    args = ('check-attestation', report, signed, '--trusted-key', public, '--expect-role', 'reviewer')
    assert cli(*args, '--expect-actor', 'maintainer')['status'] == 'verified'
    assert not cli(*args, '--expect-actor', 'someone-else', code=2)['checks']['actor']
    assert not cli('check-attestation', second['report_dir'], signed, '--trusted-key', public,
                   '--expect-role', 'reviewer', '--expect-actor', 'maintainer', code=2)['checks']['report']
    payload = json.loads(signed.read_bytes())
    payload['record']['decision'] = 'rejected'
    signed.write_text(json.dumps(payload), encoding='utf-8')
    assert not cli(*args, '--expect-actor', 'maintainer', code=2)['checks']['signature']


def test_order_reduction_leaves_only_the_required_predecessor(repo, cli, tmp_path):
    target = 'test_ordered.py::test_consumes'
    write(repo, 'version.py', 'BROKEN = False\n')
    write(repo, 'test_ordered.py', 'from version import BROKEN\n'
          'armed = False\n'
          'def test_unrelated(): assert True\n'
          'def test_arms():\n    global armed\n    armed = True\n'
          'def test_consumes():\n    assert not (armed and BROKEN), "armed failure"\n')
    freeze(repo)
    write(repo, 'version.py', 'BROKEN = True\n')
    result = review(cli, repo, tmp_path / 'original', code=1)
    source = bundle(cli, result, tmp_path / 'original.zip')
    output = tmp_path / 'reduced'
    args = ('minimize-order', source, '--test', target, '--output', output, '--python', sys.executable)
    assert cli(*args)['status'] == 'ready'
    assert not output.exists()
    reduced = cli(*args, '--confirm-run')
    assert reduced['status'] == 'one_minimal'
    assert reduced['original_predecessors'] == ['test_ordered.py::test_unrelated', 'test_ordered.py::test_arms']
    assert reduced['predecessors'] == ['test_ordered.py::test_arms']
    assert any(not trial['predecessors'] and trial['status'] == 'passed' for trial in reduced['probes'])
    for trial in reduced['probes']:
        assert cli('check', trial['report_dir'])['status'] == 'consistent'


def test_report_age_uses_reproducible_explicit_evaluation_time(repo, cli, tmp_path):
    from datetime import datetime, timedelta

    write(repo, 'test_age.py', 'def test_age(): assert True\n')
    freeze(repo)
    report = review(cli, repo, tmp_path / 'reports')
    created = datetime.fromisoformat(report['created_at'])
    args = ('summary', report['report_dir'], '--format', 'json', '--max-report-age-hours', 2)
    current = cli(*args, '--report-as-of', (created + timedelta(hours=1)).isoformat())
    assert current['expected_bindings']['age']['matches']
    expired = cli(*args, '--report-as-of', (created + timedelta(hours=3)).isoformat(), code=2)
    assert not expired['expected_bindings']['age']['matches']
