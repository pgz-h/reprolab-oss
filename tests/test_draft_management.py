"""Draft lifecycle tests use local fixtures, never a real model service."""
import json
from pathlib import Path

import pytest

from test_cli import cli
from test_drafts import prepare, response, import_response
from test_ai_api import api_server, api_answer, config


def saved_but_not_materialized(tmp_path, monkeypatch, provider="external"):
    from reprolab import drafts
    _, _, _, state = prepare(tmp_path)
    folder = Path(state["draft_dir"])
    def unavailable(*args, **kwargs):
        raise ValueError("simulated old parser failure")
    with monkeypatch.context() as patch:
        patch.setattr(drafts, "_stage_materialize", unavailable)
        if provider == "external":
            raw = tmp_path / "response.json"
            raw.write_text(json.dumps(response()))
            with pytest.raises(ValueError, match="simulated old parser"):
                drafts.generate_draft(folder, response_file=raw, model="offline-fixture")
        else:
            with api_server(api_answer()) as (url, _):
                with pytest.raises(ValueError, match="simulated old parser"):
                    drafts.generate_draft(folder, config_file=config(tmp_path, url))
    return folder


@pytest.mark.parametrize("provider", ["external", "openai-compatible"])
def test_recover_saved_response_without_network_and_keep_original_provenance(tmp_path, monkeypatch, provider):
    folder = saved_but_not_materialized(tmp_path, monkeypatch, provider)
    raw = (folder/"response.json").read_bytes()
    status = cli("draft", "status", folder, "--json")
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)["recoverable"] is True
    result = cli("draft", "recover", folder, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    generated = json.loads(result.stdout)
    assert generated["status"] == "draft_generated"
    assert generated["origin"]["provider"] == provider
    assert (folder/"response.json").read_bytes() == raw
    assert (folder/"cases/case.json").exists()
    repeated = cli("draft", "recover", folder, "--json")
    assert repeated.returncode == 2
    assert (folder/"response.json").read_bytes() == raw


@pytest.mark.parametrize("changed", ["response.json", "context.json", "prompt.txt", "draft.json"])
def test_recovery_refuses_mismatched_saved_material_without_creating_case(tmp_path, monkeypatch, changed):
    folder = saved_but_not_materialized(tmp_path, monkeypatch)
    path = folder/changed
    if changed == "draft.json":
        state = json.loads(path.read_text()); state["after_commit"] = "0"*40
        path.write_text(json.dumps(state))
    else:
        path.write_text(path.read_text()+" ")
    result=cli("draft", "recover", folder, "--json")
    assert result.returncode == 2, result.stdout
    assert not (folder/"cases/case.json").exists()
    status=json.loads(cli("draft", "status", folder, "--json").stdout)
    assert status["recoverable"] is False


def test_legacy_response_without_binding_record_is_not_claimed_as_authenticated(tmp_path):
    _, _, _, state=prepare(tmp_path)
    folder=Path(state["draft_dir"])
    (folder/"response.json").write_text(json.dumps(response()))
    (folder/"provider.json").write_text(json.dumps({"provider":"openai-compatible","model":"claimed"}))
    result=cli("draft", "recover", folder, "--json")
    assert result.returncode == 2
    assert "import" in result.stdout
    assert not (folder/"cases/case.json").exists()


def test_list_is_static_and_includes_invalid_entries_without_following_links(tmp_path):
    _, _, _, state=prepare(tmp_path)
    root=Path(state["draft_dir"]).parent
    bad=root/"broken";bad.mkdir();(bad/"draft.json").write_text("invalid")
    (root/"linked").symlink_to(Path(state["draft_dir"]),target_is_directory=True)
    marker=tmp_path/"executed"
    (Path(state["draft_dir"])/"regression.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()")
    result=cli("draft", "list", root, "--json")
    assert result.returncode == 0, result.stdout+result.stderr
    data=json.loads(result.stdout)
    assert data["count"] == 3
    assert sum(item["readable"] for item in data["drafts"]) == 1
    assert not marker.exists()


def test_draft_verify_saves_separate_verdict_and_review_in_report_and_replay(tmp_path):
    from reprolab.case import load_case
    _, _, _, state=prepare(tmp_path)
    data=response();data["checks"]='<script>unsafe</script> calls compute'
    generated=import_response(tmp_path,state,data)
    assert generated.returncode == 0,generated.stdout+generated.stderr
    folder=Path(state["draft_dir"])
    result=cli("draft", "verify", folder, "--output", tmp_path/"reports", "--json")
    assert result.returncode == 0,result.stdout+result.stderr
    verdict=json.loads(result.stdout)
    assert verdict["status"] == "verified"
    assert json.loads((folder/"draft.json").read_text())["status"] == "draft_generated"
    assert verdict["draft_review"]["checks"] == data["checks"]
    report=Path(verdict["report_dir"])
    html=(report/"report.html").read_text()
    assert "AI 草稿检查说明" in html and "&lt;script&gt;" in html
    assert "<script>unsafe</script>" not in html
    assert load_case(report/"evidence/case.json").draft_review == verdict["draft_review"]
    status=json.loads(cli("draft", "status", folder, "--json").stdout)
    assert status["generation_status"] == "draft_generated"
    assert status["verification"]["status"] == "verified"
    assert status["verification"]["stale"] is False
    assert status["verification"]["report"] == str(report/"report.html")


@pytest.mark.parametrize("changed", ["regression.py", "cases/case.json"])
def test_status_marks_old_verification_stale_when_test_or_case_changes(tmp_path, changed):
    _, _, _, state=prepare(tmp_path)
    assert import_response(tmp_path,state).returncode == 0
    folder=Path(state["draft_dir"])
    assert cli("draft", "verify", folder, "--output",tmp_path/"reports", "--json").returncode == 0
    path=folder/changed;path.write_text(path.read_text()+"\n")
    status=json.loads(cli("draft", "status", folder, "--json").stdout)
    assert status["verification"]["stale"] is True
    assert status["verification"]["status"] == "stale"
    assert status["verification"]["last_verdict"] == "verified"


def test_verify_environment_failure_is_recorded_without_a_fake_report(tmp_path):
    _, _, _, state=prepare(tmp_path)
    assert import_response(tmp_path,state).returncode == 0
    folder=Path(state["draft_dir"])
    result=cli("draft", "verify", folder, "--python", "/missing/python", "--output",tmp_path/"reports", "--json")
    assert result.returncode == 2
    status=json.loads(cli("draft", "status", folder, "--json").stdout)
    assert status["verification"]["status"] == "inconclusive"
    assert status["verification"]["report"] is None
    assert status["verification"]["error"]


def test_verify_prepared_draft_does_not_execute_or_invent_a_verdict(tmp_path):
    _, _, _, state=prepare(tmp_path)
    folder=Path(state["draft_dir"])
    result=cli("draft", "verify", folder, "--output",tmp_path/"reports", "--json")
    assert result.returncode == 2
    assert not (folder/"verification.json").exists()
    assert not (tmp_path/"reports").exists()


def test_latest_missing_case_error_is_inconclusive_not_a_stale_success(tmp_path):
    _, _, _, state=prepare(tmp_path)
    assert import_response(tmp_path,state).returncode == 0
    folder=Path(state["draft_dir"]);(folder/"cases/case.json").unlink()
    result=cli("draft", "verify", folder, "--output",tmp_path/"reports", "--json")
    assert result.returncode == 2
    status=json.loads(cli("draft", "status", folder, "--json").stdout)
    assert status["verification"]["status"] == "inconclusive"


def test_malformed_verification_record_does_not_crash_status(tmp_path):
    _, _, _, state=prepare(tmp_path)
    folder=Path(state["draft_dir"])
    (folder/"verification.json").write_text(json.dumps({"schema_version":1,"status":[]}))
    result=cli("draft", "status", folder, "--json")
    assert result.returncode == 0,result.stdout+result.stderr
    assert json.loads(result.stdout)["verification"]["status"] == "invalid"


@pytest.mark.parametrize("review", [[], {"checks":"calls compute","expected_behavior":"42","assumptions":"wrong type"}])
def test_invalid_review_metadata_is_rejected_before_test_execution(tmp_path, review):
    _, _, _, state=prepare(tmp_path)
    assert import_response(tmp_path,state).returncode == 0
    folder=Path(state["draft_dir"]);path=folder/"cases/case.json"
    data=json.loads(path.read_text());data["draft_review"]=review;path.write_text(json.dumps(data))
    result=cli("draft", "verify", folder, "--output",tmp_path/"reports", "--json")
    assert result.returncode == 2
    assert "draft_review" in json.loads(result.stdout)["error"]
    assert not (tmp_path/"reports").exists()


def test_project_test_recipe_does_not_immediately_mark_its_verdict_stale(tmp_path):
    from test_workflow import project_case
    target_root=tmp_path/"target";target_root.mkdir()
    repo,before,after,target=project_case(target_root)
    prepared=cli("draft", "prepare",repo,"--before",before,"--after",after,"--issue","compute returns 42", "--context","sample.py","--drafts",tmp_path/"drafts","--json")
    assert prepared.returncode == 0,prepared.stdout+prepared.stderr
    state=json.loads(prepared.stdout)
    assert import_response(tmp_path,state).returncode == 0
    folder=Path(state["draft_dir"]);path=folder/"cases/case.json"
    case=json.loads(path.read_text());case.pop("test");case["test_target"]=target
    path.write_text(json.dumps(case))
    result=cli("draft","verify",folder,"--output",tmp_path/"reports","--json")
    assert result.returncode == 0,result.stdout+result.stderr
    status=json.loads(cli("draft","status",folder,"--json").stdout)
    assert status["verification"]["status"] == "verified"
    assert status["verification"]["stale"] is False
