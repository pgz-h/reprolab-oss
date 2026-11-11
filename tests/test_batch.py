import json
from pathlib import Path

from test_cli import cli
from test_runner import make_case


def saved_case(directory, name, *, after="return 42", title=None):
    fixture = directory / "fixtures" / name
    fixture.mkdir(parents=True)
    config = make_case(fixture, after=after)
    data = json.loads(config.read_text())
    for key in ("before", "after", "test"):
        data[key] = f"fixtures/{name}/{data[key]}"
    data["title"] = title or name
    case = directory / f"{name}.json"
    case.write_text(json.dumps(data), encoding="utf-8")
    return case


def test_case_listing_reads_metadata_without_running_code(tmp_path):
    cases = tmp_path / "cases"
    case = saved_case(cases, "example")
    marker = tmp_path / "must-not-run"
    (cases / "fixtures/example/regression.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef test_one(): pass\n"
    )
    result = cli("cases", cases, "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["cases"][0]["file"] == str(case)
    assert data["cases"][0]["title"] == "example"
    assert not marker.exists()


def test_batch_continues_after_bad_case_and_reports_all_outcomes(tmp_path):
    cases = tmp_path / "cases"
    saved_case(cases, "02-success")
    saved_case(cases, "03-still-broken", after='raise ValueError("the reported bug")')
    (cases / "01-bad.json").write_text("{invalid")
    result = cli("batch", cases, "--output", tmp_path / "reports", "--json")
    assert result.returncode == 2
    data = json.loads(result.stdout)
    assert data["total"] == data["completed"] == 3
    assert data["counts"]["verified"] == 1
    assert data["counts"]["still_failing"] == 1
    assert data["counts"]["inconclusive"] == 1
    folder = Path(data["summary_dir"])
    assert (folder / "summary.html").is_file()
    assert len([e for e in data["entries"] if e.get("report")]) == 2
    for entry in data["entries"]:
        if entry.get("report"):
            assert (folder / entry["report"]).is_file()
    assert "01-bad" in (folder / "summary.html").read_text()


def test_all_verified_batch_is_success_and_escapes_case_titles(tmp_path):
    cases = tmp_path / "cases"
    title = '<img src=x onerror="alert(1)">'
    saved_case(cases, "one", title=title)
    result = cli("batch", cases, "--output", tmp_path / "reports", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "verified"
    html = (Path(data["summary_dir"]) / "summary.html").read_text()
    assert title not in html
    assert "&lt;img" in html
    assert data["counts"]["verified"] == 1


def test_batch_with_unreproduced_case_has_failure_exit_code(tmp_path):
    cases = tmp_path / "cases"
    case = saved_case(cases, "not-reproduced")
    (cases / "fixtures/not-reproduced/before/sample.py").write_text("def compute():\n    return 42\n")
    result = cli("batch", cases, "--output", tmp_path / "reports", "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout)["counts"]["not_reproduced"] == 1


def test_empty_batch_does_not_claim_success(tmp_path):
    cases = tmp_path / "empty"
    cases.mkdir()
    result = cli("batch", cases, "--output", tmp_path / "reports", "--json")
    assert result.returncode == 2
    assert "error" in json.loads(result.stdout)


def test_batch_output_cannot_be_created_inside_case_source(tmp_path):
    cases = tmp_path / "cases"
    saved_case(cases, "one")
    output = cases / "fixtures/one/before/reports"
    result = cli("batch", cases, "--output", output, "--json")
    assert result.returncode == 2
    assert not output.exists()
