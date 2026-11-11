import json
from pathlib import Path

from test_runner import make_case
from test_cli import cli
from reprolab.runner import run_case
from reprolab.report import write_report


def test_actual_runs_record_environment_and_explain_expected_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("REPROLAB_TEST_SECRET", "private-test-value-never-record")
    result = run_case(make_case(tmp_path), tmp_path / "reports")
    assert "environment" in result, "execution environment inventory missing"
    assert result["environment"]["status"] == "recorded"
    assert result["environment"]["distributions"]["pytest"]
    assert result["diagnosis"]["before"]["code"] == "expected_failure_reproduced"
    assert result["diagnosis"]["after"]["summary"] == "同一测试通过。"
    saved = json.loads(Path(result["report_dir"], "report.json").read_text())
    assert saved["diagnosis"] == result["diagnosis"]
    assert "private-test-value-never-record" not in json.dumps(saved)
    html = write_report(result).read_text()
    assert "下一步" in html and "环境版本记录" in html


def test_missing_module_diagnosis_uses_real_worker_evidence(tmp_path):
    case = make_case(tmp_path, test="import reprolab_unavailable_fixture_module\ndef test_one(): pass\n")
    result = run_case(case, tmp_path / "reports")
    assert result["status"] == "inconclusive"
    assert "diagnosis" in result, "structured diagnosis missing"
    assert result["diagnosis"]["before"]["code"] == "module_not_found"
    assert result["before"]["diagnostic_exceptions"][0]["phase"] == "collection"
    assert "reprolab_unavailable_fixture_module" in result["diagnosis"]["before"]["detail"]


def test_environment_comparison_and_legacy_html_are_supported(tmp_path):
    case = make_case(tmp_path)
    first = run_case(case, tmp_path / "reports")
    assert "environment" in first, "environment reference is missing"
    previous = dict(first["environment"])
    previous["distributions"] = {**previous["distributions"], "teaching-package": "1.0"}
    second = run_case(case, tmp_path / "reports", previous_environment=previous)
    assert second["environment_comparison"]["status"] == "changed"
    assert second["environment_comparison"]["removed"] == [{"name": "teaching-package", "version": "1.0"}]
    second.pop("environment")
    second.pop("diagnosis")
    second.pop("environment_comparison")
    assert write_report(second).is_file()


def test_cli_preflight_never_executes_test_or_creates_report(tmp_path):
    marker = tmp_path / "executed"
    case = make_case(tmp_path, test=f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef test_one(): pass\n")
    result = cli("preflight", case, "--output", tmp_path / "reports", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "ready"
    assert data["tests_executed"] is False
    assert not marker.exists() and not (tmp_path / "reports").exists()


def test_cli_preflight_reports_invalid_environment_as_attention(tmp_path):
    case = make_case(tmp_path)
    result = cli("preflight", case, "--python", tmp_path / "missing-python", "--json")
    assert result.returncode == 2
    assert result.stdout, "preflight JSON response missing"
    data = json.loads(result.stdout)
    assert data["status"] == "attention" and data["tests_executed"] is False


def test_worker_exit_without_record_is_not_claimed_to_be_zero_collection(tmp_path):
    case = make_case(tmp_path, test="def test_one():\n    import os\n    os._exit(0)\n")
    result = run_case(case, tmp_path / "reports")
    assert result["status"] == "inconclusive"
    assert result["diagnosis"]["before"]["code"] != "no_tests"
    assert result["before"]["worker_record_complete"] is False


def test_actual_zero_collection_still_has_zero_test_diagnosis(tmp_path):
    result = run_case(make_case(tmp_path, test="# intentionally no tests\n"), tmp_path / "reports")
    assert result["diagnosis"]["before"]["code"] == "no_tests"
    assert result["before"]["worker_record_complete"] is True
