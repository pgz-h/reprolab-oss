"""The shortcut either runs the original scope or leaves a reviewable session."""
import json
from pathlib import Path
import subprocess
import pytest
from reprolab.project_refresh import recheck_and_run
from reprolab.project_sessions import prepare_project, run_project, check_project_report
from reprolab.workspace import WORKTREE
from test_pytest_profile import make_project


def original_run(tmp_path, *, timeout=120):
    repo = make_project(tmp_path / 'project')
    (repo / 'calc.py').write_text('def answer():\n    return 43\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    original = run_project(state['directory'], tmp_path / 'reports', timeout=timeout, confirmed=True)
    return repo, state, original


@pytest.mark.parametrize('shallow', [False, True])
def test_cli_recheck_runs_current_code_with_original_baseline_and_preserves_source(tmp_path, capsys, shallow):
    from reprolab.project_cli import main
    repo, state, original = original_run(tmp_path, timeout=47)
    saved = (Path(original['report_dir']) / 'report.json').read_bytes()
    if shallow:
        git = ['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
               '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid']
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'candidate'], check=True)
        full = repo.rename(tmp_path / 'full-project')
        subprocess.run(['git', 'clone', '-q', '--depth=1', full.as_uri(), str(repo)], check=True)
        assert subprocess.run([*git, 'cat-file', '-e', state['before_commit']], capture_output=True).returncode != 0
    (repo / 'calc.py').write_text('def answer():\n    return 42\n')
    code = main(['recheck', original['report_dir'], '--sessions', str(tmp_path / 'sessions'),
                 '--output', str(tmp_path / 'reports'), '--scope', 'attention'])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result['status'] == 'completed'
    assert result['report']['entries'][0]['status'] == 'improved'
    assert result['report']['timeout'] == original['timeout']
    assert result['review_progress']['comparison']['same_check_resolved'] == 1
    assert result['state']['before_commit'] == state['before_commit']
    for key in ('source_sha256', 'source_mode_sha256'):
        assert result['state'][key]['before'] == state[key]['before']
    assert result['report']['review_source']['run_id'] == original['run_id']
    assert (Path(original['report_dir']) / 'report.json').read_bytes() == saved
    assert check_project_report(result['report']['report_dir'])['status'] == 'consistent'


@pytest.mark.parametrize('change', ['renamed', 'edited'])
def test_shortcut_stops_at_reviewable_session_when_original_checks_changed(tmp_path, change):
    repo, state, original = original_run(tmp_path)
    path = repo / 'tests/test_calc.py'
    text = path.read_text()
    path.write_text(text.replace('test_answer', 'test_renamed') if change == 'renamed'
                    else text.replace('answer() == expected', 'answer() > 0'))
    result = recheck_and_run(original['report_dir'], tmp_path / 'sessions', tmp_path / 'reports')
    assert result['status'] == 'needs_review' and result['report'] is None
    assert Path(result['state']['directory']).is_dir()
    assert not result['state']['runs']
    assert result['state']['review_source']['run_id'] == original['run_id']
    assert len(list((tmp_path / 'reports').iterdir())) == 1


@pytest.mark.parametrize('name', ['conftest.py', 'test_support.py', 'pytest.ini'])
def test_shortcut_reviews_changed_support_before_running_tests(tmp_path, name):
    repo, _, original = original_run(tmp_path)
    path = repo / name
    if path.exists():
        path.write_text(path.read_text().replace('return 42', 'return 43'))
    else:
        path.write_text('[pytest]\n' if name == 'pytest.ini' else '# A new root fixture file\n')
    result = recheck_and_run(original['report_dir'], tmp_path / 'sessions', tmp_path / 'reports')
    assert result['status'] == 'needs_review' and result['report'] is None
    assert result['changed_support_files'] == [name]
    assert not result['state']['runs']
    assert len(list((tmp_path / 'reports').iterdir())) == 1
