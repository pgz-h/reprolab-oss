"""Exercise audit follow-through via the project CLI and separately checked evidence."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest

from reprolab.project_cli import main
from test_processing_history import make_chain


def test_series_restores_sources_and_rejects_invalid_parts(tmp_path, capsys):
    first, second, latest = make_chain(tmp_path)
    root = tmp_path / 'segments'
    assert main(['pack-series', latest['report_dir'], '--output', str(root), '--part-mib', '1']) == 0
    packed = json.loads(capsys.readouterr().out)
    assert [row['run_id'] for row in packed['reports']] == [latest['run_id'], second['run_id'], first['run_id']]
    (tmp_path / 'reports').rename(tmp_path / 'originals-moved')
    assert main(['check-series', str(root)]) == 0
    assert json.loads(capsys.readouterr().out)['report_count'] == 3
    destination = tmp_path / 'recipient'
    assert main(['unpack-series', str(root), str(destination)]) == 0
    capsys.readouterr()
    assert main(['history', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    part = root / packed['reports'][1]['parts'][0]['file']
    payload = bytearray(part.read_bytes())
    payload[0] ^= 1
    part.write_bytes(payload)
    unused = tmp_path / 'unused'
    assert main(['unpack-series', str(root), str(unused)]) == 2
    assert '指纹不符' in capsys.readouterr().err
    assert not unused.exists()


def test_series_bypasses_single_bundle_chain_limit(tmp_path, monkeypatch, capsys):
    from reprolab import processing_history

    first, second, latest = make_chain(tmp_path)
    monkeypatch.setattr(processing_history, 'MAX_ROUNDS', 2)
    single = tmp_path / 'too-short.zip'
    assert main(['pack', latest['report_dir'], '--include-sources', '--output', str(single)]) == 2
    assert not single.exists()
    capsys.readouterr()
    sections = tmp_path / 'parts'
    assert main(['pack-series', latest['report_dir'], '--output', str(sections)]) == 0
    actual = json.loads(capsys.readouterr().out)
    assert len(actual['reports']) == 3


@pytest.mark.skipif(
    not (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
         or sys.platform.startswith('linux') and os.environ.get('REPROLAB_TEST_LINUX_NETNS') == '1'),
    reason='requires macOS sandbox or opted-in Linux network namespaces',
)
def test_isolated_replay_records_kernel_denial_and_never_silently_drops_isolation(tmp_path):
    out = tmp_path / 'original'
    repo = tmp_path / 'fixture'
    repo.mkdir()
    py = [sys.executable, '-m', 'reprolab', 'project']
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        (repo / 'test_connection.py').write_text(
            f'import socket\ndef test_connect():\n    with socket.create_connection(("127.0.0.1", {port})): pass\n',
            encoding='utf-8',
        )
        git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid']
        for args in ('init -q', 'add .', 'commit -qm baseline'):
            subprocess.run([*git, *args.split()], check=True, capture_output=True)

        def cli(*args, code=0):
            run = subprocess.run([*py, *map(str, args)], capture_output=True, check=False)
            assert run.returncode == code, run.stderr.decode()
            return json.loads(run.stdout) if run.stdout else json.loads(run.stderr)

        source = cli('review', repo, '--before', 'HEAD', '--after', 'HEAD', '--output', out)
        isolated = cli('replay', source['report_dir'], '--network-isolated', '--confirm-run', '--verify-result',
                       '--output', tmp_path / 'isolated', code=1)
        if sys.platform == 'darwin':
            assert isolated['entries'][0]['after']['exception']['type'] == 'PermissionError'
            assert isolated['network_isolation']['tcp_bind'] == isolated['network_isolation']['tcp_connect'] == 'denied'
        else:
            assert isolated['entries'][0]['after']['exception']['type'] in {'OSError', 'ConnectionRefusedError'}
            assert isolated['network_isolation']['namespace_distinct'] is True
            assert isolated['network_isolation']['interfaces'] == ['lo']
            assert isolated['network_isolation']['ipv4_routes'] == 0
        assert isolated['replay_verification']['conditions']['network_isolation'] is False
        assert cli('check', isolated['report_dir'])['status'] == 'consistent'
        plain = cli('replay', isolated['report_dir'], '--confirm-run', '--output', tmp_path / 'plain', code=2)
        assert '--network-isolated' in plain['error']['message']


def test_init_collect_creates_verified_case_for_a_dynamic_node(tmp_path, capsys):
    from reprolab.cli import main as cli

    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'pytest.ini').write_text('[pytest]\npython_files = check_*.py\n', encoding='utf-8')
    (repo / 'conftest.py').write_text(
        'def pytest_generate_tests(metafunc):\n'
        '    if "number" in metafunc.fixturenames:\n'
        '        metafunc.parametrize("number", [2], ids=["dynamic"])\n', encoding='utf-8')
    (repo / 'check_value.py').write_text(
        'from value import value\ndef test_value(number): assert value() == number\n', encoding='utf-8')
    value = repo / 'value.py'
    value.write_text('def value(): return 1\n', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for args in ('init -q', 'add .', 'commit -qm baseline'):
        subprocess.run([*git, *args.split()], check=True, capture_output=True)
    assert cli(['list-tests', str(repo), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['tests'] == []
    value.write_text('def value(): return 2\n', encoding='utf-8')
    recipe = tmp_path / 'case.json'
    args = ['init', str(recipe), '--repository', str(repo), '--working-tree', '--collect',
            '--failure-type', 'AssertionError', '--failure-message', 'assert 1 == 2', '--json']
    assert cli(args) == 0
    assert 'check_value.py::test_value[dynamic]' in capsys.readouterr().out
    assert cli(['verify', str(recipe), '--output', str(tmp_path / 'runs'), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'


def test_fixed_clock_is_applied_during_collection_and_automatically_replayed(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_clock.py').write_text(
        'from datetime import date, datetime, timezone\nimport time\nimport pytest\n'
        '@pytest.mark.parametrize("day", [date.today().isoformat()])\n'
        'def test_clock(day):\n'
        '    assert day == "2026-01-02"\n'
        '    assert datetime.now(timezone.utc).hour == 12\n'
        '    assert time.gmtime().tm_year == 2026\n'
        '    assert time.time_ns() // 1_000_000_000 == int(time.time())\n'
        '    assert time.monotonic() > 0\n', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for args in ('init -q', 'add .', 'commit -qm baseline'):
        subprocess.run([*git, *args.split()], check=True, capture_output=True)
    initial = ['review', str(repo), '--before', 'HEAD', '--after', 'HEAD', '--output', str(tmp_path / 'first'),
               '--freeze-clock', '2026-01-02T12:00:00+00:00', '--locale', 'C']
    assert main(initial) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['selected_tests'] == ['test_clock.py::test_clock[2026-01-02]']
    assert result['clock_control']['instant'] == '2026-01-02T12:00:00+00:00'
    assert main(['replay', result['report_dir'], '--output', str(tmp_path / 'replay'), '--confirm-run',
                 '--verify-result']) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay['replay_verification']['inputs']['clock_control'] is True


def test_same_exception_with_different_messages_cannot_verify_stability(tmp_path, capsys, monkeypatch):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\npass_env = ["TEST_MESSAGE"]\n', encoding='utf-8')
    (repo / 'test_message.py').write_text(
        'import os\ndef test_message(): raise ValueError(os.environ["TEST_MESSAGE"])\n', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for args in ('init -q', 'add .', 'commit -qm baseline'):
        subprocess.run([*git, *args.split()], check=True, capture_output=True)
    monkeypatch.setenv('TEST_MESSAGE', 'old branch')
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'first')]) == 2
    original = json.loads(capsys.readouterr().out)
    monkeypatch.setenv('TEST_MESSAGE', 'different branch')
    args = ['stability', original['report_dir'], '--confirm-run', '--rounds', '2',
            '--output', str(tmp_path / 'stability')]
    assert main(args) == 1
    run = json.loads(capsys.readouterr().out)
    assert run['status'] == 'different'
    assert run['entries'][0]['status'] == 'consistently_different'
    assert run['rounds'][0]['verification']['entries'][0]['reason'] == 'recorded_failure_changed'


def test_first_attempts_reports_unknown_for_canceled_or_skipped(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_attempt.py').write_text(
        'import pytest\ndef test_fail(): assert False\ndef test_pass(): assert True\n'
        '@pytest.mark.skip(reason="disabled")\ndef test_skip(): pass\n', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for args in ('init -q', 'add .', 'commit -qm baseline'):
        subprocess.run([*git, *args.split()], check=True, capture_output=True)
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'runs')]) == 2
    report = json.loads(capsys.readouterr().out)
    assert main(['first-attempts', report['report_dir']]) == 0
    counts = json.loads(capsys.readouterr().out)['reports'][0]['runs']['after']
    assert counts['observed_failure_rate'] == .5 and counts['observed_denominator'] == 2
    assert counts['counts'] == {'passed': 1, 'failed': 1, 'skipped': 1, 'unknown': 0}


def _commit(repo):
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for args in ('init -q', 'add .', 'commit -qm baseline'):
        subprocess.run([*git, *args.split()], check=True, capture_output=True)


def test_scoped_snapshot_runs_large_worktree_and_records_scope(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_simple.py').write_text('def test_simple(): assert True\n', encoding='utf-8')
    _commit(repo)
    noise = repo / 'unrelated'
    noise.mkdir()
    for number in range(5001):
        (noise / f'{number}.txt').touch()
    options = ['review', str(repo), '--before', 'HEAD', '--working-tree',
               '--output', str(tmp_path / 'scoped')]
    assert main(options) == 2
    assert '5000' in capsys.readouterr().err
    assert main([*options, '--source-path', 'test_simple.py']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['snapshot_scope'] == ['test_simple.py']
    assert result['counts'] == {'regression_passed': 1}
    assert main(['check', result['report_dir']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'consistent'


def test_resource_evidence_reports_exact_errno_without_inferring_oom(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    source = repo / 'target.py'
    source.write_text('def action(): pass\n', encoding='utf-8')
    (repo / 'test_action.py').write_text('from target import action\ndef test_action(): action()\n', encoding='utf-8')
    _commit(repo)
    source.write_text('import errno\ndef action(): raise OSError(errno.ENOSPC, "fixture full")\n', encoding='utf-8')
    assert main(['review', str(repo), '--before', 'HEAD', '--working-tree',
                 '--output', str(tmp_path / 'run')]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['after']['resource_errors'][0]['phase'] == 'call'
    assert result['after']['resource_errors'][0]['errno'] == __import__('errno').ENOSPC
    if sys.platform in ('darwin', 'linux'):
        assert result['after']['worker_resources']['peak_rss_bytes'] > 0
        assert result['after']['worker_resources']['scope'] == 'worker_process_only'
    assert main(['check', result['report_dir']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'consistent'


def test_service_matrix_freezes_provider_and_resets_before_both_versions(tmp_path, capsys):
    provider, consumer = tmp_path / 'provider', tmp_path / 'consumer'
    provider.mkdir()
    consumer.mkdir()
    server = provider / 'server.py'
    template = '''from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import os
requests = 0
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global requests
        requests += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(str(requests * MULTIPLIER).encode())
s = HTTPServer(("127.0.0.1", int(os.environ["REPROLAB_SERVICE_PORT"])), Handler)
Path(os.environ["REPROLAB_READY_FILE"]).touch()
s.serve_forever()
'''
    server.write_text('MULTIPLIER = 1\n' + template, encoding='utf-8')
    _commit(provider)
    server.write_text('MULTIPLIER = 2\n' + template, encoding='utf-8')
    subprocess.run(['git', '-C', str(provider), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qam', 'v2'], check=True, capture_output=True)
    (consumer / 'pyproject.toml').write_text('[tool.reprolab.pytest]\npass_env=["SERVICE_PORT"]\n', encoding='utf-8')
    (consumer / 'test_api.py').write_text(
        'import os, urllib.request\ndef test_first_call():\n'
        '    with urllib.request.urlopen("http://127.0.0.1:"+os.environ["SERVICE_PORT"], timeout=2) as result:\n'
        '        assert result.read() == b"1"\n', encoding='utf-8')
    _commit(consumer)
    plan, output = tmp_path / 'plan.json', tmp_path / 'matrix'
    plan.write_text(json.dumps({'schema_version': 1,
                                'consumer': {'repository': 'consumer', 'before': 'HEAD', 'after': 'HEAD',
                                             'tests': ['test_api.py::test_first_call']},
                                'services': [{'name': 'provider', 'repository': 'provider', 'refs': ['HEAD^', 'HEAD'],
                                              'entry': 'server.py', 'port_env': 'SERVICE_PORT'}]}), encoding='utf-8')
    assert main(['service-matrix', str(plan), '--output', str(output), '--confirm-run']) == 2
    result = json.loads(capsys.readouterr().out)
    assert [cell['gate']['status'] for cell in result['cells']] == ['passed', 'blocked']
    assert main(['check-service-matrix', str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['integrity'] == 'consistent'


def test_sqlite_matrix_replays_frozen_seeds_and_compares_paths(tmp_path, capsys):
    import sqlite3

    database = tmp_path / 'seed.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE records (name TEXT)')
        connection.execute('INSERT INTO records VALUES ("keep")')
    (tmp_path / 'keep.sql').write_text('ALTER TABLE records ADD COLUMN mark INTEGER DEFAULT 1;', encoding='utf-8')
    (tmp_path / 'drop.sql').write_text(
        'DELETE FROM records; ALTER TABLE records ADD COLUMN mark INTEGER DEFAULT 1;', encoding='utf-8')
    (tmp_path / 'empty.sql').write_text('', encoding='utf-8')
    plan, output = tmp_path / 'plan.json', tmp_path / 'matrix'
    plan.write_text(json.dumps({'schema_version': 1, 'probes': {'records': 'SELECT name FROM records ORDER BY name'},
                                'paths': [{'name': 'kept', 'seed': 'seed.db', 'before': ['keep.sql'], 'after': ['keep.sql']},
                                          {'name': 'lost', 'seed': 'seed.db', 'before': ['empty.sql'], 'after': ['drop.sql']}]}),
                    encoding='utf-8')
    assert main(['sqlite-matrix', str(plan), '--output', str(output), '--confirm-run']) == 1
    result = json.loads(capsys.readouterr().out)
    assert [row['verdict'] for row in result['paths']] == ['passed', 'failed']
    assert main(['check-sqlite-matrix', str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['integrity'] == 'consistent'
    database.unlink()
    assert main(['sqlite-matrix', str(output / 'migration-matrix.json'), '--output', str(tmp_path / 'replay'),
                 '--confirm-run']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'


def test_pr_context_uses_real_local_merge_parents_and_refuses_wrong_candidate(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_basic.py').write_text('def test_basic(): pass\n', encoding='utf-8')
    _commit(repo)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                                        '-c', 'user.email=fixture@example.invalid', *args],
                                       stderr=subprocess.PIPE, text=True).strip()
    base = git('rev-parse', 'HEAD')
    target = git('branch', '--show-current')
    git('checkout', '-qb', 'feature')
    (repo / 'feature.txt').write_text('new feature\n', encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'feature')
    head = git('rev-parse', 'HEAD')
    git('checkout', '-q', target)
    (repo / 'target.txt').write_text('target modernized\n', encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'target')
    target_sha = git('rev-parse', 'HEAD')
    git('merge', '--no-ff', '-qm', 'integration', 'feature')
    merged = git('rev-parse', 'HEAD')
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'pull_request': {'base': {'sha': target_sha}, 'head': {'sha': head},
                                                  'merge_commit_sha': merged}}), encoding='utf-8')
    args = ['review', str(repo), '--github-event', str(event), '--pr-mode']
    assert main([*args, 'merge', '--output', str(tmp_path / 'merge')]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [report['before_commit'], report['after_commit']] == [target_sha, merged]
    assert report['pr_context']['mode'] == 'merge'
    assert main([*args, 'head', '--output', str(tmp_path / 'head')]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [report['before_commit'], report['after_commit']] == [base, head]
    assert main([*args, 'merge', '--merge-commit', head, '--output', str(tmp_path / 'wrong')]) == 2
    assert '父提交' in capsys.readouterr().err


def test_private_projection_reduces_standalone_data_and_checks_source_binding(tmp_path, capsys):
    from reprolab import project_projection_format

    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_secret.py').write_text(
        'def test_user_name(): raise ValueError("not-shared-secret")\n', encoding='utf-8')
    _commit(repo)
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'runs')]) == 2
    source = json.loads(capsys.readouterr().out)
    destination = tmp_path / 'public.json'
    assert main(['project-outcomes', source['report_dir'], '--output', str(destination)]) == 0
    capsys.readouterr()
    assert 'secret' not in destination.read_text() and 'user_name' not in destination.read_text()
    standalone = subprocess.run([sys.executable, '-I', project_projection_format.__file__, str(destination)],
                                capture_output=True, check=True)
    audit = json.loads(standalone.stdout)
    assert audit['source_verified'] is False and audit['replayable'] is False
    assert audit['counts'] == {'baseline_failure': 1}
    assert main(['check-outcomes', str(destination), '--source', source['report_dir']]) == 0
    assert json.loads(capsys.readouterr().out)['source_verified'] is True


@pytest.mark.skipif(shutil.which('openssl') is None, reason='OpenSSL not installed')
def test_signed_approval_required_for_exact_plan_and_exact_report(tmp_path, capsys):
    from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_sample.py').write_text('def test_sample(): assert True\n', encoding='utf-8')
    _commit(repo)
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'run')]) == 0
    report = json.loads(capsys.readouterr().out)
    plan = tmp_path / 'plan.json'
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', ()), plan)
    key, public = tmp_path / 'key.pem', tmp_path / 'public.pem'
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(key)],
                   check=True, capture_output=True)
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(public)],
                   check=True, capture_output=True)
    signed = tmp_path / 'signed.json'
    arguments = ['summary', report['report_dir'], '--acceptance-plan', str(plan), '--require-approval',
                 str(signed), '--approval-key', str(public), '--expect-reviewer', 'fixture-reviewer', '--format', 'json']
    assert main(arguments) == 2
    assert json.loads(capsys.readouterr().out)['review_approval']['status'] == 'blocked'
    assert main(['attest', report['report_dir'], '--key', str(key), '--actor', 'fixture-reviewer',
                 '--role', 'reviewer', '--decision', 'approved', '--reason', 'reviewed', '--plan', str(plan),
                 '--output', str(signed)]) == 0
    capsys.readouterr()
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)['review_approval']['status'] == 'passed'


def test_captured_behavior_and_automatic_api_check_are_independently_recomputable(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    module = repo / 'service.py'
    module.write_text('__all__=["fetch"]\ndef fetch(customer): return {"customer": customer, "amount": 5}\n',
                      encoding='utf-8')
    _commit(repo)
    module.write_text('__all__=["fetch"]\ndef fetch(customer, region): return {"customer": customer}\n',
                      encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qam', 'candidate'], check=True, capture_output=True)
    probe = tmp_path / 'probe.py'
    probe.write_text('import json,inspect\nfrom service import fetch\n'
                     'args = ("alice", "eu") if len(inspect.signature(fetch).parameters) == 2 else ("alice",)\n'
                     'print(json.dumps(fetch(*args)))\n', encoding='utf-8')
    args = ['capture-behavior', str(repo), '--before', 'HEAD^', '--after', 'HEAD', '--confirm-run']
    directory = tmp_path / 'json'
    assert main([*args, '--probe', str(probe), '--policy', 'shape', '--output', str(directory)]) == 1
    assert json.loads(capsys.readouterr().out)['findings'] == [{'path': ['amount'], 'reason': 'missing'}]
    assert main(['check-behavior', str(directory)]) == 0
    assert json.loads(capsys.readouterr().out)['integrity'] == 'consistent'
    assert main([*args, '--module', 'service', '--output', str(tmp_path / 'api')]) == 1
    assert json.loads(capsys.readouterr().out)['findings'] == [{'path': ['fetch'], 'reason': 'signature_changed'}]


def test_minimize_captured_json_retains_the_same_observed_failure(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_input.py').write_text(
        'def test_capture(reprolab_input):\n'
        '    payload = reprolab_input("case", lambda: {"needed": 9, "noise": [1, 2]})\n'
        '    if "needed" in payload: raise ValueError("same failure")\n', encoding='utf-8')
    _commit(repo)
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'first')]) == 2
    source = json.loads(capsys.readouterr().out)
    output = tmp_path / 'reduced'
    assert main(['minimize-input', source['report_dir'], '--test', 'test_input.py::test_capture',
                 '--confirm-run', '--output', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['value'] == {'case': {'needed': 0}}
    assert main(['check-input-reduction', str(output), '--source', source['report_dir']]) == 0
    assert json.loads(capsys.readouterr().out)['source_verified'] is True


def test_performance_gate_uses_real_paired_calls_and_noise_floor(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    script = repo / 'target.py'
    script.write_text('import time\ndef operation(): time.sleep(0.002)\n', encoding='utf-8')
    (repo / 'test_cost.py').write_text('from target import operation\ndef test_cost(): operation()\n', encoding='utf-8')
    _commit(repo)
    script.write_text('import time\ndef operation(): time.sleep(0.04)\n', encoding='utf-8')
    assert main(['review', str(repo), '--before', 'HEAD', '--working-tree',
                 '--output', str(tmp_path / 'first')]) == 0
    original = json.loads(capsys.readouterr().out)
    assert main(['stability', original['report_dir'], '--confirm-run', '--rounds', '5',
                 '--output', str(tmp_path / 'rounds')]) == 0
    saved = json.loads(capsys.readouterr().out)
    locations = [row['report_dir'] for row in saved['rounds']]
    assert main(['check-performance', *locations, '--warmup', '0', '--min-delta-ms', '10']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['measurements'][0]['paired_rounds'] == 5
    assert result['measurements'][0]['lower_quartile_delta_ms'] > 10


def test_mutations_distinguish_detected_and_surviving_boolean_changes(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'logic.py').write_text('def important(): return True\ndef ignored(): return True\n', encoding='utf-8')
    (repo / 'test_logic.py').write_text('from logic import important\ndef test_important(): assert important()\n',
                                       encoding='utf-8')
    _commit(repo)
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'source')]) == 0
    report = json.loads(capsys.readouterr().out)
    output = tmp_path / 'mutants'
    assert main(['mutation-check', report['report_dir'], '--source-path', 'logic.py',
                 '--confirm-run', '--output', str(output)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert [row['status'] for row in result['mutants']] == ['detected', 'survived']
    assert main(['check-mutations', str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['integrity'] == 'consistent'
    file = output / 'mutation-check.json'
    manifest = json.loads(file.read_bytes())
    manifest['status'] = 'detected'
    file.write_text(json.dumps(manifest))
    assert main(['check-mutations', str(output)]) == 2
    assert '汇总' in capsys.readouterr().err



def test_existing_issue_contract_requires_a_bound_passing_plan(tmp_path, capsys):
    from reprolab.acceptance_plan import AcceptancePlan, save_acceptance_plan

    repo = tmp_path / 'fixture'
    repo.mkdir()
    test = 'test_ready.py::test_ready'
    (repo / 'test_ready.py').write_text('def test_ready(): assert True\n', encoding='utf-8')
    _commit(repo)
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'source')]) == 0
    report = json.loads(capsys.readouterr().out)
    plan, case, contract = (tmp_path / name for name in ('plan.json', 'case.json', 'contract.json'))
    save_acceptance_plan(AcceptancePlan('pytest', 'strict', (), required_passes=(test,)), plan)
    data = {'schema_version': 1, 'kind': 'issue', 'id': 'LOCAL-5', 'expected': 'check passes', 'tests': [test]}
    case.write_text(json.dumps(data), encoding='utf-8')
    args = [str(case), report['report_dir'], str(plan)]
    assert main(['link-case-contract', *args, '--output', str(contract)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'linked'
    assert main(['check-case-contract', *args, '--contract', str(contract)]) == 0
    assert json.loads(capsys.readouterr().out)['integrity'] == 'consistent'
    data['expected'] = 'different issue'
    case.write_text(json.dumps(data), encoding='utf-8')
    assert main(['check-case-contract', *args, '--contract', str(contract)]) == 2


def test_candidate_history_reads_normal_reports_and_distinguishes_children(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_behavior.py').write_text('from data import check\ndef test_state(): assert check\n',
                                          encoding='utf-8')
    state = repo / 'data.py'
    state.write_text('check = True\n', encoding='utf-8')
    _commit(repo)
    baseline = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    state.write_text('check = False\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qam', 'bad candidate'], check=True, capture_output=True)
    args = ['compare-candidates', str(repo), '--before', baseline, '--candidate', 'bad', 'HEAD',
            '--candidate', 'good', 'HEAD^', '--test', 'test_behavior.py::test_state',
            '--confirm-run', '--output', str(tmp_path / 'comparison')]
    assert main(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert [candidate['gate']['status'] for candidate in result['candidates']] == ['failed', 'passed']
    (repo / 'test_behavior.py').unlink()
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qam', 'remove test'], check=True, capture_output=True)
    assert main(['compare-candidates', str(repo), '--before', baseline, '--candidate', 'bad', 'HEAD^',
                 '--candidate', 'unavailable', 'HEAD', '--test', 'test_behavior.py::test_state',
                 '--confirm-run', '--output', str(tmp_path / 'with-missing')]) == 1
    missing = json.loads(capsys.readouterr().out)
    assert missing['issues'][0]['name'] == 'unavailable'
    assert missing['candidates'][0]['gate']['status'] == 'failed'



def test_git_export_never_silently_merges_case_colliding_paths(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_direct.py').write_text('def test_direct(): assert True\n', encoding='utf-8')
    _commit(repo)
    check = tmp_path / 'Case-Probe'
    check.touch()
    case_insensitive = (tmp_path / 'case-probe').exists()

    def git(*args, data=None):
        return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                               '-c', 'user.email=fixture@example.invalid', *args], input=data,
                              capture_output=True, check=True).stdout.strip()

    one = git('hash-object', '-w', '--stdin', data=b'first\n')
    two = git('hash-object', '-w', '--stdin', data=b'second\n')
    original = git('ls-tree', 'HEAD') + b'\n'
    tree = git('mktree', data=original + b'100644 blob ' + one + b'\tAlpha.dat\n100644 blob ' + two + b'\talpha.dat\n')
    candidate = git('commit-tree', tree.decode(), '-p', 'HEAD', '-m', 'two distinct Git paths').decode()
    args = ['prepare', str(repo), '--before', 'HEAD', '--after', candidate,
            '--output', str(tmp_path / 'sessions')]
    if case_insensitive:
        assert main(args) == 2
        assert '文件名冲突' in capsys.readouterr().err
        assert not list((tmp_path / 'sessions').glob('*/project.json'))
    else:
        assert main(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['archive_preflight']['portability_count'] >= 1
        path = Path(result['directory']) / 'inputs/after'
        assert (path / 'Alpha.dat').read_bytes() == b'first\n'
        assert (path / 'alpha.dat').read_bytes() == b'second\n'


def test_hosted_policy_delegates_exact_artifact_and_rejects_wrong_subject(tmp_path, capsys, monkeypatch):
    import hashlib
    from reprolab import project_github_attestation

    repo = tmp_path / 'fixture'
    repo.mkdir()
    (repo / 'test_ready.py').write_text('def test_ready(): pass\n', encoding='utf-8')
    _commit(repo)
    assert main(['review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--output', str(tmp_path / 'runs')]) == 0
    run = json.loads(capsys.readouterr().out)
    archive = tmp_path / 'evidence.zip'
    assert main(['pack', run['report_dir'], '--output', str(archive)]) == 0
    capsys.readouterr()
    root, bundle = tmp_path / 'root.jsonl', tmp_path / 'bundle.jsonl'
    root.write_text('{}\n')
    bundle.write_text('{}\n')
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    observed = expected
    def fake_gh(args, **options):
        assert '--bundle' in args and '--custom-trusted-root' in args
        assert '--signer-workflow' in args and '--source-digest' in args and '--deny-self-hosted-runners' in args
        payload = [{'verificationResult': {'statement': {'predicateType': 'https://slsa.dev/provenance/v1',
                                                          'subject': [{'digest': {'sha256': observed}}]}}}]
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload).encode())
    monkeypatch.setattr(project_github_attestation.shutil, 'which', lambda name: '/fixture/gh')
    monkeypatch.setattr(project_github_attestation.subprocess, 'run', fake_gh)
    args = ['check-github-attestation', str(archive), '--bundle', str(bundle), '--trusted-root', str(root),
            '--repo', 'trusted/repo', '--workflow', 'trusted/repo/.github/workflows/build.yml',
            '--source-digest', run['after_commit']]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['artifact_sha256'] == expected
    observed = '0' * 64
    assert main(args) == 2
    assert '没有覆盖' in capsys.readouterr().err


def test_failed_linux_namespace_start_never_becomes_a_regression(monkeypatch):
    from reprolab import project_network

    monkeypatch.setattr(project_network.sys, 'platform', 'linux')
    monkeypatch.setattr(project_network.shutil, 'which', lambda name: '/tested/unshare')
    old_readlink = project_network.os.readlink
    def readlink(path, *args, **kwargs):
        return 'net:[100]' if path == '/proc/self/ns/net' else old_readlink(path, *args, **kwargs)
    monkeypatch.setattr(project_network.os, 'readlink', readlink)
    def fail(args, **kwargs):
        assert args[1:5] == ['--user', '--map-root-user', '--net', '--']
        assert '--original-netns' in args
        return subprocess.CompletedProcess(args, 1)
    monkeypatch.setattr(project_network.subprocess, 'run', fail)
    assert project_network.run_isolated(['replay', 'fixture.zip', '--confirm-run']) == 2


@pytest.mark.parametrize(('mode', 'guest_exit', 'expected'), [
    ('none', 80, 0), ('none', 81, 1), ('none', 82, 2), ('none', 1, 2), ('nat', None, 2),
])
def test_windows_container_gate_requires_disconnected_target_and_guest_verdict(monkeypatch, mode, guest_exit, expected):
    from reprolab import project_windows_network as backend

    monkeypatch.setattr(backend.sys, 'platform', 'win32')
    monkeypatch.setattr(backend.shutil, 'which', lambda name: 'docker')
    commands = []
    def invoke(command, **kwargs):
        commands.append(command)
        if 'inspect' in command:
            state = {'Id': 'f' * 64, 'Platform': 'windows', 'State': {'Running': True},
                     'HostConfig': {'NetworkMode': mode}, 'NetworkSettings': {'Networks': {mode: {}}}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([state]).encode())
        assert guest_exit is not None and command[command.index('exec') + 1] == 'f' * 64
        return subprocess.CompletedProcess(command, guest_exit)
    monkeypatch.setattr(backend.subprocess, 'run', invoke)
    assert main(['replay', 'test.zip', '--confirm-run', '--network-isolated',
                 '--windows-container', 'probe']) == expected
    assert len(commands) == (1 if mode == 'nat' else 2)
