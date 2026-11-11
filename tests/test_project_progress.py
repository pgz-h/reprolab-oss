"""Real worker progress, interruption and portable evidence contracts."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from reprolab import project_sessions as sessions
from reprolab.project_progress import ProgressReader, selection_digest
from reprolab.runner import RunControl
from reprolab.workspace import WORKTREE

IDS = ['test_progress.py::test_fast', 'test_progress.py::test_wait', 'test_progress.py::test_pending']


def make_repository(root, delay=0):
    repo = root / 'repo'
    repo.mkdir()
    (repo / 'settings.py').write_text(f'DELAY = {delay}\n')
    (repo / 'test_progress.py').write_text('import time, os\nfrom settings import DELAY\n'
        'def test_fast():\n    assert True\n'
        'def test_wait():\n    if DELAY < 0: os._exit(3)\n    time.sleep(DELAY)\n'
        'def test_pending():\n    assert True\n')
    def git(*args):
        subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null',
            '-c', 'commit.gpgsign=false', '-c', 'user.name=ReproLab test',
            '-c', 'user.email=test@example.invalid', *args], check=True, capture_output=True)
    git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
    return repo


def prepare(root, delay=0):
    repo = make_repository(root, delay)
    (repo / 'settings.py').write_text('DELAY = 0\n')
    state = sessions.prepare_project(repo, 'HEAD', WORKTREE, root / 'sessions', python_executable=sys.executable)
    sessions.collect_project(state['directory'])
    return state


@pytest.mark.parametrize('interrupt', ['timeout', 'cancel', 'keyboard', 'crash'])
def test_interrupted_run_keeps_observed_phases_without_publishing_pass(tmp_path, interrupt):
    state = prepare(tmp_path, delay=-1 if interrupt == 'crash' else 8)
    updates = []
    interrupted = False
    def progress(phase, payload):
        nonlocal interrupted
        if phase == 'project_progress':
            updates.append(payload)
            if not interrupted and interrupt in ('cancel', 'keyboard') and payload.get('current_test') == IDS[1] and payload.get('current_phase') == 'call':
                interrupted = True
                if interrupt == 'keyboard':
                    raise KeyboardInterrupt
                control.cancel()
    control = RunControl(progress=progress)
    result = sessions.run_project(state['directory'], tmp_path / 'reports', selected_ids=IDS,
        confirmed=True, timeout=1, control=control)
    assert result['status'] == ('cancelled' if interrupt in ('cancel', 'keyboard') else 'attention')
    first = result['before']['tests'][IDS[0]]
    assert first['observed_status'] == 'passed'
    assert [event['phase'] for event in first['phases']] == ['setup', 'call', 'teardown']
    assert first['status'] == 'inconclusive'
    assert result['before']['progress']['completed'] == 1
    assert result['before']['progress']['current_test'] == IDS[1]
    assert result['before']['tests'][IDS[2]]['observed_status'] == 'not_run'
    assert all(entry['status'] == 'inconclusive' for entry in result['entries'])
    assert any(item['completed'] == 1 and item['current_phase'] == 'call' for item in updates)
    assert sessions.check_project_report(result['report_dir'])['status'] == 'consistent'
    bundle = tmp_path / 'evidence.zip'
    sessions.pack_project_report(result['report_dir'], bundle)
    imported = sessions.unpack_project_report(bundle, tmp_path / 'imported')
    assert sessions.check_project_report(imported['report_dir'])['status'] == 'consistent'
    saved = json.loads((Path(imported['report_dir']) / 'report.json').read_text())
    assert saved['before']['tests'][IDS[0]]['observed_status'] == 'passed'
    assert '中断前观察' in (Path(result['report_dir']) / 'report.html').read_text()


def test_complete_run_matches_stream_and_replay_creates_new_evidence(tmp_path, capsys):
    state = prepare(tmp_path)
    from reprolab.project_cli import main
    report = sessions.run_project(state['directory'], tmp_path / 'reports', selected_ids=IDS,
                                  timeout=8, confirmed=True)
    assert report['status'] == 'regression_passed'
    assert all(report[side]['progress']['completed'] == 3 and report[side]['progress']['session_finished'] for side in ('before', 'after'))
    assert sessions.check_project_report(report['report_dir'])['status'] == 'consistent'
    args = ['replay', report['report_dir'], '--output', str(tmp_path / 'replays')]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['source_timeout'] == preview['timeout'] == 8
    assert preview['source_runtime']['executable'] == report['runtime']['executable']
    assert not (tmp_path / 'replays').exists()
    assert main([*args, '--timeout', '15']) == 0
    override = json.loads(capsys.readouterr().out)
    assert override['source_timeout'] == 8 and override['timeout'] == 15
    assert main([*args, '--confirm-run']) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay['timeout'] == 8
    assert replay['run_id'] != report['run_id'] and replay['status'] == 'regression_passed'
    assert sessions.check_project_report(replay['report_dir'])['status'] == 'consistent'
    journal = Path(report['report_dir']) / 'before.progress.jsonl'
    journal.write_text(journal.read_text() + '{}\n')
    assert sessions.check_project_report(report['report_dir'])['status'] == 'inconsistent'


def header():
    return {'sequence': 1, 'event': 'session', 'schema_version': 1, 'mode': 'run',
            'selection_sha256': selection_digest(IDS), 'python_version': '3.14', 'pytest_version': '9.2'}


def test_partial_tail_is_ignored_but_invalid_complete_record_is_rejected(tmp_path):
    path = tmp_path / 'progress.jsonl'
    path.write_text(json.dumps(header()) + '\n' + '{"sequence":')
    reader = ProgressReader(path, IDS)
    reader.poll(final=True)
    assert reader.valid and reader.summary()['tail_ignored'] and not reader.finished
    path.write_text(json.dumps(header()) + '\n' + '{}\n')
    reader = ProgressReader(path, IDS)
    reader.poll(final=True)
    assert not reader.valid and reader.summary()['status'] == 'invalid'


@pytest.mark.parametrize('change', ['selection', 'sequence', 'unexpected_test'])
def test_journal_cannot_change_selected_scope_or_order(tmp_path, change):
    records = [header(), {'sequence': 2, 'event': 'collected', 'count': 3}]
    if change == 'selection':
        records[0]['selection_sha256'] = selection_digest([IDS[1]])
    elif change == 'sequence':
        records[1]['sequence'] = 4
    else:
        records.append({'sequence': 3, 'event': 'test_start', 'test_id': 'other.py::test_extra'})
    path = tmp_path / 'progress.jsonl'
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))
    reader = ProgressReader(path, IDS)
    reader.poll(final=True)
    assert not reader.valid


def test_cli_progress_stays_on_stderr_and_stdout_remains_json(tmp_path, capsys):
    from reprolab.project_cli import main
    state = prepare(tmp_path, delay=.25)
    status = main(['run', state['directory'], '--output', str(tmp_path / 'reports'), '--progress'])
    captured = capsys.readouterr()
    assert status == 0 and json.loads(captured.out)['status'] == 'regression_passed'
    assert '基线版本' in captured.err and '已完成' in captured.err


def test_cli_review_reports_improvement_and_regression_with_evidence(tmp_path, capsys):
    from reprolab.cli import main

    repo = make_repository(tmp_path)
    (repo / 'settings.py').write_text('DELAY = 1\n')
    (repo / 'test_change.py').write_text(
        'from settings import DELAY\n'
        'def test_repaired():\n    assert DELAY == 1\n'
        'def test_regressed():\n    assert DELAY == 0\n'
    )
    (repo / 'test_unrelated.py').write_text('raise RuntimeError("outside discovery scope")\n')
    selected = ['test_change.py::test_repaired', 'test_change.py::test_regressed']
    output = tmp_path / 'review'
    assert main(['project', 'review', str(repo), '--before', 'HEAD', '--working-tree',
                 '--python', sys.executable, '--path', 'test_change.py',
                 '--test', selected[0], '--test', selected[1], '--output', str(output), '--progress']) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert '基线版本' in captured.err and '已完成' in captured.err
    assert report['status'] == 'regression_detected'
    assert report['selected_tests'] == selected
    assert report['collection']['collection_path'] == 'test_change.py'
    assert {entry['test_id']: entry['status'] for entry in report['entries']} == {
        selected[0]: 'improved',
        selected[1]: 'regression_detected',
    }
    assert Path(report['project_directory']).parent == output / 'sessions'
    assert Path(report['report_dir']).parent == output / 'reports'
    assert (Path(report['report_dir']) / 'report.html').is_file()
    assert sessions.check_project_report(report['report_dir'])['status'] == 'consistent'


def test_cli_review_returns_saved_session_on_selection_error(tmp_path, capsys):
    from reprolab.cli import main

    repo = make_repository(tmp_path)
    selection = tmp_path / 'selection.txt'
    selection.write_text('test_progress.py::test_missing\n')
    output = tmp_path / 'review'
    assert main(['project', 'review', str(repo), '--before', 'HEAD', '--after', 'HEAD',
                 '--selection-file', str(selection), '--output', str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    error = json.loads(captured.err)
    assert error['status'] == 'error'
    directory = error['project_directory']
    assert sessions.read_project(directory)['collection']['status'] == 'collected'
    assert not (output / 'reports').exists()
    assert main(['project', 'run', directory, '--test', IDS[0],
                 '--output', str(output / 'reports')]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'regression_passed'
