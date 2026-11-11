import json
from pathlib import Path
import sys

import pytest

from test_cli import cli
from test_workflow import make_environment
from test_ai_api import api_server, api_answer, config


def test_doctor_reports_local_capabilities_without_claiming_verification():
    result=cli("doctor","--json")
    assert result.returncode == 0,result.stdout+result.stderr
    data=json.loads(result.stdout)
    assert data["status"] == "ready"
    assert data["capabilities"]["snapshot_verification"] is True
    assert data["capabilities"]["model_configuration"] is None
    assert data["runtime"]["executable"] == sys.executable
    assert data["tests_executed"] is False and data["network_used"] is False
    assert "verified" not in data["status"]


def test_selected_venv_and_distribution_metadata_are_checked_without_import(tmp_path):
    executable,purelib=make_environment(tmp_path,True)
    marker=tmp_path/"imported"
    (purelib/"doctor_example.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    metadata=purelib/"doctor_example-1.2.dist-info";metadata.mkdir()
    (metadata/"METADATA").write_text("Metadata-Version: 2.1\nName: doctor-example\nVersion: 1.2\n")
    result=cli("doctor","--python",executable,"--require","doctor-example","--json")
    assert result.returncode == 0,result.stdout+result.stderr
    data=json.loads(result.stdout)
    assert data["runtime"]["executable"] == str(executable)
    assert data["dependencies"]["doctor-example"] == "1.2"
    assert not marker.exists()


def test_missing_pytest_is_an_actionable_local_environment_result(tmp_path):
    executable,_=make_environment(tmp_path,False)
    result=cli("doctor","--python",executable,"--json")
    assert result.returncode == 2
    data=json.loads(result.stdout)
    assert data["status"] == "attention"
    assert data["capabilities"]["snapshot_verification"] is False
    assert any("pytest" in item["message"] for item in data["checks"])
    assert "Traceback" not in result.stderr


def test_missing_requested_distribution_is_reported():
    result=cli("doctor","--require","reprolab-doctor-not-installed","--json")
    assert result.returncode == 2
    data=json.loads(result.stdout)
    assert data["dependencies"]["reprolab-doctor-not-installed"] is None
    assert data["capabilities"]["snapshot_verification"] is False


@pytest.mark.parametrize("name",["../project","pytest>=8","package;command"])
def test_distribution_names_reject_paths_or_install_expressions(name):
    result=cli("doctor","--require",name,"--json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_model_config_is_checked_offline_and_never_prints_key(tmp_path):
    with api_server(api_answer()) as (url,requests):
        result=cli("doctor","--config",config(tmp_path,url),"--json")
    assert result.returncode == 0,result.stdout+result.stderr
    data=json.loads(result.stdout)
    assert data["capabilities"]["model_configuration"] is True
    assert data["model"]["connection_tested"] is False
    assert not requests
    assert "test-secret-do-not-save" not in result.stdout+result.stderr


def test_bad_optional_model_config_does_not_hide_working_verification(tmp_path):
    path=tmp_path/"bad.json";path.write_text('{"base_url":"https://example.invalid/v1","model":"model"}')
    result=cli("doctor","--config",path,"--json")
    assert result.returncode == 2
    data=json.loads(result.stdout)
    assert data["capabilities"]["snapshot_verification"] is True
    assert data["capabilities"]["model_configuration"] is False


def test_no_git_leaves_snapshot_mode_available(monkeypatch):
    from reprolab.doctor import diagnose
    monkeypatch.setattr("reprolab.doctor.shutil.which",lambda name:None)
    data=diagnose()
    assert data["status"] == "ready"
    assert data["capabilities"]["snapshot_verification"] is True
    assert data["capabilities"]["git_verification"] is False
    assert any(item["id"]=="git" and item["status"]=="warning" for item in data["checks"])


def test_broken_pytest_version_metadata_becomes_a_diagnostic_error(monkeypatch):
    from reprolab.doctor import diagnose
    monkeypatch.setattr("reprolab.doctor.choose_runtime",lambda executable=None:{"executable":sys.executable,"python_version":"3.14.7","pytest_version":None})
    result=diagnose()
    assert result["status"] == "attention"
    assert result["capabilities"]["snapshot_verification"] is False
    assert any(item["id"]=="runtime" and item["status"]=="error" for item in result["checks"])
