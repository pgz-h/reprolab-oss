"""Offline verification against complete summaries produced by an earlier CLI."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from reprolab.acceptance_plan import read_acceptance_plan
from reprolab.project_cli import main


FIXTURE = Path(__file__).parent / 'fixtures/review_summary_cb56b04'
GATE = ('--gate', 'no-new-failures')
MODES = [('private', '--include-test-ids'), ('public', '--public-summary')]


@pytest.fixture
def evidence(tmp_path):
    archive = tmp_path / 'evidence.zip'
    archive.write_bytes((FIXTURE / 'evidence.zip').read_bytes())
    return str(archive)


@pytest.fixture
def checked(capsys, evidence):
    def run(markdown, *options, facts=None, code):
        args = ['check-summary', evidence, str(markdown), *options]
        if facts is not None:
            args.extend(['--facts-file', str(facts)])
        assert main(args) == code
        streams = capsys.readouterr()
        return json.loads(streams.out or streams.err)
    return run


@pytest.mark.parametrize(('name', 'mode'), MODES)
def test_historical_pair_matches_and_current_gate_still_blocks(evidence, checked, tmp_path, capsys, name, mode):
    saved = (FIXTURE / f'{name}.md').read_bytes()
    markdown = tmp_path / f'{name}.md'
    markdown.write_bytes(saved.replace(b'\n', b'\r\n'))
    facts = FIXTURE / f'{name}.json'
    result = checked(markdown, *GATE, mode, facts=facts, code=0)
    assert result['status'] == result['markdown_status'] == result['facts_status'] == 'consistent'
    assert result['markdown_format'] == result['facts_format'] == 'cb56b04'
    assert result['historical_gate']['exit_code'] == 0
    assert result['gate']['exit_code'] == 2
    assert result['gate']['reason'] == 'pytest_legacy_assertion_unverified'
    assert result['matched_markdown_sha256'] == hashlib.sha256(saved).hexdigest()
    assert result['expected_markdown_sha256'] != result['matched_markdown_sha256']

    assert main(['summary', evidence, *GATE, mode]) == 2
    current = capsys.readouterr().out
    assert 'CI gate** · Blocked' in current
    if name == 'public':
        before = json.loads((FIXTURE / 'private.json').read_bytes())['revisions']['before']['commit']
        for private in ('test_receipts.py', before, '/fixture/'):
            assert private not in current and private.encode() not in saved
            assert private not in facts.read_text(encoding='utf-8')
            assert private not in json.dumps(result)


@pytest.mark.parametrize(('name', 'mode'), MODES)
def test_mixing_formats_never_verifies_as_a_pair(evidence, checked, tmp_path, capsys, name, mode):
    markdown, facts = (FIXTURE / f'{name}.{ext}' for ext in ('md', 'json'))
    current_markdown = tmp_path / 'current.md'
    current_facts = tmp_path / 'current.json'
    assert main(['summary', evidence, *GATE, mode]) == 2
    current_markdown.write_text(capsys.readouterr().out, encoding='utf-8')
    assert main(['summary', evidence, *GATE, mode, '--format', 'json']) == 2
    current_facts.write_text(capsys.readouterr().out, encoding='utf-8')

    result = checked(current_markdown, *GATE, mode, facts=current_facts, code=0)
    assert result['markdown_format'] == result['facts_format'] == 'current'
    for supplied_markdown, supplied_facts, version in ((markdown, current_facts, 'cb56b04'),
                                                        (current_markdown, facts, 'current')):
        result = checked(supplied_markdown, *GATE, mode, facts=supplied_facts, code=2)
        assert result['status'] == 'different' and result['reason'] == 'summary_format_mismatch'
        assert result['markdown_status'] == result['facts_status'] == 'consistent'
        assert result['markdown_format'] == version and result['facts_format'] != version
        assert result['gate']['exit_code'] == 2


@pytest.mark.parametrize(('name', 'mode'), MODES)
def test_check_summary_verifies_pre_scope_pairs_but_rejects_mixing_scope_formats(evidence, checked, tmp_path, capsys, name, mode):
    from reprolab.project_review_summary import render_review_markdown

    assert main(['summary', evidence, *GATE, mode, '--format', 'json']) == 2
    current = json.loads(capsys.readouterr().out)
    previous = {key: value for key, value in current.items() if key not in ('run_scope', 'pytest_phases')}
    markdown, facts = tmp_path / f'{name}.md', tmp_path / f'{name}.json'
    markdown.write_text(render_review_markdown(previous), encoding='utf-8')
    facts.write_text(json.dumps(previous), encoding='utf-8')
    check = checked(markdown, *GATE, mode, facts=facts, code=0)
    assert check['markdown_format'] == check['facts_format'] == 'pre_scope'
    assert check['gate']['exit_code'] == 2 and 'historical_gate' not in check
    facts.write_text(json.dumps(current), encoding='utf-8')
    mixed = checked(markdown, *GATE, mode, facts=facts, code=2)
    assert mixed['markdown_format'] == 'pre_scope' and mixed['facts_format'] == 'current'
    assert mixed['reason'] == 'summary_format_mismatch'


@pytest.mark.parametrize(('original', 'changed'), [
    ('**Needs attention**', '**Selected scope passed**'),
    ('CI gate** · Passed', 'CI gate** · Failed'),
    ('Scope · 2 selected', 'Scope · 1 selected'),
    ('no-new-failures', 'strict'),
    (json.loads((FIXTURE / 'private.json').read_bytes())['report_sha256'], '0' * 64),
    ('Event SHA-256', 'Unsupported format cb56b04 · Event SHA-256'),
])
def test_entire_historical_markdown_must_match(checked, tmp_path, original, changed):
    saved = (FIXTURE / 'private.md').read_text(encoding='utf-8')
    assert original in saved
    edited = tmp_path / 'edited.md'
    edited.write_text(saved.replace(original, changed, 1), encoding='utf-8')
    result = checked(edited, *GATE, '--include-test-ids', facts=FIXTURE / 'private.json', code=2)
    assert result['status'] == result['markdown_status'] == 'different'
    assert result['facts_status'] == 'consistent' and result['facts_format'] == 'cb56b04'
    assert 'historical_gate' not in result and 'markdown_format' not in result
    assert result['gate']['exit_code'] == 2


def test_historical_check_keeps_options_and_all_json_values_bound(checked, tmp_path):
    markdown = FIXTURE / 'private.md'
    facts = tmp_path / 'facts.json'
    old = json.loads((FIXTURE / 'private.json').read_bytes())
    assert checked(markdown, '--gate', 'strict', '--include-test-ids', code=2)['status'] == 'different'
    assert checked(markdown, *GATE, '--public-summary', code=2)['status'] == 'different'
    for edit in ({'selected_count': 1}, {'report_sha256': '0' * 64},
                 {'gate': {**old['gate'], 'exit_code': 2}}):
        facts.write_text(json.dumps(old | edit), encoding='utf-8')
        result = checked(markdown, *GATE, '--include-test-ids', facts=facts, code=2)
        assert result['facts_status'] == 'different' and result['markdown_status'] == 'consistent'
    facts.write_text('{"selected_count": 1, ' + json.dumps(old)[1:], encoding='utf-8')
    assert checked(markdown, *GATE, '--include-test-ids', facts=facts, code=2)['status'] == 'error'


def test_historical_approval_is_rechecked_and_current_gate_is_kept(evidence, checked, tmp_path, capsys):
    openssl = shutil.which('openssl')
    if openssl is None:
        pytest.skip('OpenSSL is required for detached signatures.')
    private, public, signature = (tmp_path / name for name in ('private.pem', 'public.pem', 'signed.json'))
    subprocess.run([openssl, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                    '-out', str(private)], check=True, capture_output=True)
    subprocess.run([openssl, 'pkey', '-in', str(private), '-pubout', '-out', str(public)],
                   check=True, capture_output=True)
    plan = str(FIXTURE / 'approval-plan.json')
    actor = 'Fixture maintainer'
    fingerprint = read_acceptance_plan(plan).fingerprint()
    args = ('--acceptance-plan', plan, '--require-approval', str(signature), '--approval-key', str(public))
    assert main(['attest', evidence, '--key', str(private), '--actor', actor, '--role', 'reviewer',
                 '--decision', 'approved', '--reason', 'Checked sample evidence', '--plan', plan,
                 '--output', str(signature)]) == 0
    capsys.readouterr()
    assert main(['check-attestation', evidence, str(signature), '--trusted-key', str(public),
                 '--expect-actor', actor, '--expect-role', 'reviewer', '--plan', plan]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'

    markdown = FIXTURE / 'approved.md'
    result = checked(markdown, *args, '--expect-reviewer', actor, '--expect-plan-sha256', fingerprint, code=0)
    assert result['markdown_format'] == 'cb56b04' and result['historical_gate']['exit_code'] == 0
    assert result['gate']['exit_code'] == 2 and result['acceptance_plan_sha256'] == fingerprint
    wrong_actor = checked(markdown, *args, '--expect-reviewer', 'Another reviewer',
                          '--expect-plan-sha256', fingerprint, code=2)
    wrong_plan = checked(markdown, *args, '--expect-reviewer', actor, '--expect-plan-sha256', '0' * 64, code=2)
    for result in (wrong_actor, wrong_plan):
        assert result['status'] == 'different' and 'historical_gate' not in result
        assert result['gate']['exit_code'] == 2
