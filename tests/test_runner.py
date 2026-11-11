import hashlib
import json
from pathlib import Path
import time

import pytest


def make_case(tmp_path, before='raise ValueError("the reported bug")', after='return 42', test=None):
    for name, body in (("before", before), ("after", after)):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "sample.py").write_text(f"def compute():\n    {body}\n", encoding="utf-8")
    (tmp_path / "regression.py").write_text(
        test or "from sample import compute\ndef test_regression():\n    assert compute() == 42\n",
        encoding="utf-8",
    )
    config = {"title": "回归验证", "description": "一个已知问题", "before": "before", "after": "after",
              "test": "regression.py", "expected_failure": {"type": "ValueError", "message_contains": "the reported bug"}}
    path = tmp_path / "case.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def verify(path, output, timeout=15):
    # Keep imports inside tests so the initial red run reports the missing feature explicitly.
    import reprolab
    assert hasattr(reprolab, "run_case"), "before/after verification is not implemented yet"
    return reprolab.run_case(path, output, timeout=timeout)


def test_verifies_only_matching_failure_then_same_test_passes(tmp_path):
    case = make_case(tmp_path)
    original = (tmp_path / "before/sample.py").read_bytes()
    result = verify(case, tmp_path / "reports")
    assert result["status"] == "verified"
    assert result["before"]["kind"] == "failed"
    assert result["before"]["failure"]["type"] == "ValueError"
    assert result["after"]["kind"] == "passed"
    assert result["before"]["test_id"] == result["after"]["test_id"]
    assert (tmp_path / "before/sample.py").read_bytes() == original
    assert not list((tmp_path / "before").glob("__pycache__"))
    report_dir = Path(result["report_dir"])
    assert (report_dir / "evidence/before/sample.py").read_bytes() == original
    assert result["test_sha256"] == hashlib.sha256((tmp_path / "regression.py").read_bytes()).hexdigest()
    assert (report_dir / "before.log").is_file()


@pytest.mark.parametrize(("before", "after", "expected"), [
    ("return 42", "return 42", "not_reproduced"),
    ('raise ValueError("the reported bug")', 'raise ValueError("the reported bug")', "still_failing"),
    ('raise TypeError("the reported bug")', "return 42", "inconclusive"),
    ('raise ValueError("another problem")', "return 42", "inconclusive"),
])
def test_does_not_claim_other_outcomes_are_verified(tmp_path, before, after, expected):
    result = verify(make_case(tmp_path, before, after), tmp_path / "reports")
    assert result["status"] == expected


@pytest.mark.parametrize("test", [
    "import definitely_nonexistent_reprolab_module\ndef test_one(): pass\n",
    "def test_one(:\n    pass\n",
    "# no tests\n",
    "import pytest\n@pytest.mark.skip(reason='not ready')\ndef test_one(): pass\n",
    "import pytest\n@pytest.mark.xfail(reason='known bug')\ndef test_one(): assert False\n",
    "import pytest\n@pytest.mark.xfail(reason='known bug')\ndef test_one(): pass\n",
    "def test_one(): pass\ndef test_two(): pass\n",
    "import pytest\n@pytest.fixture\ndef broken(): raise ValueError('the reported bug')\ndef test_one(broken): pass\n",
    "import pytest\n@pytest.fixture\ndef broken():\n    yield\n    raise ValueError('the reported bug')\ndef test_one(broken): pass\n",
])
def test_infrastructure_and_non_test_outcomes_are_inconclusive(tmp_path, test):
    result = verify(make_case(tmp_path, test=test), tmp_path / "reports")
    assert result["status"] == "inconclusive"


def test_timeout_is_inconclusive(tmp_path):
    case = make_case(tmp_path, test="import time\ndef test_hangs(): time.sleep(30)\n")
    result = verify(case, tmp_path / "reports", timeout=0.5)
    assert result["status"] == "inconclusive"
    assert result["before"]["timed_out"] is True


def test_does_not_inherit_secret_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("REPROLAB_TEST_SECRET", "must-not-leak")
    test = "import os\nfrom sample import compute\ndef test_regression():\n    assert 'REPROLAB_TEST_SECRET' not in os.environ\n    assert compute() == 42\n"
    result = verify(make_case(tmp_path, test=test), tmp_path / "reports")
    assert result["status"] == "verified"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301])
def test_rejects_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError):
        verify(make_case(tmp_path), tmp_path / "reports", timeout=timeout)


def test_rejects_output_overlapping_source(tmp_path):
    case = make_case(tmp_path)
    with pytest.raises(ValueError, match="output|输出"):
        verify(case, tmp_path / "before/reports")


def test_rejects_symlink_in_source(tmp_path):
    case = make_case(tmp_path)
    (tmp_path / "before/linked.py").symlink_to(tmp_path / "after/sample.py")
    with pytest.raises(ValueError, match="symlink|符号链接"):
        verify(case, tmp_path / "reports")


def test_rejects_empty_failure_match(tmp_path):
    case = make_case(tmp_path)
    data = json.loads(case.read_text())
    data["expected_failure"]["message_contains"] = ""
    case.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        verify(case, tmp_path / "reports")


def test_empty_directories_are_preserved_so_copying_cannot_fabricate_a_bug(tmp_path):
    before = "from pathlib import Path; Path('assets/output.txt').write_text('ok'); return 42"
    after = "from pathlib import Path; Path('assets').mkdir(exist_ok=True); Path('assets/output.txt').write_text('ok'); return 42"
    case = make_case(tmp_path, before=before, after=after)
    for label in ("before", "after"):
        (tmp_path / label / "assets").mkdir()
    data = json.loads(case.read_text())
    data["expected_failure"] = {"type": "FileNotFoundError", "message_contains": "assets/output.txt"}
    case.write_text(json.dumps(data))
    result = verify(case, tmp_path / "out")
    assert result["status"] == "not_reproduced"
    assert (Path(result["report_dir"]) / "evidence/before/assets").is_dir()
    assert not (tmp_path / "before/assets/output.txt").exists()
    (tmp_path / "before/another-empty-directory").mkdir()
    second = verify(case, tmp_path / "out")
    assert result["source_sha256"]["before"] != second["source_sha256"]["before"]


@pytest.mark.parametrize("hang", [False, True])
def test_background_children_are_stopped_at_end_of_each_run(tmp_path, hang):
    marker = tmp_path / "background-finished"
    child = f"import time; from pathlib import Path; time.sleep(1.4); Path({str(marker)!r}).write_text('leaked')"
    action = "time.sleep(30)" if hang else "assert compute() == 42"
    test = f"import subprocess, sys, time\nfrom sample import compute\ndef test_regression():\n    subprocess.Popen([sys.executable, '-c', {child!r}])\n    {action}\n"
    result = verify(make_case(tmp_path, test=test), tmp_path / "out", timeout=0.5 if hang else 15)
    assert result["status"] == ("inconclusive" if hang else "verified")
    time.sleep(1.5)
    assert not marker.exists(), "A child process survived after verification returned"
