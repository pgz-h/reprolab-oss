"""Real evidence round trips and bounded archive validation; no model calls."""
import hashlib
import json
from pathlib import Path
import shutil
import stat
import warnings
import zipfile

import pytest

from test_cli import cli
from test_runner import make_case


@pytest.fixture(scope="module")
def original_report(tmp_path_factory):
    from reprolab.runner import run_case
    from reprolab.report import write_report
    root=tmp_path_factory.mktemp("bundle-original")
    marker=root/"test-executed"
    test=f"from pathlib import Path\nfrom sample import compute\nPath({str(marker)!r}).write_text('executed')\ndef test_regression():\n    assert compute() == 42\n"
    case=make_case(root,test=test)
    for label in ("before","after"):
        (root/label/"empty-assets").mkdir()
        helper=root/label/"helper.sh";helper.write_text("#!/bin/sh\nexit 0\n");helper.chmod(0o755)
    result=run_case(case,root/"reports");write_report(result)
    marker.unlink()
    return Path(result["report_dir"]), marker


def copy_report(tmp_path, original_report):
    target=tmp_path/"报告"
    shutil.copytree(original_report[0],target)
    return target


def pack(tmp_path, report):
    archive=tmp_path/"证据包.zip"
    result=cli("report","pack",report,"--output",archive,"--json")
    assert result.returncode == 0,result.stdout+result.stderr
    return archive


def test_pack_unpack_and_replay_preserve_evidence_empty_dirs_and_modes(tmp_path, original_report):
    report=copy_report(tmp_path,original_report)
    (report/"ai.local.json").write_text('{"api_key":"must-not-bundle"}')
    original_report[1].unlink(missing_ok=True)
    checked=cli("report","check",report,"--json")
    assert checked.returncode == 0,checked.stdout+checked.stderr
    assert json.loads(checked.stdout)["status"] == "consistent"
    assert json.loads(checked.stdout)["recorded_verdict"] == "verified"
    archive=pack(tmp_path,report)
    with zipfile.ZipFile(archive) as z:
        assert "ai.local.json" not in z.namelist()
        assert "reprolab-bundle.json" in z.namelist()
        assert "README_REPLAY.txt" in z.namelist()
    destination=tmp_path/"imported"
    imported=cli("report","unpack",archive,destination,"--json")
    assert imported.returncode == 0,imported.stdout+imported.stderr
    assert not original_report[1].exists(), "Checking/packing/importing must not execute code"
    assert (destination/"evidence/before/empty-assets").is_dir()
    assert (destination/"evidence/after/helper.sh").stat().st_mode & stat.S_IXUSR
    before=json.loads((report/"report.json").read_text())
    replay=cli("verify",destination/"evidence/case.json","--output",tmp_path/"replayed","--json")
    assert replay.returncode == 0,replay.stdout+replay.stderr
    after=json.loads(replay.stdout)
    assert after["status"] == "verified"
    assert after["source_sha256"] == before["source_sha256"]
    assert after["test_sha256"] == before["test_sha256"]


@pytest.mark.parametrize("path",["evidence/regression.py","evidence/before/sample.py","evidence/after/sample.py"])
def test_changed_evidence_is_reported_and_cannot_be_packed(tmp_path,original_report,path):
    report=copy_report(tmp_path,original_report)
    target=report/path;target.write_text(target.read_text()+"\n# changed\n")
    result=cli("report","check",report,"--json")
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "inconsistent"
    archive=tmp_path/"bad.zip"
    packed=cli("report","pack",report,"--output",archive,"--json")
    assert packed.returncode == 2
    assert not archive.exists()


def test_changed_case_contract_is_not_silently_exported(tmp_path,original_report):
    report=copy_report(tmp_path,original_report)
    path=report/"evidence/case.json";data=json.loads(path.read_text())
    data["expected_failure"]["message_contains"]="a different bug";path.write_text(json.dumps(data))
    result=cli("report","check",report,"--json")
    assert result.returncode == 2
    assert "expected_failure" in result.stdout


@pytest.mark.parametrize("change",["missing","symlink","outside-path"])
def test_invalid_evidence_layout_is_rejected_without_following_external_paths(tmp_path,original_report,change):
    report=copy_report(tmp_path,original_report)
    if change == "missing":
        (report/"before.log").unlink()
    elif change == "symlink":
        (report/"evidence/regression.py").unlink()
        (report/"evidence/regression.py").symlink_to(original_report[0]/"evidence/regression.py")
    else:
        p=report/"evidence/case.json";d=json.loads(p.read_text());d["before"]="/tmp";p.write_text(json.dumps(d))
    result=cli("report","check",report,"--json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def rewrite_bundle(archive,destination,modify):
    with zipfile.ZipFile(archive) as z:
        items=[(info,z.read(info)) for info in z.infolist()]
    items=modify(items)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore",UserWarning)
        with zipfile.ZipFile(destination,"w") as z:
            for info,raw in items:z.writestr(info,raw)


@pytest.mark.parametrize("kind",["tamper","escape","duplicate","symlink"])
def test_bad_archives_are_rejected_before_publishing_destination(tmp_path,original_report,kind):
    archive=pack(tmp_path,copy_report(tmp_path,original_report))
    bad=tmp_path/"bad.zip"
    def modify(items):
        if kind == "tamper":
            return [(info,b"modified" if info.filename=="evidence/regression.py" else raw) for info,raw in items]
        if kind == "duplicate":
            return items+[next(item for item in items if item[0].filename=="report.json")]
        info=zipfile.ZipInfo("../escaped.txt" if kind=="escape" else "evidence/before/link")
        info.create_system=3;info.external_attr=((stat.S_IFLNK if kind=="symlink" else stat.S_IFREG)|0o644)<<16
        return items+[(info,b"outside")]
    rewrite_bundle(archive,bad,modify)
    destination=tmp_path/"rejected"
    result=cli("report","unpack",bad,destination,"--json")
    assert result.returncode == 2,result.stdout
    assert not destination.exists()
    assert not (tmp_path/"escaped.txt").exists()


def test_existing_output_and_destination_are_never_overwritten(tmp_path,original_report):
    report=copy_report(tmp_path,original_report)
    archive=pack(tmp_path,report);old=archive.read_bytes()
    result=cli("report","pack",report,"--output",archive,"--json")
    assert result.returncode == 2 and archive.read_bytes()==old
    dest=tmp_path/"keep";dest.mkdir();(dest/"note.txt").write_text("keep")
    result=cli("report","unpack",archive,dest,"--json")
    assert result.returncode == 2 and (dest/"note.txt").read_text()=="keep"


def test_bundle_size_limit_is_checked_before_import(tmp_path,original_report,monkeypatch):
    from reprolab import evidence_bundle
    archive=pack(tmp_path,copy_report(tmp_path,original_report))
    monkeypatch.setattr(evidence_bundle,"MAX_BUNDLE_BYTES",100)
    dest=tmp_path/"too-big"
    with pytest.raises(ValueError):evidence_bundle.unpack_report(archive,dest)
    assert not dest.exists()


def test_replay_case_cannot_switch_to_git_source(tmp_path,original_report):
    report=copy_report(tmp_path,original_report)
    p=report/"evidence/case.json";data=json.loads(p.read_text());data["source"]="git";p.write_text(json.dumps(data))
    result=cli("report","check",report,"--json")
    assert result.returncode == 2


def test_float_permission_in_manifest_is_a_readable_error(tmp_path,original_report):
    archive=pack(tmp_path,copy_report(tmp_path,original_report))
    bad=tmp_path/"bad-mode.zip"
    def modify(items):
        changed=[]
        for info,raw in items:
            if info.filename=="reprolab-bundle.json":
                data=json.loads(raw);data["entries"]["report.json"]["mode"]=420.0;raw=json.dumps(data).encode()
            changed.append((info,raw))
        return changed
    rewrite_bundle(archive,bad,modify)
    result=cli("report","unpack",bad,tmp_path/"bad-mode","--json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_pack_size_budget_includes_its_manifest(tmp_path,original_report,monkeypatch):
    from reprolab import evidence_bundle
    report=copy_report(tmp_path,original_report)
    archive=pack(tmp_path,report)
    with zipfile.ZipFile(archive) as z:total=sum(info.file_size for info in z.infolist())
    assert archive.stat().st_size < total-100
    monkeypatch.setattr(evidence_bundle,"MAX_BUNDLE_BYTES",total-100)
    output=tmp_path/"too-large.zip"
    with pytest.raises(ValueError):evidence_bundle.pack_report(report,output)
    assert not output.exists()


def test_uncompressed_budget_is_checked_even_when_zip_is_small(tmp_path,original_report,monkeypatch):
    from reprolab import evidence_bundle
    archive=pack(tmp_path,copy_report(tmp_path,original_report))
    with zipfile.ZipFile(archive) as z:total=sum(info.file_size for info in z.infolist())
    assert archive.stat().st_size < total-100
    monkeypatch.setattr(evidence_bundle,"MAX_BUNDLE_BYTES",total-100)
    destination=tmp_path/"too-large"
    with pytest.raises(ValueError,match="总大小"):evidence_bundle.unpack_report(archive,destination)
    assert not destination.exists()
