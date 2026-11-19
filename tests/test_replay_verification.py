import json
from pathlib import Path
import shutil

from reprolab import project_archive, project_sessions
from reprolab.project_cli import main
from reprolab.project_sessions import pack_project_report, prepare_project, run_project
from reprolab.replay_verification import open_replay_evidence, replay_verification_text
from reprolab.runner import RunControl
from reprolab.workspace import WORKTREE
from test_pytest_profile import make_project


def test_zip_replay_distinguishes_matching_failures_changes_and_incomplete_results(tmp_path, capsys, monkeypatch):
    repo = make_project(tmp_path / 'project')
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\npass_env = ["EXPECTED_ANSWER"]\n')
    (repo / 'tests/test_calc.py').write_text(
        'import os\nimport pytest\nfrom calc import answer\n'
        'def test_answer(): assert answer() == int(os.environ["EXPECTED_ANSWER"])\n'
        '@pytest.mark.skip\ndef test_skip(): pass\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    selection = ['tests/test_calc.py::test_answer']
    monkeypatch.setenv('EXPECTED_ANSWER', '0')
    source = run_project(state['directory'], tmp_path / 'reports', selected_ids=selection, confirmed=True)
    incomplete = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert source['status'] == incomplete['status'] == 'regression_detected'
    assert incomplete['counts']['inconclusive'] == 1
    bundle = tmp_path / 'source.zip'
    pack_project_report(source['report_dir'], bundle)
    original_bytes = bundle.read_bytes()
    repo.rename(tmp_path / 'sender-moved')

    # A ZIP import must leave the main operation cancellable.
    control = RunControl()
    with open_replay_evidence(bundle, control=control) as imported:
        assert (imported / 'report.json').is_file()
        assert control.cancel()
    assert not imported.exists()

    def cli(*args, code=0, markdown=False):
        assert main(list(map(str, args))) == code
        stdout, stderr = capsys.readouterr()
        if markdown:
            assert not stderr
            return stdout
        return json.loads(stdout or stderr)

    output = tmp_path / 'replayed'
    receipt_dir = tmp_path / 'receipt'
    cli('replay', bundle, '--verify-result', '--output', output, code=2)
    cli('replay', bundle, '--receipt-dir', receipt_dir, '--output', output, code=2)
    cli('replay', bundle, '--confirm-run', '--receipt-dir', output, '--output', output, code=2)
    cli('replay', source['report_dir'], '--confirm-run', '--receipt-dir',
        Path(source['report_dir']) / 'receipt', '--output', output, code=2)
    assert not output.exists()
    assert not receipt_dir.exists()
    replay = cli('replay', bundle, '--confirm-run', '--receipt-dir', receipt_dir, '--output', output)
    proof = replay['replay_verification']
    assert proof['status'] == 'verified' and replay['status'] == 'regression_detected'
    assert proof['counts'] == {'matched': 1, 'different': 0, 'incomplete': 0}
    assert all(proof['inputs'].values())
    assert proof['conditions']['runtime_variables'] is None
    assert '运行变量' in replay_verification_text(proof)
    assert 'replay_verification' not in json.loads((Path(replay['report_dir']) / 'report.json').read_text())
    assert cli('verify-replay', bundle, replay['report_dir']) == proof
    assert Path(replay['receipt_dir']) == receipt_dir
    assert {path.name for path in receipt_dir.iterdir()} == {'replay-check.json', 'replay-evidence.zip', 'replay-receipt.md'}
    result_bundle = receipt_dir / 'replay-evidence.zip'
    assert cli('verify-replay', bundle, result_bundle) == proof
    assert json.loads((receipt_dir / 'replay-check.json').read_text(encoding='utf-8')) == proof
    receipt = cli('verify-replay', bundle, result_bundle, '--format', 'markdown', markdown=True)
    assert (receipt_dir / 'replay-receipt.md').read_text(encoding='utf-8') == receipt
    assert 'Selected outcomes reproduced' in receipt
    assert '| Matched | 1 |' in receipt
    assert 'Recorded conditions with unknown values · Runtime variables' in receipt
    assert all(proof[key]['report_sha256'] in receipt for key in ('source', 'replay'))
    assert 'New failures detected' in receipt
    assert selection[0] not in receipt
    assert str(tmp_path) not in receipt
    detailed = cli('verify-replay', bundle, replay['report_dir'], '--format', 'markdown',
                   '--include-test-ids', markdown=True)
    assert selection[0] in detailed
    assert 'Source Passed → failed · Replay Passed → failed' in detailed
    assert cli('verify-replay', bundle, replay['report_dir'], '--include-test-ids', code=2)['status'] == 'error'
    assert cli('verify-replay', bundle, source['report_dir'], code=2)['status'] == 'same_run'
    assert cli('verify-replay', bundle, incomplete['report_dir'], code=2)['status'] == 'inputs_changed'
    assert 'Test selection and order' in cli('verify-replay', bundle, incomplete['report_dir'],
                                             '--format', 'markdown', code=2, markdown=True)
    collision_output = tmp_path / 'collision-output'
    cli('replay', bundle, '--confirm-run', '--receipt-dir', receipt_dir, '--output', collision_output, code=2)
    assert not collision_output.exists()
    assert (receipt_dir / 'replay-receipt.md').read_text(encoding='utf-8') == receipt

    monkeypatch.setenv('EXPECTED_ANSWER', '42')
    changed = cli('replay', bundle, '--confirm-run', '--receipt-dir', tmp_path / 'changed-receipt',
                  '--output', output, code=1)
    row, = changed['replay_verification']['entries']
    assert changed['status'] == 'regression_passed'
    assert row == {'test_id': selection[0], 'status': 'different',
                   'source': {'before': 'passed', 'after': 'failed'},
                   'replay': {'before': 'failed', 'after': 'passed'}}
    assert '| Changed | 1 |' in cli('verify-replay', bundle, changed['report_dir'],
                                     '--format', 'markdown', code=1, markdown=True)
    assert '| Changed | 1 |' in (Path(changed['receipt_dir']) / 'replay-receipt.md').read_text(encoding='utf-8')
    checked = cli('check-receipt', bundle, changed['receipt_dir'], code=1)
    assert checked['status'] == 'consistent'
    assert checked['replay_verification'] == changed['replay_verification']

    monkeypatch.setenv('EXPECTED_ANSWER', '0')
    rerun = cli('replay', incomplete['report_dir'], '--confirm-run', '--receipt-dir', tmp_path / 'incomplete-receipt',
                '--output', output, code=2)
    assert rerun['replay_verification']['status'] == 'incomplete'
    assert rerun['replay_verification']['counts'] == {'matched': 1, 'different': 0, 'incomplete': 1}
    assert (Path(rerun['receipt_dir']) / 'replay-evidence.zip').is_file()
    checked = cli('check-receipt', incomplete['report_dir'], rerun['receipt_dir'], code=2)
    assert checked['status'] == 'consistent'
    assert checked['replay_verification'] == rerun['replay_verification']
    assert bundle.read_bytes() == original_bytes

    def fail_pack(report, destination, **kwargs):
        assert kwargs['expected_report']['report_dir'] == report
        destination.write_bytes(b'partial')
        raise OSError('simulated write failure')

    failed_receipt = tmp_path / 'failed-receipt'
    with monkeypatch.context() as patch:
        patch.setattr(project_archive, 'pack_project_report', fail_pack)
        failed = cli('replay', bundle, '--confirm-run', '--receipt-dir', failed_receipt, '--output', output, code=2)
    assert failed['receipt_error'] == {'type': 'OSError', 'message': 'simulated write failure'}
    assert failed['replay_verification']['status'] == 'verified'
    assert cli('check', failed['report_dir'])['status'] == 'consistent'
    assert 'receipt_dir' not in failed and not failed_receipt.exists()
    assert not list(tmp_path.glob('.reprolab-receipt-*'))

    tampered = tmp_path / 'tampered'
    shutil.copytree(replay['report_dir'], tampered)
    (tampered / 'before.log').write_text('changed evidence')
    assert cli('verify-replay', bundle, tampered, code=2)['status'] == 'error'
    assert cli('verify-replay', bundle, tampered, '--format', 'markdown', code=2)['status'] == 'error'


def test_sender_checks_returned_files_without_rerunning_tests(tmp_path, capsys, monkeypatch):
    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    source = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    bundle = tmp_path / 'source.zip'
    pack_project_report(source['report_dir'], bundle)
    repo.rename(tmp_path / 'sender-moved')
    receipt = tmp_path / 'receipt'

    def cli(*args, code=0):
        assert main(list(map(str, args))) == code
        stdout, stderr = capsys.readouterr()
        assert not stderr
        return json.loads(stdout)

    replay = cli('replay', bundle, '--confirm-run', '--receipt-dir', receipt, '--output', tmp_path / 'replayed')
    returned = tmp_path / 'returned'
    receipt.rename(returned)

    def unexpected_execution(*args, **kwargs):
        raise AssertionError('checking a receipt must never start a replay')

    monkeypatch.setattr(project_sessions, 'replay_project_report', unexpected_execution)
    checked = cli('check-receipt', bundle, returned)
    proof = replay['replay_verification']
    assert checked == {'kind': 'project_replay_receipt_check', 'schema_version': 1,
                       'status': 'consistent', 'replay_verification': proof, 'issues': []}
    repacked = tmp_path / 'repacked'
    rebuilt = cli('pack-receipt', bundle, returned / 'replay-evidence.zip', '--output', repacked)
    assert rebuilt['status'] == 'saved'
    assert rebuilt['replay_verification'] == proof
    assert cli('check-receipt', bundle, repacked) == checked
    rejected = tmp_path / 'same-run'
    same = cli('pack-receipt', bundle, bundle, '--output', rejected, code=2)
    assert same['status'] == 'not_saved' and same['replay_verification']['status'] == 'same_run'
    assert not rejected.exists()

    json_path = returned / 'replay-check.json'
    markdown_path = returned / 'replay-receipt.md'
    archive = returned / 'replay-evidence.zip'
    markdown = markdown_path.read_bytes()
    json_path.write_text(json.dumps(proof, sort_keys=True), encoding='utf-8')
    markdown_path.write_bytes(markdown.replace(b'\n', b'\r\n'))
    assert cli('check-receipt', bundle, returned) == checked

    json_path.write_text(json.dumps({**proof, 'inputs': {**proof['inputs'], 'sources': 1}}), encoding='utf-8')
    markdown_path.write_bytes(markdown + b'\nApproved\n')
    edited = cli('check-receipt', bundle, returned, code=2)
    assert edited['status'] == 'inconsistent' and edited['replay_verification'] == proof
    assert [(issue['part'], issue['reason']) for issue in edited['issues']] == [
        ('replay-check.json', 'different'), ('replay-receipt.md', 'different')]

    json_path.write_text('{"status":"different","status":"verified"}', encoding='utf-8')
    markdown_path.unlink()
    malformed = cli('check-receipt', bundle, returned, code=2)
    assert [(issue['part'], issue['reason']) for issue in malformed['issues']] == [
        ('replay-check.json', 'invalid'), ('replay-receipt.md', 'invalid')]

    json_path.write_text(json.dumps(proof), encoding='utf-8')
    markdown_path.write_bytes(markdown)
    wrong_source = cli('check-receipt', archive, returned, code=2)
    assert wrong_source['status'] == 'inconsistent'
    assert wrong_source['replay_verification']['status'] == 'same_run'

    archive.write_bytes(b'invalid archive')
    damaged = cli('check-receipt', bundle, returned, code=2)
    assert damaged['status'] == 'inconsistent' and damaged['replay_verification'] is None
    assert [(issue['part'], issue['reason']) for issue in damaged['issues']] == [('evidence', 'invalid')]


def test_explained_replay_distinguishes_repr_tokens_and_preserves_receipt(tmp_path, capsys, monkeypatch):
    repo = make_project(tmp_path / 'project')
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\npass_env = ["LABEL"]\n')
    (repo / 'tests/test_calc.py').write_text(
        'import os\n'
        'def test_repr(): raise ValueError(f"private text {object()!r}")\n'
        'def test_amount(): raise ValueError("private sum " + os.environ["LABEL"])\n'
    )
    monkeypatch.setenv('LABEL', '0x1000')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    run = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    source = tmp_path / 'source.zip'
    pack_project_report(run['report_dir'], source)
    monkeypatch.setenv('LABEL', '0x1001')
    receipt = tmp_path / 'receipt'
    assert main(['replay', str(source), '--confirm-run', '--output', str(tmp_path / 'again'),
                 '--receipt-dir', str(receipt)]) == 1
    capsys.readouterr()
    args = ['verify-replay', str(source), str(receipt / 'replay-evidence.zip')]
    assert main(args) == 1
    strict = json.loads(capsys.readouterr().out)
    assert strict == json.loads((receipt / 'replay-check.json').read_bytes())
    assert main([*args, '--explain']) == 1
    explained = json.loads(capsys.readouterr().out)
    # Even if the allocator happens to reuse an address, the amount difference always remains.
    assert explained['failure_text_differences']['exception_text_changed'] == 2
    for row in explained['entries']:
        for side, basis in row.pop('failure_text_difference', {}).items():
            assert side in ('before', 'after')
            assert basis == ('repr_address_text_only' if row['test_id'].endswith('test_repr')
                             else 'exception_text_changed')
    explained.pop('failure_text_differences')
    assert explained == strict
    assert main([*args, '--explain', '--format', 'markdown']) == 1
    text = capsys.readouterr().out
    assert 'Recorded exception text differs beyond object addresses' in text
    assert 'private sum' not in text and 'private text' not in text and '0x1001' not in text
    assert main(['check-receipt', str(source), str(receipt)]) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'consistent'


def test_explained_address_pattern_preserves_aliases_and_literals():
    from reprolab.replay_explanation import _classify

    def failure(message):
        return [('call', 'AssertionError', message)]

    original = '<function f.<locals>.g at 0xdead> <function f.<locals>.g at 0xdead>'
    for message, expected in [
        (original.replace('0xdead', '0xbeef'), 'repr_address_text_only'),
        (original.replace('0xdead', '0xbeef', 1), 'exception_text_changed'),
        (original.replace('function f', 'function h'), 'exception_text_changed'),
        (original + ' 0xbeef', 'exception_text_changed'),
    ]:
        assert _classify(failure(original), failure(message)) == expected
    assert _classify(failure('customer at 0x123>'), failure('customer at 0x124>')) == 'exception_text_changed'
    assert _classify(failure('x'), [('setup', 'AssertionError', 'x')]) == 'phase_or_type_changed'
