"""Discovery respects project scope and never silently accepts a partial default."""
import json
from pathlib import Path
import sys

import pytest
from test_git_source import git
from reprolab import project_sessions as sessions
from reprolab.discovery import collect_project_tests
from reprolab.workspace import WORKTREE


def project(tmp_path, config="pytest.ini", content="[pytest]\ntestpaths = tests\n"):
    repo = tmp_path / "repo"
    repo.mkdir(); (repo / "tests").mkdir(); (repo / "examples").mkdir()
    (repo / config).write_text(content)
    (repo / "tests/test_real.py").write_text("import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_number(n): assert n > 0\n")
    (repo / "examples/test_optional.py").write_text("import unavailable_optional_service\ndef test_optional(): pass\n")
    git(repo, "init", "-q"); git(repo, "config", "user.name", "Test"); git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", "."); git(repo, "commit", "-qm", "baseline")
    return repo


def freeze(repo, tmp_path):
    return sessions.prepare_project(repo, "HEAD", WORKTREE, tmp_path / "sessions", python_executable=sys.executable)


@pytest.mark.parametrize("config,content", [
    ("pytest.ini", "[pytest]\ntestpaths = tests\n"),
    ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'),
    ("setup.cfg", "[tool:pytest]\ntestpaths = tests\n"),
])
def test_native_testpaths_in_both_discovery_entries(tmp_path, config, content):
    repo = project(tmp_path, config, content)
    state = freeze(repo, tmp_path)
    collected = sessions.collect_project(state["directory"])
    assert collected["status"] == "collected" and not collected["partial"]
    expected = ["tests/test_real.py::test_number[1]", "tests/test_real.py::test_number[2]"]
    assert collected["tests"] == expected
    single = collect_project_tests(repo, WORKTREE, python_executable=sys.executable)
    assert single["status"] == "collected" and single["tests"] == expected
    report = sessions.run_project(state["directory"], tmp_path / "reports", confirmed=True)
    assert report["status"] == "regression_passed" and report["selected_tests"] == expected
    assert sessions.check_project_report(report["report_dir"])["status"] == "consistent"


def test_partial_discovery_needs_explicit_selection_and_cli_reports_attention(tmp_path, capsys):
    from reprolab.project_cli import main
    repo = project(tmp_path, content="[pytest]\n")
    state = freeze(repo, tmp_path)
    status = main(["collect", state["directory"]])
    collected = json.loads(capsys.readouterr().out)
    assert status == 2 and collected["partial"]
    assert collected["diagnosis"]["missing_modules"] == ["unavailable_optional_service"]
    assert "unavailable_optional_service" in collected["diagnosis"]["summary"]
    with pytest.raises(ValueError, match="发现清单不完整"):
        sessions.run_project(state["directory"], tmp_path / "reports", confirmed=True)
    assert not (tmp_path / "reports").exists()
    report = sessions.run_project(state["directory"], tmp_path / "reports", selected_ids=collected["tests"], confirmed=True)
    assert report["status"] == "regression_passed" and report["collection"]["partial"]
    assert sessions.check_project_report(report["report_dir"])["status"] == "consistent"


@pytest.mark.parametrize("scenario,code", [("empty", "empty"), ("timeout", "timed_out"), ("crash", "worker_error")])
def test_collection_diagnosis_uses_execution_evidence(tmp_path, scenario, code):
    repo = project(tmp_path)
    if scenario == "empty":
        (repo / "tests/test_real.py").write_text("# no tests\n")
    else:
        (repo / "conftest.py").write_text("import time; time.sleep(30)\n" if scenario == "timeout" else "import os; os._exit(0)\n")
    state = freeze(repo, tmp_path)
    collected = sessions.collect_project(state["directory"], timeout=.5 if scenario == "timeout" else 10)
    assert collected["status"] == "collection_failed" and collected["tests"] == []
    assert collected["diagnosis"]["code"] == code
    assert collected["diagnosis"]["summary"] in collected["notice"]


def test_abnormal_collection_exit_cannot_publish_a_usable_list(tmp_path):
    repo = project(tmp_path)
    (repo / "conftest.py").write_text("def pytest_sessionfinish(session): session.exitstatus = 3\n")
    state = freeze(repo, tmp_path)
    collected = sessions.collect_project(state['directory'])
    assert collected['status'] == 'collection_failed' and collected['tests'] == []
    with pytest.raises(ValueError, match='真实测试清单'):
        sessions.run_project(state['directory'], tmp_path / 'reports', confirmed=True)


def test_truncated_discovery_never_becomes_an_implicit_range(tmp_path, capsys):
    from reprolab.project_cli import main
    repo = project(tmp_path)
    (repo / 'tests/test_real.py').write_text("import pytest\n@pytest.mark.parametrize('n', range(10001))\ndef test_number(n): assert n >= 0\n")
    state = freeze(repo, tmp_path)
    assert main(['collect', state['directory']]) == 2
    collected = json.loads(capsys.readouterr().out)
    assert collected['count'] == 10001 and collected['truncated']
    assert len(collected['tests']) == 10000 and collected['diagnosis']['code'] == 'truncated'
    with pytest.raises(ValueError, match='发现清单不完整'):
        sessions.run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    result = sessions.run_project(state['directory'], tmp_path / 'reports', confirmed=True, selected_ids=[collected['tests'][0]])
    assert result['status'] == 'regression_passed' and result['collection']['truncated']


def test_scoped_discovery_survives_implicit_run_and_evidence_round_trip(tmp_path, capsys):
    from reprolab.project_cli import main
    repo = project(tmp_path, content="[pytest]\n")
    state = freeze(repo, tmp_path)
    directory = state['directory']
    assert main(['collect', directory, '--path', 'tests/']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['collection_path'] == 'tests' and not result['partial']
    assert len(result['tests']) == 2

    assert main(['collect', directory, '--path', '../outside']) == 2
    assert '相对' in capsys.readouterr().err
    assert main(['collect', directory, '--path', 'tests/missing.py']) == 2
    assert '找不到' in capsys.readouterr().err
    assert sessions.read_project(directory)['collection_path'] == 'tests'

    assert main(['collect', directory, '--path', r'tests\test_real.py']) == 0
    scoped = json.loads(capsys.readouterr().out)
    assert scoped['collection_path'] == 'tests/test_real.py'
    assert scoped['tests'] == result['tests']
    assert main(['collect', directory]) == 0
    assert json.loads(capsys.readouterr().out)['collection_path'] == scoped['collection_path']

    report = sessions.run_project(directory, tmp_path / 'reports', confirmed=True)
    assert report['status'] == 'regression_passed'
    assert report['selected_tests'] == scoped['tests']
    assert report['collection']['collection_path'] == scoped['collection_path']
    assert '发现范围 tests/test_real.py' in (Path(report['report_dir']) / 'report.html').read_text()
    bundle = tmp_path / 'evidence.zip'
    sessions.pack_project_report(report['report_dir'], bundle)
    imported = sessions.unpack_project_report(bundle, tmp_path / 'imported')['report_dir']
    preview = sessions.replay_project_report(imported, tmp_path / 'replayed')
    assert preview['collection_path'] == scoped['collection_path']
    replay = sessions.replay_project_report(imported, tmp_path / 'replayed', confirmed=True)
    assert replay['collection']['collection_path'] == scoped['collection_path']
    assert replay['selected_tests'] == scoped['tests']
    assert sessions.check_project_report(replay['report_dir'])['status'] == 'consistent'

    raw = Path(imported) / 'report.json'
    data = json.loads(raw.read_text())
    data['collection']['collection_path'] = 'tests'
    raw.write_text(json.dumps(data))
    assert sessions.check_project_report(imported)['status'] == 'inconsistent'

    assert main(['collect', directory, '--default-scope']) == 2
    default = json.loads(capsys.readouterr().out)
    assert default['collection_path'] == '' and default['partial']


def test_saved_discovery_path_is_inherited_by_recheck_and_missing_scope_stops(tmp_path, capsys):
    from reprolab.project_cli import main
    from reprolab.project_refresh import recheck_and_run
    repo = project(tmp_path, content="[pytest]\n")
    assert main(['prepare', str(repo), '--before', 'HEAD', '--working-tree', '--path', 'tests',
                 '--output', str(tmp_path / 'sessions')]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state['collection_path'] == 'tests'
    report = sessions.run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    result = recheck_and_run(report['report_dir'], tmp_path / 'sessions', tmp_path / 'reports')
    assert result['status'] == 'completed'
    assert result['collection']['collection_path'] == result['report']['collection']['collection_path'] == 'tests'

    (repo / 'tests').rename(repo / 'renamed')
    result = recheck_and_run(report['report_dir'], tmp_path / 'sessions', tmp_path / 'reports')
    assert result['status'] == 'needs_review' and result['report'] is None
    assert '找不到' in result['notice']
    assert result['state']['collection_path'] == 'tests'
    assert result['state']['collection'] is None
    assert sessions.collect_project(result['state']['directory'], collection_path='renamed')['count'] == 2
