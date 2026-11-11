from pathlib import Path
from test_git_source import prepare_repo, git


def test_actual_collection_lists_parameters_without_starting_fixtures(tmp_path):
    from reprolab.discovery import collect_project_tests
    from reprolab.workspace import WORKTREE
    repo, _, _, _ = prepare_repo(tmp_path)
    imported, executed = tmp_path / 'imported', tmp_path / 'executed'
    (repo / 'conftest.py').write_text(
        f'import pytest\nfrom pathlib import Path\nPath({str(imported)!r}).touch()\n'
        f'@pytest.fixture(autouse=True)\ndef setup(): Path({str(executed)!r}).touch()\n')
    (repo / 'test_params.py').write_text(
        f'import pytest\nfrom pathlib import Path\n@pytest.mark.parametrize("value", [1, 2], ids=["first", "second"])\n'
        f'def test_value(value): Path({str(executed)!r}).touch()\n')
    result = collect_project_tests(repo, WORKTREE, target='test_params.py::test_value')
    assert result['status'] == 'collected'
    assert result['tests'] == ['test_params.py::test_value[first]', 'test_params.py::test_value[second]']
    assert imported.exists() and not executed.exists()
    assert result['tests_executed'] is False
    assert not list(repo.rglob('__pycache__'))


def test_collection_failure_is_explicit_without_partial_success(tmp_path):
    from reprolab.discovery import collect_project_tests
    repo, _, _, _ = prepare_repo(tmp_path)
    (repo / 'test_import.py').write_text('import reprolab_teaching_no_such_module\ndef test_one(): pass\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'collection failure')
    result = collect_project_tests(repo, 'HEAD', target='test_import.py')
    assert result['status'] == 'collection_failed'
    assert result['tests'] == [] and result['collection_errors']
