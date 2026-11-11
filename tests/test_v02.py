import json
from pathlib import Path

import pytest

from reprolab import run_case
from test_runner import make_case


def test_records_observations_without_changing_verdict(tmp_path):
    test = "from sample import compute\ndef test_one(record_property):\n    record_property('input', 'same input')\n    assert compute() == 42\n"
    result = run_case(make_case(tmp_path, test=test), tmp_path / "out")
    assert result["status"] == "verified"
    assert result["before"]["observations"] == [{"name": "input", "value": "same input"}]
    assert result["after"]["observations"] == [{"name": "input", "value": "same input"}]


def test_provenance_survives_report_and_evidence_replay(tmp_path):
    case = make_case(tmp_path)
    data = json.loads(case.read_text())
    metadata = {"project": "Example", "issue_url": "https://github.com/example/project/pull/1",
                "before_ref": "v1", "after_ref": "v2", "scope": "单模块"}
    data["provenance"] = metadata
    case.write_text(json.dumps(data))
    result = run_case(case, tmp_path / "out")
    assert result["provenance"] == metadata
    replay = json.loads((Path(result["report_dir"]) / "evidence/case.json").read_text())
    assert replay["provenance"] == metadata


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://example.com", "https://user:secret@example.com", "https:///missing", "https://example.com/\npath"])
def test_unsafe_provenance_links_are_rejected(tmp_path, url):
    case = make_case(tmp_path)
    data = json.loads(case.read_text())
    data["provenance"] = {"project": "Example", "issue_url": url, "before_ref": "v1", "after_ref": "v2", "scope": "module"}
    case.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        run_case(case, tmp_path / "out")
