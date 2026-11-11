import json
import os
from pathlib import Path
import subprocess
import sys

from test_runner import make_case


ROOT = Path(__file__).resolve().parents[1]


def cli(*arguments):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "reprolab", *map(str, arguments)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)


def test_bundled_demo_runs_real_before_after_and_saves_report(tmp_path):
    result = cli("demo", "--output", tmp_path / "中文 报告", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "verified"
    assert data["demo"] is True
    assert data["before"]["failure"]["type"] == "UnicodeEncodeError"
    assert data["after"]["kind"] == "passed"
    report = Path(data["report_dir"]) / "report.html"
    assert report.exists()
    assert "UnicodeEncodeError" in report.read_text(encoding="utf-8")
    assert (report.parent / "evidence/regression.py").exists()


def test_cli_exit_codes_distinguish_unchanged_bug_from_unusable_input(tmp_path):
    case = make_case(tmp_path, after='raise ValueError("the reported bug")')
    result = cli("verify", case, "--output", tmp_path / "out", "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "still_failing"
    invalid = cli("verify", tmp_path / "missing.json", "--output", tmp_path / "out")
    assert invalid.returncode == 2
    assert "Traceback" not in invalid.stderr


def test_reports_escape_user_text_and_keep_runs_separate(tmp_path):
    case = make_case(tmp_path)
    config = json.loads(case.read_text())
    config["title"] = '<script>alert("unsafe")</script>'
    case.write_text(json.dumps(config))
    first = cli("verify", case, "--output", tmp_path / "out", "--json")
    assert first.returncode == 0, first.stderr
    first_data = json.loads(first.stdout)
    html = (Path(first_data["report_dir"]) / "report.html").read_text(encoding="utf-8")
    assert '<script>alert("unsafe")</script>' not in html
    assert "&lt;script&gt;" in html
    second = cli("verify", case, "--output", tmp_path / "out", "--json")
    assert second.returncode == 0
    assert json.loads(second.stdout)["report_dir"] != first_data["report_dir"]


def test_saved_evidence_can_be_replayed(tmp_path):
    case = make_case(tmp_path)
    first = cli("verify", case, "--output", tmp_path / "out", "--json")
    assert first.returncode == 0
    data = json.loads(first.stdout)
    replay = cli("verify", Path(data["report_dir"]) / "evidence/case.json", "--output", tmp_path / "replay", "--json")
    assert replay.returncode == 0, replay.stderr
    replay_data = json.loads(replay.stdout)
    assert replay_data["test_sha256"] == data["test_sha256"]
    assert replay_data["source_sha256"] == data["source_sha256"]


def test_valid_python_source_encoding_also_renders_in_report(tmp_path):
    case = make_case(tmp_path)
    test = "# coding: latin-1\n# café\nfrom sample import compute\ndef test_regression():\n    assert compute() == 42\n"
    (tmp_path / "regression.py").write_bytes(test.encode("latin-1"))
    result = cli("verify", case, "--output", tmp_path / "out", "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads(result.stdout)
    html = (Path(data["report_dir"]) / "report.html").read_text(encoding="utf-8")
    assert "café" in html
