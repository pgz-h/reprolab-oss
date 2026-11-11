import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from test_runner import make_case
from reprolab import runtime, runner


def test_frozen_worker_uses_explicit_dispatch(monkeypatch):
    assert hasattr(runtime, "worker_command"), "packaged worker entry is missing"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = runtime.worker_command(sys.executable, ["source", "test", "result", "{}"])
    assert command == [sys.executable, "--reprolab-worker", "source", "test", "result", "{}"]
    external = "/tmp/my-venv/bin/python"
    assert runtime.worker_command(external, ["source"])[0:2] == [external, "-I"]


def test_progress_reports_real_before_and_after(tmp_path):
    assert hasattr(runner, "RunControl"), "cancellable execution is missing"
    seen = []
    result = runner.run_case(make_case(tmp_path), tmp_path / "reports",
                             control=runner.RunControl(progress=lambda phase, result: seen.append((phase, result))))
    assert result["status"] == "verified"
    assert [p for p, _ in seen] == ["preparing", "before", "before_done", "after", "after_done", "complete"]
    assert seen[2][1]["kind"] == "failed"
    assert seen[4][1]["kind"] == "passed"


def test_cancel_kills_worker_and_child_without_success_report(tmp_path):
    assert hasattr(runner, "RunControl"), "cancellable execution is missing"
    marker = tmp_path / "child-alive"
    child_started = tmp_path / "child-started"
    # A child would write after cancellation if the worker group were not cleaned.
    script = f"import pathlib,time; pathlib.Path({str(child_started)!r}).touch(); time.sleep(1.5); pathlib.Path({str(marker)!r}).touch()"
    case = make_case(tmp_path, test=f"import subprocess,sys,time\ndef test_hangs():\n    subprocess.Popen([sys.executable, '-c', {script!r}])\n    time.sleep(30)\n")
    control = runner.RunControl()
    errors = []
    def execute():
        try:
            runner.run_case(case, tmp_path / "reports", control=control)
        except runner.RunCancelled:
            errors.append("cancelled")
    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 5
    while not child_started.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert child_started.exists()
    control.cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert errors == ["cancelled"]
    time.sleep(1.6)
    assert not marker.exists()
    assert not list((tmp_path / "reports").glob("*/report.json"))


def test_cancel_before_start_creates_no_report(tmp_path):
    assert hasattr(runner, "RunControl"), "cancellable execution is missing"
    control = runner.RunControl()
    control.cancel()
    with pytest.raises(runner.RunCancelled):
        runner.run_case(make_case(tmp_path), tmp_path / "reports", control=control)
    assert not (tmp_path / "reports").exists()


def test_frozen_doctor_reads_bundled_distribution_metadata(monkeypatch):
    from reprolab.doctor import diagnose
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    real_run = runtime.subprocess.run
    def reject_python_interpreter_flags(command, *args, **kwargs):
        assert not (command[0] == sys.executable and "-c" in command), "app cannot execute Python -c"
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(runtime.subprocess, "run", reject_python_interpreter_flags)
    result = diagnose(requirements=["pytest"])
    assert result["status"] == "ready"
    assert result["dependencies"]["pytest"]


def test_completed_run_no_longer_accepts_cancel(tmp_path):
    decisions = []
    control = runner.RunControl(progress=lambda phase, result:
                                decisions.append(control.cancel()) if phase == "complete" else None)
    result = runner.run_case(make_case(tmp_path), tmp_path / "reports", control=control)
    assert result["status"] == "verified"
    assert decisions == [False], "completed work must not acknowledge a cancellation it cannot honor"
