from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from reprolab.project_sessions import prepare_project, collect_project, run_project
from reprolab.workspace import WORKTREE
import pytest
from reprolab.pytest_profile import read_pytest_profile
from reprolab.project_sessions import check_project_report, pack_project_report, unpack_project_report, replay_project_report


def make_project(root):
    root.mkdir()
    (root / 'tests').mkdir()
    (root / 'calc.py').write_text('def answer():\n    return 0\n')
    (root / 'test_support.py').write_text('import pytest\n@pytest.fixture\ndef expected():\n    return 42\n')
    (root / 'tests/test_calc.py').write_text('from calc import answer\ndef test_answer(expected):\n    assert answer() == expected\n')
    def git(*args):
        return subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c',
            'commit.gpgsign=false', '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid',
            *args], check=True, capture_output=True)
    git('init', '-q')
    git('add', '.')
    git('commit', '-qm', 'baseline')
    (root / 'calc.py').write_text('def answer():\n    return 42\n')
    (root / 'pyproject.toml').write_text('[tool.reprolab.pytest]\nplugins = ["test_support"]\nimport_mode = "importlib"\n')
    return root


def test_explicit_plugin_runs_both_frozen_versions(tmp_path):
    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions', python_executable=sys.executable)
    collection = collect_project(state['directory'])
    assert collection['status'] == 'collected'
    result = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert result['entries'][0]['status'] == 'improved', result['after']['log_excerpt']



def test_profile_is_preserved_in_strict_case_and_export_replay(tmp_path):
    from reprolab.runner import run_case
    from reprolab.evidence import check_report
    from reprolab.evidence_bundle import pack_report, unpack_report
    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    folder = Path(state['directory'])
    case = tmp_path / 'case.json'
    case.write_text(json.dumps({'title': 'plugin regression',
        'before': str(folder / 'inputs/before'), 'after': str(folder / 'inputs/after'),
        'test': str(folder / 'inputs/after/tests/test_calc.py'),
        'test_target': 'tests/test_calc.py::test_answer',
        'expected_failure': {'type': 'AssertionError', 'message_contains': '42'}}))
    result = run_case(case, tmp_path / 'strict')
    assert result['status'] == 'verified', result['before']['log_excerpt']
    assert result['before']['pytest_profile']['plugins'] == ['test_support']
    assert result['before']['pytest_profile'] == result['after']['pytest_profile']
    assert check_report(result['report_dir'])['status'] == 'consistent'
    pack_report(result['report_dir'], tmp_path / 'case.zip')
    imported = unpack_report(tmp_path / 'case.zip', tmp_path / 'imported')
    replay = run_case(Path(imported['directory']) / 'evidence/case.json', tmp_path / 'replay')
    assert replay['status'] == 'verified'


def test_profile_frozen_with_project_evidence_and_does_not_inherit_selection_flags(tmp_path):
    repo = make_project(tmp_path / 'project')
    with (repo / 'pyproject.toml').open('a') as stream:
        stream.write('\n[tool.pytest.ini_options]\naddopts = "-k no_such_test --maxfail=1"\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    # Later edits to the live project cannot change either execution.
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\nplugins=["not_installed"]\n')
    result = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert result['entries'][0]['status'] == 'improved'
    assert result['pytest_profile']['plugins'] == ['test_support']
    assert check_project_report(result['report_dir'])['status'] == 'consistent'
    assert 'test_support' in (Path(result['report_dir']) / 'report.html').read_text()
    pack_project_report(result['report_dir'], tmp_path / 'project.zip')
    imported = unpack_project_report(tmp_path / 'project.zip', tmp_path / 'imported')
    replay = replay_project_report(imported['report_dir'], tmp_path / 'replays', confirmed=True)
    assert replay['entries'][0]['status'] == 'improved'
    report_path = Path(result['report_dir']) / 'report.json'
    raw = json.loads(report_path.read_text())
    raw['pytest_profile']['plugins'] = []
    report_path.write_text(json.dumps(raw))
    assert check_project_report(report_path.parent)['status'] == 'inconsistent'


@pytest.mark.parametrize('configuration', [
    'plugins = "pytest_asyncio.plugin"', 'plugins = ["-k"]',
    'plugins = ["../../plugin"]', 'plugins = ["no:terminal"]',
    'plugins = [42]', 'import_mode = "unknown"', 'args = ["--collect-only"]',
])
def test_invalid_profiles_are_rejected_without_importing_project(tmp_path, configuration):
    (tmp_path / 'pyproject.toml').write_text('[tool.reprolab.pytest]\n' + configuration + '\n')
    (tmp_path / 'conftest.py').write_text('raise RuntimeError("must not execute")')
    with pytest.raises(ValueError):
        read_pytest_profile(tmp_path)


def test_missing_plugin_is_actionable_and_never_a_pass(tmp_path):
    repo = make_project(tmp_path / 'project')
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\nplugins=["reprolab_missing_plugin"]\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    result = collect_project(state['directory'])
    assert result['status'] == 'collection_failed'
    assert 'reprolab_missing_plugin' in result['log_excerpt']
    assert result['pytest_profile']['plugins'] == ['reprolab_missing_plugin']
    with pytest.raises(ValueError, match='真实测试清单'):
        run_project(state['directory'], tmp_path / 'reports', confirmed=True)


def test_importlib_profile_supports_duplicate_test_filenames(tmp_path):
    repo = make_project(tmp_path / 'project')
    for name in ('one', 'two'):
        folder = repo / 'tests' / name
        folder.mkdir()
        (folder / 'test_same.py').write_text('def test_value(expected):\n    assert expected == 42\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    result = collect_project(state['directory'])
    assert result['status'] == 'collected' and len(result['tests']) == 3
    # New tests are overlaid into both snapshots; the plugin is also shared.
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert report['counts'] == {'regression_passed': 2, 'improved': 1}


def test_declared_plugin_cannot_add_unselected_execution(tmp_path):
    repo = make_project(tmp_path / 'project')
    with (repo / 'test_support.py').open('a') as stream:
        stream.write('\ndef pytest_collection_modifyitems(session, items):\n    if not session.config.option.collectonly and items:\n        items.append(items[0])\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert report['status'] == 'attention'
    assert report['after']['tests']['tests/test_calc.py::test_answer']['status'] == 'inconclusive'
    assert check_project_report(report['report_dir'])['status'] == 'consistent'
