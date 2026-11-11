"""Deterministic changes during preparation must stop both workers."""
import json
from pathlib import Path

import pytest

from reprolab import runner
from test_runner import make_case


@pytest.mark.parametrize("change", ["source", "added", "removed", "test", "case", "symlink"])
def test_preparation_changes_are_rejected_before_execution(tmp_path, monkeypatch, change):
    case = make_case(tmp_path)
    original = runner.snapshot
    called = []
    def changing_snapshot(source, destination, entries):
        result = original(source, destination, entries)
        if destination.name == "before":
            if change == "source":
                (source / "sample.py").write_text("def compute(): return 42\n")
            elif change == "added":
                (source / "extra.py").write_text("VALUE = 1\n")
            elif change == "removed":
                (source / "sample.py").unlink()
            elif change == "symlink":
                (source / "sample.py").unlink()
                (source / "sample.py").symlink_to(tmp_path / "after/sample.py")
            elif change == "test":
                (tmp_path / "regression.py").write_text("def test_changed(): pass\n")
            else:
                data = json.loads(case.read_text())
                data["title"] = "changed during preparation"
                case.write_text(json.dumps(data))
        return result
    monkeypatch.setattr(runner, "snapshot", changing_snapshot)
    def no_worker(*args, **kwargs):
        called.append(True)
        raise AssertionError("worker must not start after input changed")
    monkeypatch.setattr(runner, "run_snapshot", no_worker)
    with pytest.raises((ValueError, OSError), match="变化|符号链接|不存在|No such"):
        runner.run_case(case, tmp_path / "reports")
    assert not called
    assert not list((tmp_path / "reports").rglob("report.json"))


def test_snapshot_changed_before_copy_is_not_accepted_as_original(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    original = runner.snapshot
    def mutate(source, destination, entries):
        if destination.name == "before":
            (source / "sample.py").write_text("def compute(): return 42\n")
        return original(source, destination, entries)
    monkeypatch.setattr(runner, "snapshot", mutate)
    with pytest.raises(ValueError, match="变化"):
        runner.run_case(case, tmp_path / "reports")


def test_unreadable_walk_is_not_silently_an_empty_snapshot(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    original = runner.os.walk
    def failed_walk(top, *args, **kwargs):
        if Path(top) == tmp_path / "before":
            handler = kwargs.get("onerror")
            if handler:
                handler(PermissionError("unreadable source"))
            return iter(())
        return original(top, *args, **kwargs)
    monkeypatch.setattr(runner.os, "walk", failed_walk)
    with pytest.raises(PermissionError, match="unreadable"):
        runner.run_case(case, tmp_path / "reports")
