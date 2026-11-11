"""Offline fixtures test orchestration, never stand in for live AI evidence."""
import json
from pathlib import Path
import sys

import pytest

from test_cli import cli
from test_git_source import git, prepare_repo


def prepare(tmp_path, *extra, before_override=None):
    repo, before, after, _ = prepare_repo(tmp_path)
    result = cli("draft", "prepare", repo, "--before", before_override or before,
                 "--after", "HEAD", "--issue", "compute() should return 42",
                 "--context", "sample.py", "--drafts", tmp_path / "drafts", "--json", *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    return repo, before, after, json.loads(result.stdout)


def response(test=None):
    return {"title": "compute regression", "test_code": test or
            "from sample import compute\ndef test_regression():\n    assert compute() == 42, 'expected 42'\n",
            "checks": "Calls compute and checks the result.", "expected_behavior": "Return 42.",
            "assumptions": ["sample is importable"],
            "expected_failure": {"type": "ValueError", "message_contains": "reported bug"}}


def import_response(tmp_path, prepared, data=None):
    path = tmp_path / "model-response.json"
    path.write_text(json.dumps(data or response()))
    return cli("draft", "import", prepared["draft_dir"], "--response", path,
               "--model", "offline-test-fixture", "--json")


def test_prepare_pins_commits_collects_both_versions_without_executing_or_reading_dirty_code(tmp_path):
    repo, before, after, _ = prepare_repo(tmp_path)
    marker = tmp_path / "executed"
    (repo / "sample.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    dirty = git(repo, "status", "--porcelain")
    issue = tmp_path / "问题.txt"
    issue.write_text("compute 应返回 42", encoding="utf-8")
    result = cli("draft", "prepare", repo, "--before", before, "--issue-file", issue,
                 "--drafts", tmp_path / "drafts", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "prepared"
    folder = Path(data["draft_dir"])
    context = json.loads((folder / "context.json").read_text())
    assert context["before_commit"] == before and context["after_commit"] == after
    assert "reported bug" in context["files"][0]["before"]
    assert "return 42" in context["files"][0]["after"]
    assert not marker.exists()
    assert git(repo, "status", "--porcelain") == dirty
    assert not (folder / "cases/case.json").exists()


def test_import_saves_a_draft_then_existing_verify_and_batch_execute_it(tmp_path):
    repo, before, after, prepared = prepare(tmp_path)
    result = import_response(tmp_path, prepared)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = json.loads(result.stdout)
    assert generated["status"] == "draft_generated"
    case = Path(generated["case"])
    data = json.loads(case.read_text())
    assert data["before_ref"] == before and data["after_ref"] == after
    assert data["python"] == sys.executable
    assert data["draft_origin"]["provider"] == "external"
    assert (Path(prepared["draft_dir"]) / "explanation.md").is_file()
    (repo / "sample.py").write_text("do not use working tree")
    result = cli("verify", case, "--output", tmp_path / "reports", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    verified = json.loads(result.stdout)
    assert verified["status"] == "verified"
    assert verified["before"]["collected"] == verified["after"]["collected"] == 1
    assert (repo / "sample.py").read_text() == "do not use working tree"
    listed = cli("cases", case.parent, "--json")
    assert json.loads(listed.stdout)["cases"][0]["readable"]
    batch = cli("batch", case.parent, "--output", tmp_path / "batch", "--json")
    assert batch.returncode == 0, batch.stdout + batch.stderr
    assert json.loads(batch.stdout)["counts"]["verified"] == 1
    again = import_response(tmp_path, prepared)
    assert again.returncode == 2
    assert json.loads(case.read_text()) == data


@pytest.mark.parametrize("change,expected", [("same", "not_reproduced"), ("bad_import", "inconclusive"), ("wrong_expected", "still_failing")])
def test_draft_is_never_a_verdict_and_run_outcomes_remain_distinct(tmp_path, change, expected):
    repo, before, after, prepared = prepare(tmp_path, before_override="HEAD" if change == "same" else None)
    data = response()
    if change == "bad_import":
        data["test_code"] = "import nonexistent_dependency\nfrom sample import compute\ndef test_regression():\n    assert compute() == 42\n"
    if change == "wrong_expected":
        data["test_code"] = "from sample import compute\ndef test_regression():\n    assert compute() == 99\n"
    result = import_response(tmp_path, prepared, data)
    assert result.returncode == 0, result.stdout + result.stderr
    draft = json.loads(result.stdout)
    assert draft["status"] == "draft_generated"
    result = cli("verify", draft["case"], "--output", tmp_path / "reports", "--json")
    assert json.loads(result.stdout)["status"] == expected


@pytest.mark.parametrize("code", ["def test_one():\n    assert True\n", "def test_one(: broken", "from sample import compute\ndef test_one():\n    assert compute() == 42\ndef test_two():\n    assert compute() == 42\n", "import pytest\nfrom sample import compute\n@pytest.mark.skip\ndef test_one():\n    assert compute() == 42\n"])
def test_invalid_model_tests_never_create_runnable_cases(tmp_path, code):
    _, _, _, prepared = prepare(tmp_path)
    result = import_response(tmp_path, prepared, response(code))
    assert result.returncode == 2, result.stdout
    assert not (Path(prepared["draft_dir"]) / "cases/case.json").exists()


@pytest.mark.parametrize("path", ["../sample.py", ".env", "missing.py"])
def test_context_rejects_invalid_or_secret_paths(tmp_path, path):
    repo, before, _, _ = prepare_repo(tmp_path)
    result = cli("draft", "prepare", repo, "--before", before, "--issue", "bug",
                 "--context", path, "--drafts", tmp_path / "drafts", "--json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_context_size_limit_and_output_inside_repo_are_rejected(tmp_path):
    repo, before, _, _ = prepare_repo(tmp_path)
    (repo / "huge.py").write_text("#" * 40000)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "large source")
    for folder, context in [(tmp_path / "drafts", "huge.py"), (repo / "drafts", "sample.py")]:
        result = cli("draft", "prepare", repo, "--before", before, "--issue", "bug",
                     "--context", context, "--drafts", folder, "--json")
        assert result.returncode == 2
    assert not (repo / "drafts").exists()


def test_unavailable_model_leaves_prepared_context_but_no_fake_test(tmp_path):
    _, _, _, prepared = prepare(tmp_path)
    result = cli("draft", "generate", prepared["draft_dir"], "--config", tmp_path / "missing-config.json", "--json")
    assert result.returncode == 2
    folder = Path(prepared["draft_dir"])
    assert (folder / "prompt.txt").exists()
    assert not (folder / "cases/case.json").exists()
    assert json.loads((folder / "draft.json").read_text())["status"] == "generation_failed"



def test_source_text_only_test_is_not_a_project_behavior_check(tmp_path):
    _, _, _, prepared = prepare(tmp_path)
    fake = response("from pathlib import Path\ndef test_regression():\n    assert 'return 42' in Path('sample.py').read_text(), 'expected 42'\n")
    result = import_response(tmp_path, prepared, fake)
    assert result.returncode == 2
    assert not (Path(prepared["draft_dir"]) / "cases/case.json").exists()


def test_partial_save_failure_leaves_no_case_and_can_be_retried(tmp_path, monkeypatch):
    from reprolab.drafts import generate_draft
    _, _, _, prepared = prepare(tmp_path)
    path = tmp_path / "response.json"
    path.write_text(json.dumps(response()))
    original = Path.write_text
    def fail_explanation(self, *args, **kwargs):
        if self.name == "explanation.md":
            raise OSError("simulated disk error")
        return original(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_explanation)
        with pytest.raises(OSError, match="simulated disk error"):
            generate_draft(prepared["draft_dir"], response_file=path, model="fixture")
    folder = Path(prepared["draft_dir"])
    assert not (folder / "cases/case.json").exists()
    assert not (folder / "regression.py").exists()
    result = generate_draft(folder, response_file=path, model="fixture")
    assert result["status"] == "draft_generated"


def test_generation_provenance_survives_report_and_evidence_replay(tmp_path):
    _, _, _, prepared = prepare(tmp_path)
    generated = json.loads(import_response(tmp_path, prepared).stdout)
    origin = generated["origin"]
    result = cli("verify", generated["case"], "--output", tmp_path / "reports", "--json")
    data = json.loads(result.stdout)
    assert data["draft_origin"] == origin
    saved = Path(data["report_dir"]) / "evidence/case.json"
    assert json.loads(saved.read_text())["draft_origin"] == origin
    assert "AI 测试草稿来源" in (saved.parent.parent / "report.html").read_text()


def test_response_with_extra_prose_is_not_silently_accepted(tmp_path):
    _, _, _, prepared = prepare(tmp_path)
    path=tmp_path/"response.json"
    path.write_text("Here is your test:\n```json\n"+json.dumps(response())+"\n```")
    result=cli("draft", "import", prepared["draft_dir"], "--response", path, "--model", "fixture", "--json")
    assert result.returncode == 2
    assert not (Path(prepared["draft_dir"])/"cases/case.json").exists()
