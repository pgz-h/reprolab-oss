import json
from pathlib import Path
import shutil

from reprolab import replay_stability as stability
from reprolab.project_cli import main
from reprolab.project_sessions import pack_project_report, prepare_project, run_project
from reprolab.runner import RunControl
from reprolab.stability_history import check_saved_stability
from reprolab.workspace import WORKTREE
from test_pytest_profile import make_project


def test_stability_classifies_actual_outcomes_and_keeps_independent_reports(tmp_path, capsys, monkeypatch):
    repo = make_project(tmp_path / 'project')
    mode = tmp_path / 'test-mode'
    mode.write_text('original', encoding='utf-8')
    (repo / 'tests/test_calc.py').write_text(
        'from pathlib import Path\n'
        'import pytest\n'
        'from calc import answer\n'
        f'def mode(): return Path({str(mode)!r}).read_text()\n'
        'def test_answer(): assert answer() == 42\n'
        'def test_shifted(): assert answer() == (42 if mode() == "original" else 0)\n'
        'def test_variable(): assert answer() == (0 if mode() == "second" else 42)\n'
        'def test_incomplete():\n'
        '    if mode() == "partial": pytest.skip("no observation")\n'
        '    assert answer() == 42\n', encoding='utf-8')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    source = run_project(state['directory'], tmp_path / 'original', confirmed=True)
    bundle = tmp_path / 'source.zip'
    pack_project_report(source['report_dir'], bundle)
    original_bytes = bundle.read_bytes()
    repo.rename(tmp_path / 'sender-moved')

    output = tmp_path / 'checks'
    assert main(['stability', str(bundle), '--output', str(output)]) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'
    assert not output.exists()

    real_replay = stability.replay_project_report
    replay_modes = iter(('first', 'second', 'first', 'partial', 'first', 'original', 'original'))

    def replay(*args, **kwargs):
        mode.write_text(next(replay_modes), encoding='utf-8')
        return real_replay(*args, **kwargs)

    monkeypatch.setattr(stability, 'replay_project_report', replay)
    args = ['stability', str(bundle), '--rounds', '2', '--confirm-run', '--output', str(output)]
    assert main([*args, '--progress']) == 1
    stdout, stderr = capsys.readouterr()
    assert '第 1/2 轮' in stderr and '第 2/2 轮' in stderr
    result = json.loads(stdout)
    assert result['status'] == 'different'
    assert result['counts'] == {'matched': 2, 'consistently_different': 1, 'variable': 1, 'incomplete': 0}
    assert result['completed_rounds'] == 2
    assert result['source']['path'] == str(bundle)
    assert len({item['verification']['replay']['run_id'] for item in result['rounds']}) == 2
    assert all((Path(item['report_dir']) / 'report.json').is_file() for item in result['rounds'])
    assert json.loads(Path(result['summary_file']).read_text(encoding='utf-8')) == result

    assert main(args) == 2
    partial = json.loads(capsys.readouterr().out)
    assert partial['status'] == 'incomplete' and partial['counts']['incomplete'] == 1
    assert partial['completed_rounds'] == 2
    row, = [row for row in partial['entries'] if row['status'] == 'incomplete']
    assert row['rounds'][0] is not None and row['rounds'][1] is None

    def progress(phase, payload):
        if phase == 'stability_round' and payload['round'] == 2:
            assert control.cancel()

    control = RunControl(progress=progress)
    cancelled = stability.replay_stability(bundle, output, rounds=3, confirmed=True, control=control)
    assert cancelled['status'] == 'cancelled' and cancelled['completed_rounds'] == 1
    assert all(entry['observed_rounds'] == 1 for entry in cancelled['entries'])
    assert json.loads(Path(cancelled['summary_file']).read_text(encoding='utf-8')) == cancelled
    assert bundle.read_bytes() == original_bytes

    history_args = ['stability-history', str(bundle), '--output-root', str(output)]
    assert main(history_args) == 0
    history = json.loads(capsys.readouterr().out)
    assert history['kind'] == 'project_stability_history' and len(history['checks']) == 3
    assert {check['summary_file'] for check in history['checks']} == {
        result['summary_file'], partial['summary_file'], cancelled['summary_file']}
    assert main(['check-stability', str(bundle), result['summary_file']]) == 1
    checked = json.loads(capsys.readouterr().out)
    assert checked == {**result, 'history_issues': []}
    restored_cancelled = check_saved_stability(bundle, cancelled['summary_file'])
    assert restored_cancelled['status'] == 'cancelled' and restored_cancelled['completed_rounds'] == 1

    assert main(args) == 0
    stable = json.loads(capsys.readouterr().out)

    def gate(summary, code, *options):
        assert main(['summary', str(bundle), '--format', 'json', '--require-stability',
                     str(summary), *options]) == code
        return json.loads(capsys.readouterr().out)

    verified = gate(Path(stable['summary_file']).parent, 0)
    assert verified['stability']['status'] == 'passed'
    assert verified['stability']['round_report_sha256'] == [
        item['verification']['replay']['report_sha256'] for item in stable['rounds']]
    assert str(tmp_path) not in json.dumps(verified)
    assert gate(stable['summary_file'], 2, '--min-stability-rounds', '3')['stability']['status'] == 'blocked'
    assert gate(result['summary_file'], 1)['stability']['counts']['variable'] == 1
    assert gate(partial['summary_file'], 2)['stability']['verification_status'] == 'incomplete'
    assert gate(cancelled['summary_file'], 2)['stability']['verification_status'] == 'cancelled'
    assert main(['summary', str(bundle), '--min-stability-rounds', '2']) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'

    other = Path(result['rounds'][0]['report_dir'])
    assert main(['stability-history', str(other), '--output-root', str(output)]) == 0
    assert not json.loads(capsys.readouterr().out)['checks']
    assert main(['check-stability', str(other), result['summary_file']]) == 2
    assert '指纹不一致' in json.loads(capsys.readouterr().err)['error']['message']

    moved = tmp_path / 'moved-check'
    shutil.move(Path(result['summary_file']).parent, moved)
    moved_file = moved / 'replay-stability.json'
    edited = {**result, 'entries': [], 'counts': {}, 'status': 'verified'}
    edited['rounds'][1]['report_dir'] = str(other)
    moved_file.write_text(json.dumps(edited), encoding='utf-8')
    restored = check_saved_stability(bundle, moved)
    assert restored['status'] == 'different' and restored['entries'] == result['entries']
    assert all(Path(round_['report_dir']).parent == moved / 'reports' for round_ in restored['rounds'])
    assert json.loads(moved_file.read_text(encoding='utf-8')) == edited

    first = Path(restored['rounds'][0]['report_dir'])
    log = first / 'before.log'
    log.write_bytes(log.read_bytes() + b'tampered')
    blocked = check_saved_stability(bundle, moved)
    assert blocked['status'] == 'incomplete' and blocked['completed_rounds'] == 1
    assert [issue['round'] for issue in blocked['history_issues']] == [1]
    assert [round_['round'] for round_ in blocked['rounds']] == [2]
    assert all(entry['rounds'][0] is None and entry['rounds'][1] is not None for entry in blocked['entries'])
    assert gate(moved, 2)['stability']['unverified_rounds'] == [1]
