import json
from pathlib import Path
import site
import subprocess
import sys
import venv

import pytest

from test_cli import cli
from test_git_source import git, prepare_repo


def project_case(tmp_path):
    repo, _, _, _ = prepare_repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    (repo / "pytest.ini").write_text("[pytest]\nmarkers = project_marker: configured in this project\n")
    (tests / "conftest.py").write_text(
        "import pytest\n@pytest.fixture\ndef expected(pytestconfig):\n"
        "    assert any('project_marker' in m for m in pytestconfig.getini('markers'))\n    return 42\n"
    )
    (repo / "sample.py").write_text('def compute():\n    raise ValueError("reported bug")\n')
    (tests / "test_logic.py").write_text("def test_regression():\n    raise AssertionError('obsolete test')\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "before with project fixtures")
    before = git(repo, "rev-parse", "HEAD")
    (repo / "sample.py").write_text("def compute():\n    return 42\n")
    (tests / "test_logic.py").write_text(
        "from sample import compute\n"
        "def test_regression(expected):\n    assert compute() == expected\n"
        "def test_other():\n    raise RuntimeError('must not run')\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixed version with updated regression")
    return repo, before, git(repo, "rev-parse", "HEAD"), "tests/test_logic.py::test_regression"


def verify_args(repo, before, after, target, tmp_path):
    return ("verify-git", repo, "--before", before, "--after", after,
            "--project-test", target, "--failure-type", "ValueError",
            "--failure-message", "reported bug", "--output", tmp_path / "reports", "--json")


def test_project_test_uses_fixtures_configuration_and_same_after_version_test(tmp_path):
    repo, before, after, target = project_case(tmp_path)
    result = cli(*verify_args(repo, before, after, target, tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "verified"
    assert data["test_target"] == target
    assert data["before"]["collected"] == data["after"]["collected"] == 1
    assert data["before"]["test_id"] == data["after"]["test_id"]
    assert "obsolete test" not in (Path(data["report_dir"]) / "evidence/regression.py").read_text()
    assert git(repo, "rev-parse", "HEAD") == after


def make_environment(tmp_path, with_pytest):
    env = tmp_path / "project-env"
    venv.EnvBuilder(with_pip=False).create(env)
    executable = env / "bin/python"
    purelib = Path(subprocess.check_output(
        [str(executable), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
    ).strip())
    if with_pytest:
        (purelib / "pytest-dependencies.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    return executable, purelib


def test_selected_venv_is_used_without_resolving_away_its_python_symlink(tmp_path):
    repo, before, after, target = project_case(tmp_path)
    executable, purelib = make_environment(tmp_path, True)
    (purelib / "environment_marker.py").write_text("VALUE = 'project environment'\n")
    path = repo / "tests/test_logic.py"
    path.write_text(path.read_text().replace("    assert compute()", "    from environment_marker import VALUE\n    assert VALUE == 'project environment'\n    assert compute()"))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "test needs project dependency")
    result = cli(*verify_args(repo, before, "HEAD", target, tmp_path), "--python", executable)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["runtime"]["executable"] == str(executable)
    assert data["before"]["python_executable"] == str(executable)
    assert data["after"]["python_executable"] == str(executable)


def test_missing_pytest_has_a_clear_environment_error(tmp_path):
    repo, before, after, target = project_case(tmp_path)
    executable, _ = make_environment(tmp_path, False)
    result = cli(*verify_args(repo, before, after, target, tmp_path), "--python", executable)
    assert result.returncode == 2
    assert "pytest" in json.loads(result.stdout)["error"]


def test_init_saves_pinned_versions_then_verify_reuses_recipe(tmp_path):
    repo, before, after, target = project_case(tmp_path)
    recipe = tmp_path / "cases/regression.json"
    created = cli("init", recipe, "--repository", repo, "--before", before, "--after", "HEAD",
                  "--project-test", target, "--failure-type", "ValueError",
                  "--failure-message", "reported bug", "--json")
    assert created.returncode == 0, created.stdout + created.stderr
    data = json.loads(recipe.read_text())
    assert data["source"] == "git"
    assert data["before_ref"] == before and data["after_ref"] == after
    # Advancing HEAD must not silently change this saved case.
    (repo / "sample.py").write_text("def compute():\n    return -1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "later unrelated change")
    result = cli("verify", recipe, "--output", tmp_path / "reports", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    verified = json.loads(result.stdout)
    assert verified["git_revision"]["after_commit"] == after
    assert verified["test_target"] == target
    replay = cli("verify", Path(verified["report_dir"]) / "evidence/case.json",
                 "--output", tmp_path / "replay", "--json")
    assert replay.returncode == 0, replay.stdout + replay.stderr


def test_init_never_overwrites_existing_case(tmp_path):
    repo, before, after, target = project_case(tmp_path)
    recipe = tmp_path / "keep.json"
    recipe.write_text("my existing file")
    result = cli("init", recipe, "--repository", repo, "--before", before, "--after", after,
                 "--project-test", target, "--failure-type", "ValueError",
                 "--failure-message", "reported bug", "--json")
    assert result.returncode == 2
    assert recipe.read_text() == "my existing file"


def test_list_tests_is_static_and_does_not_execute_conftest(tmp_path):
    repo, before, after, target = project_case(tmp_path)
    marker = tmp_path / "should-not-exist"
    config = repo / "tests/conftest.py"
    config.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n" + config.read_text())
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture with import side effect")
    result = cli("list-tests", repo, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert target in json.loads(result.stdout)["tests"]
    assert not marker.exists()


@pytest.mark.parametrize("target", ["../outside.py::test_one", "/tmp/other.py::test_one", ".git/test.py::test_one"])
def test_project_target_cannot_escape_the_source_tree(tmp_path, target):
    repo, before, after, _ = project_case(tmp_path)
    result = cli(*verify_args(repo, before, after, target, tmp_path))
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_guided_init_accepts_numbered_test_selection(tmp_path):
    from test_cli import ROOT
    import os
    repo, before, after, target = project_case(tmp_path)
    recipe = tmp_path / "guided.json"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    completed = subprocess.run(
        [sys.executable, "-m", "reprolab", "init", str(recipe),
         "--repository", str(repo), "--before", before, "--after", after,
         "--interactive", "--json"],
        input="1\n\nValueError\nreported bug\nGuided case\n",
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert json.loads(recipe.read_text())["test_target"] == target
    assert json.loads(completed.stdout)["case"] == str(recipe)
