import json
from pathlib import Path
import pytest
from test_git_source import prepare_repo, git
from reprolab.recipes import run_saved_case


def test_uncommitted_repair_freezes_code_and_new_test_without_changing_git(tmp_path):
    from reprolab.workspace import create_workspace_case
    repo, _, _, _ = prepare_repo(tmp_path)
    (repo / 'sample.py').write_text('def compute(): raise ValueError("local bug")\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'broken baseline')
    baseline = git(repo, 'rev-parse', 'HEAD')
    (repo / 'sample.py').write_text('def compute(): return 42\n')
    (repo / 'tests').mkdir(exist_ok=True)
    (repo / 'tests/test_fix.py').write_text('from sample import compute\ndef test_fix(): assert compute() == 42\n')
    (repo / '.gitignore').write_text('private-cache/\n')
    (repo / 'private-cache').mkdir()
    (repo / 'private-cache/not-for-evidence').write_text('local data')
    (repo / '.env.local').write_text('NOT_A_REAL_KEY=teaching\n')
    status = git(repo, 'status', '--porcelain')
    index = git(repo, 'diff', '--cached')
    case = tmp_path / 'cases/current.json'
    created = create_workspace_case(case, repo, 'HEAD', 'tests/test_fix.py::test_fix', failure_type='ValueError', failure_message='local bug')
    assert created['before_commit'] == baseline
    config = json.loads(case.read_text())
    after = case.parent / config['after']
    assert not (after / 'private-cache').exists() and not (after / '.env.local').exists()
    assert git(repo, 'status', '--porcelain') == status and git(repo, 'diff', '--cached') == index
    (repo / 'sample.py').write_text('def compute(): return -100\n')
    result = run_saved_case(case, tmp_path / 'reports')
    assert result['status'] == 'verified'
    assert result['workspace_origin']['before_commit'] == baseline
    assert result['workspace_origin']['after_sha256'] == result['source_sha256']['after']
    replay = run_saved_case(Path(result['report_dir']) / 'evidence/case.json', tmp_path / 'replay')
    assert replay['status'] == 'verified' and replay['workspace_origin'] == result['workspace_origin']


def test_workspace_case_rejects_source_destination_and_existing_path(tmp_path):
    from reprolab.workspace import create_workspace_case
    repo, _, _, _ = prepare_repo(tmp_path)
    kwargs = dict(failure_type='ValueError', failure_message='bug')
    with pytest.raises(ValueError):
        create_workspace_case(repo / 'case.json', repo, 'HEAD', 'test_new.py::test_one', **kwargs)
    existing = tmp_path / 'existing.json'
    existing.write_text('keep this')
    with pytest.raises(ValueError):
        create_workspace_case(existing, repo, 'HEAD', 'test_new.py::test_one', **kwargs)
    assert existing.read_text() == 'keep this'


def test_saved_workspace_inputs_are_checked_against_their_frozen_identity(tmp_path):
    from reprolab.workspace import create_workspace_case
    repo, _, _, _ = prepare_repo(tmp_path)
    (repo / 'test_one.py').write_text('def test_one(): pass\n')
    case = tmp_path / 'case.json'
    create_workspace_case(case, repo, 'HEAD', 'test_one.py::test_one', failure_type='ValueError', failure_message='bug')
    data = json.loads(case.read_text())
    (case.parent / data['after'] / 'sample.py').write_text('changed frozen input\n')
    with pytest.raises(ValueError, match='快照.*变化'):
        run_saved_case(case, tmp_path / 'reports')


def test_working_tree_can_replace_tracked_file_with_directory(tmp_path):
    from reprolab.workspace import copy_workspace
    repo, _, _, _ = prepare_repo(tmp_path)
    (repo / 'data').write_text('old single file')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'single file layout')
    (repo / 'data').unlink()
    (repo / 'data').mkdir()
    (repo / 'data/new.txt').write_text('new directory layout')
    destination = tmp_path / 'snapshot'
    copy_workspace(repo, destination)
    assert (destination / 'data/new.txt').read_text() == 'new directory layout'
