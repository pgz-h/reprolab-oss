"""Automatic recheck progress is derived only from checked, exact parent reports."""
import json
from pathlib import Path
import shutil
import pytest
from reprolab import project_history as history
from reprolab.runner import RunControl
from test_processing_history import chain
from test_recheck_shortcut import original_run
from reprolab.project_refresh import recheck_and_run


def test_partial_review_keeps_omissions_and_changed_conditions_separate(chain):
    report = chain[1]
    original = (Path(report['report_dir']) / 'report.json').read_bytes()
    progress = history.read_recheck_progress(report['report_dir'])
    assert progress['status'] == 'compared'
    assert progress['comparison']['same_check_resolved'] == 0
    assert '未复查 2 项' in progress['scope_summary']
    assert '需复核 1 项' in progress['summary']
    assert progress['comparison']['before']['run_id'] == chain[0]['run_id']
    assert (Path(report['report_dir']) / 'report.json').read_bytes() == original


def test_missing_or_wrong_source_does_not_replace_execution_result(chain, tmp_path):
    progress = history.read_recheck_progress(chain[1]['report_dir'], source_directory=tmp_path / 'missing')
    assert progress['status'] == 'unavailable' and progress['comparison'] is None
    wrong = history.read_recheck_progress(chain[1]['report_dir'], source_directory=chain[2]['report_dir'])
    assert wrong['status'] == 'unavailable' and '身份不符' in wrong['summary']
    moved = tmp_path / 'moved'; shutil.copytree(chain[0]['report_dir'], moved)
    valid = history.read_recheck_progress(chain[1]['report_dir'], source_directory=moved)
    assert valid['status'] == 'compared'
    (moved / 'after.log').write_text('tampered')
    invalid = history.read_recheck_progress(chain[1]['report_dir'], source_directory=moved)
    assert invalid['status'] == 'unavailable' and '不一致' in invalid['summary']


def test_cancellation_and_unbound_report_never_invent_progress(chain):
    control = RunControl(); control.cancel()
    result = history.read_recheck_progress(chain[1]['report_dir'], control=control)
    assert result['status'] == 'cancelled' and result['comparison'] is None
    assert history.read_recheck_progress(chain[0]['report_dir'])['status'] == 'not_applicable'


def test_cli_recheck_returns_progress_without_persisting_a_new_verdict(tmp_path, capsys):
    from reprolab.project_cli import main
    repo, state, original = original_run(tmp_path)
    (repo / 'calc.py').write_text('def answer():\n    return 42\n')
    code = main(['recheck', original['report_dir'], '--sessions', str(tmp_path / 'sessions'), '--output', str(tmp_path / 'reports')])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result['review_progress']['status'] == 'compared'
    assert result['review_progress']['comparison']['same_check_resolved'] == 1
    saved = json.loads((Path(result['report']['report_dir']) / 'report.json').read_text())
    assert 'review_progress' not in saved and saved['status'] == result['report']['status']


def test_timeout_change_is_not_reported_as_same_check_resolution(tmp_path):
    repo, _, original = original_run(tmp_path)
    (repo / 'calc.py').write_text('def answer():\n    return 42\n')
    result = recheck_and_run(original['report_dir'], tmp_path / 'sessions', tmp_path / 'reports', timeout=30)
    progress = history.read_recheck_progress(result['report']['report_dir'])
    assert progress['comparison']['state_counts']['became_passed'] == 1
    assert progress['comparison']['same_check_resolved'] == 0
    assert '需复核 1 项' in progress['summary']


def test_interrupted_recheck_stays_blocked_and_progress_read_never_executes(tmp_path, monkeypatch):
    from test_project_progress import prepare, IDS
    from reprolab import project_sessions as sessions
    state = prepare(tmp_path)
    first = sessions.run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    (Path(state['repository']) / 'settings.py').write_text('DELAY = -1\n')
    result = recheck_and_run(first['report_dir'], tmp_path / 'sessions', tmp_path / 'reports')
    assert result['report']['status'] == 'attention'
    def forbidden(*args, **kwargs):
        pytest.fail('reading progress must not run project code')
    monkeypatch.setattr(sessions, '_launch', forbidden)
    progress = history.read_recheck_progress(result['report']['report_dir'])
    assert progress['comparison']['same_check_resolved'] == 0
    assert progress['comparison']['execution_counts']['blocked'] == len(IDS)
    assert '本轮受阻 3 项' in progress['scope_summary']
