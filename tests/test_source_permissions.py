"""File permissions are part of the evidence needed to replay a run."""

import json
from pathlib import Path
import stat
import zipfile

import pytest

from reprolab import runner
from reprolab.evidence import check_report
from reprolab.evidence_bundle import pack_report, unpack_report
from test_runner import make_case


def _script_case(tmp_path):
    case = make_case(
        tmp_path,
        after='return int(subprocess.check_output([str(Path(__file__).with_name("helper"))]))',
    )
    with (tmp_path / "after/sample.py").open("r+") as stream:
        content = stream.read()
        stream.seek(0)
        stream.write("import subprocess\nfrom pathlib import Path\n" + content)
    for label, mode in (("before", 0o644), ("after", 0o700)):
        helper = tmp_path / label / "helper"
        helper.write_text("#!/bin/sh\nprintf 42\n")
        helper.chmod(mode)
    return case


def test_evidence_check_rejects_permission_changes_that_break_replay(tmp_path):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    assert result["status"] == "verified"
    report = Path(result["report_dir"])
    assert check_report(report)["status"] == "consistent"

    (report / "evidence/after/helper").chmod(0o600)

    assert check_report(report)["status"] == "inconsistent"
    with pytest.raises(ValueError, match="不一致"):
        pack_report(report, tmp_path / "changed.zip")
    assert not (tmp_path / "changed.zip").exists()


@pytest.mark.parametrize("change", ["mode", "content"])
def test_direct_replay_refuses_modified_evidence_before_running(tmp_path, change):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    report = Path(result["report_dir"])
    helper = report / "evidence/after/helper"
    if change == "mode":
        helper.chmod(0o600)
    else:
        helper.write_text("#!/bin/sh\nprintf 0\n")
    with pytest.raises(ValueError, match="变化|不一致"):
        runner.run_case(report / "evidence/case.json", tmp_path / "replayed")
    assert not list((tmp_path / "replayed").rglob("report.json"))


def test_evidence_round_trip_preserves_modes_and_reports_permission_only_changes(tmp_path):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    assert result["status"] == "verified"
    changes = result["source_changes"]["files"]
    assert any(item["path"] == "helper" and "权限" in item["note"] for item in changes)
    report = Path(result["report_dir"])
    pack_report(report, tmp_path / "evidence.zip")
    unpack_report(tmp_path / "evidence.zip", tmp_path / "imported")

    imported = tmp_path / "imported"
    assert stat.S_IMODE((imported / "evidence/after/helper").stat().st_mode) == 0o700
    assert check_report(imported)["status"] == "consistent"
    replay = runner.run_case(imported / "evidence/case.json", tmp_path / "replayed")
    assert replay["status"] == "verified"


@pytest.mark.parametrize("changed_tree", ["source", "snapshot"])
def test_permission_changes_during_preparation_stop_before_any_worker(tmp_path, monkeypatch, changed_tree):
    case = make_case(tmp_path)
    original = runner.snapshot
    called = []

    def changing_snapshot(source, destination, entries, *, control=None):
        result = original(source, destination, entries, control=control)
        if destination.name == "before":
            target = source if changed_tree == "source" else destination
            (target / "sample.py").chmod(0o755)
        return result

    monkeypatch.setattr(runner, "snapshot", changing_snapshot)
    monkeypatch.setattr(runner, "run_snapshot", lambda *a, **kw: called.append(True))
    with pytest.raises(ValueError, match="变化"):
        runner.run_case(case, tmp_path / "reports")
    assert not called
    assert not list((tmp_path / "reports").rglob("report.json"))


def test_legacy_report_remains_readable_and_explains_its_permission_limit(tmp_path):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    report = Path(result["report_dir"])
    _make_legacy_report(report)

    checked = check_report(report)
    assert checked["status"] == "consistent"
    assert "权限" in checked["notice"]


def _make_legacy_report(report):
    import hashlib

    replay_path = report / "evidence/case.json"
    replay = json.loads(replay_path.read_text())
    replay.pop("replay_inputs", None)
    replay.pop("replay_contract_version", None)
    replay.pop("source", None)
    replay_path.write_text(json.dumps(replay))
    report_path = report / "report.json"
    legacy = json.loads(report_path.read_text())
    legacy["schema_version"] = 3
    legacy.pop("source_mode_sha256", None)
    legacy.pop("replay_inputs", None)
    legacy["persistence"]["sha256"]["evidence/case.json"] = hashlib.sha256(replay_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(legacy))


def test_new_report_missing_permission_hash_is_inconsistent(tmp_path):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    report = Path(result["report_dir"])
    report_json = report / "report.json"
    damaged = json.loads(report_json.read_text())
    damaged.pop("source_mode_sha256")
    report_json.write_text(json.dumps(damaged))
    (report / "evidence/after/helper").chmod(0o600)
    assert check_report(report)["status"] == "inconsistent"


def test_replay_case_missing_its_input_contract_is_rejected(tmp_path):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    replay = Path(result["report_dir"]) / "evidence/case.json"
    damaged = json.loads(replay.read_text())
    damaged.pop("replay_inputs")
    replay.write_text(json.dumps(damaged))
    with pytest.raises(ValueError, match="回放"):
        runner.run_case(replay, tmp_path / "replayed")


def test_corrupt_recorded_mode_hash_is_reported_as_inconsistent(tmp_path):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    report = Path(result["report_dir"])
    report_json = report / "report.json"
    damaged = json.loads(report_json.read_text())
    damaged["workspace_origin"] = {"source_mode_sha256": None}
    report_json.write_text(json.dumps(damaged))

    assert check_report(report)["status"] == "inconsistent"


def test_saved_working_tree_case_rejects_changed_file_permissions(tmp_path):
    from reprolab.recipes import run_saved_case
    from reprolab.workspace import create_workspace_case
    from test_workflow import project_case

    repo, before, _, target = project_case(tmp_path)
    case = tmp_path / "case.json"
    create_workspace_case(case, repo, before, target,
                          failure_type="ValueError", failure_message="reported bug")
    saved = json.loads(case.read_text())
    (case.parent / saved["after"] / "sample.py").chmod(0o755)
    with pytest.raises(ValueError, match="变化"):
        run_saved_case(case, tmp_path / "reports")
    assert not list((tmp_path / "reports").rglob("report.json"))


def test_probe_and_confirmed_case_reject_changed_file_permissions(tmp_path):
    from reprolab.failure_probe import probe_failure, save_probe_case
    from reprolab.recipes import run_saved_case
    from test_workflow import project_case

    repo, before, after, target = project_case(tmp_path)
    probe = probe_failure(repo, before, after, target, tmp_path / "probes")
    probe_dir = Path(probe["probe_dir"])
    source = probe_dir / "inputs/after/sample.py"
    source.chmod(0o755)
    with pytest.raises(ValueError, match="变化"):
        save_probe_case(probe_dir, tmp_path / "case.json", confirmed=True)

    source.chmod(0o644)
    save_probe_case(probe_dir, tmp_path / "case.json", confirmed=True)
    saved = json.loads((tmp_path / "case.json").read_text())
    (tmp_path / saved["after"] / "sample.py").chmod(0o755)
    with pytest.raises(ValueError, match="变化"):
        run_saved_case(tmp_path / "case.json", tmp_path / "reports")


def test_legacy_probe_and_its_result_explain_missing_permission_records(tmp_path):
    from reprolab.failure_probe import probe_failure, save_probe_case
    from reprolab.recipes import run_saved_case
    from test_workflow import project_case

    repo, before, after, target = project_case(tmp_path)
    probe = probe_failure(repo, before, after, target, tmp_path / "probes")
    probe_file = Path(probe["probe_dir"]) / "probe.json"
    legacy = json.loads(probe_file.read_text())
    legacy.pop("source_mode_sha256")
    probe_file.write_text(json.dumps(legacy))
    saved = save_probe_case(probe_file.parent, tmp_path / "case.json", confirmed=True)
    assert "未记录" in saved["notice"] and "权限" in saved["notice"]
    result = run_saved_case(saved["case"], tmp_path / "reports")
    assert result["status"] == "verified"
    assert "未记录" in result["input_integrity_notice"]
    assert "未记录" in check_report(result["report_dir"])["notice"]


def test_suite_and_individual_case_reject_changed_file_permissions(tmp_path):
    from reprolab.recipes import run_saved_case
    from reprolab.repair_suite import generate_suite, prepare_suite, run_suite
    from test_git_source import prepare_repo
    from test_repair_suite import _plan_response

    repo, before, after, _ = prepare_repo(tmp_path)
    state = prepare_suite(repo, before, after, "修复 compute", tmp_path / "suites")
    suite = Path(state["directory"])
    response = tmp_path / "response.json"
    response.write_text(json.dumps(_plan_response()))
    source = suite / "inputs/after/sample.py"
    source.chmod(0o755)
    with pytest.raises(ValueError, match="变化"):
        generate_suite(suite, response_file=response)

    source.chmod(0o644)
    generate_suite(suite, response_file=response)
    source.chmod(0o755)
    with pytest.raises(ValueError, match="变化"):
        run_suite(suite, tmp_path / "reports", confirmed=True)
    with pytest.raises(ValueError, match="变化"):
        run_saved_case(suite / "cases/repair.json", tmp_path / "reports")


def test_desktop_input_stamp_changes_when_a_file_permission_changes(tmp_path):
    from reprolab.desktop_store import input_stamp

    case = make_case(tmp_path)
    initial = input_stamp(case)
    (tmp_path / "after/sample.py").chmod(0o755)
    assert input_stamp(case) != initial


def test_working_tree_copy_stops_if_permissions_change_during_snapshot(tmp_path, monkeypatch):
    from reprolab import workspace
    from test_git_source import prepare_repo

    repo, _, _, _ = prepare_repo(tmp_path)
    original = workspace.snapshot

    def changing_snapshot(source, destination, entries, *, control=None):
        result = original(source, destination, entries, control=control)
        (source / "sample.py").chmod(0o755)
        return result

    monkeypatch.setattr(workspace, "snapshot", changing_snapshot)
    with pytest.raises(ValueError, match="变化"):
        workspace.copy_workspace(repo, tmp_path / "snapshot")


def test_permissions_changed_before_publication_prevent_a_verdict(tmp_path):
    def change_evidence(phase, _):
        if phase == "after_done":
            report = next((tmp_path / "reports").iterdir())
            (report / "evidence/after/helper").chmod(0o600)

    with pytest.raises(ValueError, match="证据发生变化"):
        runner.run_case(_script_case(tmp_path), tmp_path / "reports",
                        control=runner.RunControl(progress=change_evidence))
    assert not list((tmp_path / "reports").rglob("report.json"))


@pytest.mark.parametrize("change", ["mode", "content"])
def test_source_changed_while_writing_report_prevents_publication(tmp_path, monkeypatch, change):
    from reprolab import report

    original = report.write_report

    def change_after_html(result):
        path = original(result)
        helper = Path(result["report_dir"]) / "evidence/after/helper"
        if change == "mode":
            helper.chmod(0o600)
        else:
            helper.write_text("#!/bin/sh\nexit 1\n")
        return path

    monkeypatch.setattr(report, "write_report", change_after_html)
    with pytest.raises(ValueError, match="证据发生变化"):
        runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    assert not list((tmp_path / "reports").rglob("report.json"))
    assert not list((tmp_path / "reports").rglob("report.html"))


def test_permissions_changed_during_export_do_not_publish_a_bundle(tmp_path, monkeypatch):
    from reprolab import evidence_bundle

    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    report = Path(result["report_dir"])
    original = evidence_bundle._zip_info

    def change_during_export(name, entry):
        if name == "evidence/after/helper":
            (report / name).chmod(0o600)
        return original(name, entry)

    monkeypatch.setattr(evidence_bundle, "_zip_info", change_during_export)
    with pytest.raises(ValueError, match="权限发生变化"):
        pack_report(report, tmp_path / "changed.zip")
    assert not (tmp_path / "changed.zip").exists()


def test_original_bundle_format_and_legacy_reports_can_still_be_imported(tmp_path):
    result = runner.run_case(_script_case(tmp_path), tmp_path / "reports")
    report = Path(result["report_dir"])
    _make_legacy_report(report)
    pack_report(report, tmp_path / "new.zip")

    with zipfile.ZipFile(tmp_path / "new.zip") as source, zipfile.ZipFile(tmp_path / "old.zip", "w") as target:
        for info in source.infolist():
            raw = source.read(info)
            if info.filename == "reprolab-bundle.json":
                manifest = json.loads(raw)
                manifest["schema_version"] = 1
                for item in manifest["entries"].values():
                    item["mode"] = 0o755 if item["mode"] & 0o111 else 0o644
                raw = json.dumps(manifest).encode()
            target.writestr(info, raw)

    unpack_report(tmp_path / "old.zip", tmp_path / "imported")
    checked = check_report(tmp_path / "imported")
    assert checked["status"] == "consistent"
    assert "权限" in checked["notice"]


def test_legacy_working_tree_case_without_mode_hash_still_runs(tmp_path):
    from reprolab.recipes import run_saved_case
    from reprolab.workspace import create_workspace_case
    from test_workflow import project_case

    repo, before, _, target = project_case(tmp_path)
    case = tmp_path / "case.json"
    create_workspace_case(case, repo, before, target,
                          failure_type="ValueError", failure_message="reported bug")
    legacy = json.loads(case.read_text())
    legacy["workspace_origin"].pop("source_mode_sha256")
    case.write_text(json.dumps(legacy))
    result = run_saved_case(case, tmp_path / "reports")
    assert result["status"] == "verified"
    assert "权限" in result["input_integrity_notice"]
    assert "权限" in Path(result["report_dir"], "report.html").read_text()
    assert "旧" in check_report(result["report_dir"])["notice"]
    from reprolab.repair_suite import prepare_explanation
    explanation = prepare_explanation(result["report_dir"])
    assert "未记录" in Path(explanation["directory"], "prompt.txt").read_text()
