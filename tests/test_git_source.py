import json
from pathlib import Path
import subprocess

from test_cli import cli


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        capture_output=True, text=True, check=True
    ).stdout.strip()


def prepare_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "ReproLab test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitattributes").write_text("sample.py export-ignore\n")
    (repo / "sample.py").write_text('def compute():\n    raise ValueError("reported bug")\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "buggy version")
    before = git(repo, "rev-parse", "HEAD")
    (repo / "sample.py").write_text("def compute():\n    return 42\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fix behavior")
    after = git(repo, "rev-parse", "HEAD")
    test = tmp_path / "regression.py"
    test.write_text("from sample import compute\ndef test_regression():\n    assert compute() == 42\n")
    return repo, before, after, test


def invoke(repo, before, after, test, output):
    return cli("verify-git", repo, "--before", before, "--after", after,
               "--test", test, "--failure-type", "ValueError",
               "--failure-message", "reported bug", "--output", output, "--json")


def test_reads_committed_trees_without_checkout_or_touching_dirty_files(tmp_path):
    repo, before, after, test = prepare_repo(tmp_path)
    (repo / "sample.py").write_text("uncommitted user content\n")
    (repo / "untracked.txt").write_text("keep me")
    initial_status = git(repo, "status", "--porcelain")
    result = invoke(repo, before, after, test, tmp_path / "reports")
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads(result.stdout)
    assert data["status"] == "verified"
    assert data["git_revision"]["before_commit"] == before
    assert data["git_revision"]["after_commit"] == after
    assert git(repo, "status", "--porcelain") == initial_status
    assert git(repo, "rev-parse", "HEAD") == after
    assert (repo / "sample.py").read_text() == "uncommitted user content\n"
    saved = Path(data["report_dir"]) / "evidence"
    assert "reported bug" in (saved / "before/sample.py").read_text()
    assert json.loads((saved / "case.json").read_text())["git_revision"] == data["git_revision"]
    html = (Path(data["report_dir"]) / "report.html").read_text()
    assert before in html and after in html


def test_invalid_revision_fails_with_a_readable_error(tmp_path):
    repo, before, after, test = prepare_repo(tmp_path)
    result = invoke(repo, "no-such-commit", after, test, tmp_path / "reports")
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "inconclusive"
    assert "Traceback" not in result.stderr
    assert git(repo, "rev-parse", "HEAD") == after


def test_rejects_symlinks_in_committed_tree(tmp_path):
    repo, before, after, test = prepare_repo(tmp_path)
    (repo / "alias.py").symlink_to("sample.py")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "symbolic link")
    result = invoke(repo, before, "HEAD", test, tmp_path / "reports")
    assert result.returncode == 2
    assert "符号链接" in json.loads(result.stdout)["error"]


def test_report_output_must_be_outside_the_source_repository(tmp_path):
    repo, before, after, test = prepare_repo(tmp_path)
    result = invoke(repo, before, after, test, repo / "reports")
    assert result.returncode == 2
    assert not (repo / "reports").exists()
