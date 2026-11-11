"""Retry planning reads saved evidence; execution creates independent batches."""
import hashlib
import json
from pathlib import Path

import pytest

from reprolab import batch, runner
from test_batch import saved_case


def retry_api():
    from reprolab import retry
    return retry


def save_summary(tmp_path, statuses):
    folder = tmp_path / "source"
    folder.mkdir()
    summary = {"kind": "batch", "schema_version": 1, "batch_id": "batch-source",
               "total": len(statuses), "counts": {"verified": 999},
               "entries": [{"file": str(tmp_path / f"case-{index}.json"),
                            "title": f"Case {index}", "source": "snapshot", "test": "regression.py",
                            "readable": True, "error": "", "status": status, "report": None,
                            "explanation": "saved result"} for index, status in enumerate(statuses)]}
    path = folder / "summary.json"
    path.write_text(json.dumps(summary))
    return path


def test_plan_is_read_only_and_selects_original_order_from_entries_not_counts(tmp_path):
    path = save_summary(tmp_path, ["verified", "pending", "still_failing", "cancelled", "not_reproduced", "inconclusive"])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    plan = retry_api().plan_retry(path.parent)
    assert plan["source_summary"] == str(path)
    assert plan["source_batch_id"] == "batch-source"
    assert plan["source_sha256"] == hashlib.sha256(before[path]).hexdigest()
    assert [entry["index"] for entry in plan["entries"]] == [2, 3, 4, 5, 6]
    assert plan["paths"] == [str(tmp_path / f"case-{i}.json") for i in [1, 2, 3, 4, 5]]
    unfinished = retry_api().plan_retry(path, "unfinished")
    assert [entry["index"] for entry in unfinished["entries"]] == [2, 4]
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_retry_after_fix_only_runs_selected_case_and_preserves_original_evidence(tmp_path):
    good = saved_case(tmp_path / "cases", "good")
    broken = saved_case(tmp_path / "cases", "broken", after='raise ValueError("the reported bug")')
    source = batch.run_selected_cases([good, broken], tmp_path / "reports")
    source_folder = Path(source["summary_dir"])
    old_files = {p: p.read_bytes() for p in source_folder.rglob("*") if p.is_file()}
    plan = retry_api().plan_retry(source_folder)
    (broken.parent / "fixtures/broken/after/sample.py").write_text("def compute():\n    return 42\n")
    snapshots = []
    def progress(phase, payload):
        if phase == "batch_start":
            snapshots.append(json.loads((Path(payload["summary_dir"]) / "summary.json").read_text()))
    result = retry_api().run_retry(plan, tmp_path / "reports", control=runner.RunControl(progress=progress))
    assert result["status"] == "verified"
    assert result["summary_dir"] != source["summary_dir"]
    assert [entry["file"] for entry in result["entries"]] == [str(broken)]
    origin = result["retry_origin"]
    assert origin["source_summary"] == plan["source_summary"]
    assert origin["source_batch_id"] == source["batch_id"]
    assert origin["source_sha256"] == plan["source_sha256"]
    assert origin["mode"] == "not_verified"
    assert origin["entries"] == plan["entries"]
    assert snapshots[0]["retry_origin"] == origin
    assert {p: p.read_bytes() for p in source_folder.rglob("*") if p.is_file()} == old_files
    html = (Path(result["summary_dir"]) / "summary.html").read_text()
    assert source["batch_id"] in html
    assert plan["source_sha256"] in html


def test_retry_rejects_source_changes_and_tampered_selection_before_creating_output(tmp_path):
    path = save_summary(tmp_path, ["still_failing", "pending"])
    api = retry_api()
    plan = api.plan_retry(path)
    tampered = {**plan, "paths": [plan["paths"][1]]}
    with pytest.raises(ValueError):
        api.run_retry(tampered, tmp_path / "runs")
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError):
        api.run_retry(plan, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("mutation", ["kind", "schema", "total", "unknown", "duplicate", "relative", "report", "oversize", "json"])
def test_corrupt_or_confusing_source_is_rejected_without_creating_files(tmp_path, mutation):
    path = save_summary(tmp_path, ["pending", "cancelled"])
    data = json.loads(path.read_text())
    if mutation == "kind": data["kind"] = "case"
    elif mutation == "schema": data["schema_version"] = True
    elif mutation == "total": data["total"] = 1
    elif mutation == "unknown": data["entries"][0]["status"] = "success"
    elif mutation == "duplicate": data["entries"][1]["file"] = data["entries"][0]["file"]
    elif mutation == "relative": data["entries"][0]["file"] = "../case.json"
    elif mutation == "report": data["entries"][0]["report"] = "../outside.html"
    elif mutation == "oversize": data["entries"][0]["title"] = "x" * 5000
    path.write_text("{" if mutation == "json" else json.dumps(data))
    with pytest.raises(ValueError):
        retry_api().plan_retry(path)
    assert list(tmp_path.iterdir()) == [path.parent]


def test_empty_plan_and_unknown_mode_do_not_create_a_batch(tmp_path):
    path = save_summary(tmp_path, ["verified"])
    api = retry_api()
    plan = api.plan_retry(path)
    assert plan["paths"] == []
    with pytest.raises(ValueError): api.run_retry(plan, tmp_path / "runs")
    with pytest.raises(ValueError): api.plan_retry(path, "all")
    assert not (tmp_path / "runs").exists()


def test_batch_preflight_attention_skips_execution_and_continues_without_environment_inventory(tmp_path, monkeypatch):
    import sys
    import types
    blocked = saved_case(tmp_path / "cases", "blocked")
    good = saved_case(tmp_path / "cases", "good")
    marker = tmp_path / "blocked-ran"
    (blocked.parent / "fixtures/blocked/regression.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef test_one(): pass\n")
    check = {"id": "source", "status": "error", "message": "Missing source tree", "suggestion": "Restore source"}
    def preflight(path, output_root=None, **kwargs):
        attention = str(path) == str(blocked)
        return {"status": "attention" if attention else "ready", "checks": [check] if attention else [],
                "tests_executed": False, "notice": "Read only", "runtime": {"executable": "python"},
                "environment": {"distributions": {"private-package": "123"}},
                "environment_comparison": {"status": "unknown"}}
    module = types.ModuleType("reprolab.preflight")
    module.check_case = preflight
    monkeypatch.setitem(sys.modules, "reprolab.preflight", module)
    result = batch.run_selected_cases([blocked, good], tmp_path / "reports")
    assert not marker.exists()
    assert [entry["status"] for entry in result["entries"]] == ["inconclusive", "verified"]
    entry = result["entries"][0]
    assert entry["report"] is None
    assert "Missing source tree" in entry["explanation"]
    assert "Restore source" in entry["explanation"]
    assert entry["preflight"]["checks"] == [check]
    assert entry["preflight"]["tests_executed"] is False
    assert "private-package" not in json.dumps(result)
    assert len(list(Path(result["summary_dir"]).glob("runs/*/report.json"))) == 1


def test_retry_provenance_is_escaped_text_without_source_links(tmp_path):
    case = saved_case(tmp_path / "cases", "pending", title='<script>alert("lineage")</script>')
    path = save_summary(tmp_path, ["pending"])
    source = json.loads(path.read_text())
    source["batch_id"] = '<img src=x onerror="lineage">'
    source["entries"][0].update(file=str(case), title='<script>alert("lineage")</script>')
    path.write_text(json.dumps(source))
    result = retry_api().run_retry(retry_api().plan_retry(path), tmp_path / "reports")
    html = (Path(result["summary_dir"]) / "summary.html").read_text()
    assert '<script>alert("lineage")</script>' not in html
    assert '<img src=x onerror="lineage">' not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert f'href="{path}"' not in html


def test_resume_cancelled_batch_runs_only_pending_case_in_new_batch(tmp_path):
    first = saved_case(tmp_path / "cases", "first")
    second = saved_case(tmp_path / "cases", "second")
    control = runner.RunControl()
    def progress(phase, payload):
        if phase == "batch_case_done" and payload["index"] == 1:
            control.cancel()
    control.progress = progress
    source = batch.run_selected_cases([first, second], tmp_path / "reports", control=control)
    assert [entry["status"] for entry in source["entries"]] == ["verified", "pending"]
    folder = Path(source["summary_dir"])
    original = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    plan = retry_api().plan_retry(folder, "unfinished")
    resumed = retry_api().run_retry(plan, tmp_path / "reports")
    assert resumed["status"] == "verified"
    assert [entry["file"] for entry in resumed["entries"]] == [str(second)]
    assert resumed["retry_origin"]["entries"] == [{"index": 2, "file": str(second), "title": "second", "status": "pending"}]
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == original


def test_deeply_nested_corrupt_json_is_a_validation_error(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    path = folder / "summary.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ValueError):
        retry_api().plan_retry(path)


@pytest.mark.parametrize("mutation", ["mode", "status"])
def test_invalid_lineage_is_rejected_before_writing_a_batch(tmp_path, mutation):
    case = saved_case(tmp_path / "cases", "first")
    path = save_summary(tmp_path, ["pending"])
    source = json.loads(path.read_text())
    source["entries"][0]["file"] = str(case)
    path.write_text(json.dumps(source))
    plan = retry_api().plan_retry(path)
    origin = {key: plan[key] for key in ("source_summary", "source_batch_id", "source_sha256", "mode", "entries")}
    if mutation == "mode": origin["mode"] = []
    else: origin["entries"][0]["status"] = []
    with pytest.raises(ValueError):
        batch.run_selected_cases([case], tmp_path / "reports", retry_origin=origin)
    assert not (tmp_path / "reports").exists()
