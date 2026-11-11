import json
from pathlib import Path

from test_cli import cli
from test_runner import make_case


def test_historical_case_exposes_real_values_provenance_and_fix_diff(tmp_path):
    completed = cli("history", "--output", tmp_path, "--json")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "verified"
    assert result["demo"] is False
    assert result["provenance"]["project"] == "humanize"
    old = {p["name"]: p["value"] for p in result["before"]["observations"]}
    new = {p["name"]: p["value"] for p in result["after"]["observations"]}
    assert old["实际输出"] == "1.0 Byte"
    assert new["实际输出"] == "1 Byte"
    assert old["预期输出"] == new["预期输出"] == "1 Byte"
    changed = result["source_changes"]["files"]
    assert len(changed) == 1
    assert changed[0]["path"] == "filesize.py"
    assert '+        return f"{int(bytes_)} Byte"' in changed[0]["diff"]
    html = (Path(result["report_dir"]) / "report.html").read_text()
    assert "1.0 Byte" in html and "1 Byte" in html
    assert "https://github.com/python-humanize/humanize/pull/250" in html


def test_observations_and_source_diff_are_escaped(tmp_path):
    text = '<img src=x onerror="alert(1)">'
    test = f"from sample import compute\ndef test_one(record_property):\n    record_property('实际输出', {text!r})\n    assert compute() == 42\n"
    case = make_case(tmp_path, test=test, after=f"value={text!r}; return 42")
    completed = cli("verify", case, "--output", tmp_path / "out", "--json")
    assert completed.returncode == 0
    data = json.loads(completed.stdout)
    html = (Path(data["report_dir"]) / "report.html").read_text()
    assert text not in html
    assert "&lt;img" in html


def test_invalid_python_encoding_still_gets_an_inconclusive_report(tmp_path):
    case = make_case(tmp_path)
    (tmp_path / "regression.py").write_bytes(b"# coding: definitely-invalid-codec\n# \xff\ndef test_one(): pass\n")
    completed = cli("verify", case, "--output", tmp_path / "out", "--json")
    assert completed.returncode == 2
    data = json.loads(completed.stdout)
    assert data["status"] == "inconclusive"
    assert (Path(data["report_dir"]) / "report.html").exists()
