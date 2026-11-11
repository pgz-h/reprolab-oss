"""Selected batches keep independent case verdicts and a cancellable parent."""
import json
from pathlib import Path
import threading
import time

import pytest

from reprolab import batch, runner
from test_batch import saved_case


def run_selected(paths, output, **kwargs):
    assert hasattr(batch, "run_selected_cases"), "selected-case batching is not implemented"
    return batch.run_selected_cases(paths, output, **kwargs)


def persisted(summary):
    return json.loads((Path(summary["summary_dir"]) / "summary.json").read_text())


def test_selected_paths_keep_user_order_across_directories_and_deduplicate(tmp_path):
    first = saved_case(tmp_path / "alpha", "first")
    second = saved_case(tmp_path / "beta", "second")
    summary = run_selected([second, first, second], tmp_path / "reports")
    assert [entry["file"] for entry in summary["entries"]] == [str(second), str(first)]
    assert summary["status"] == "verified"
    assert summary["total"] == summary["completed"] == 2
    assert persisted(summary) == summary
    for entry in summary["entries"]:
        assert (Path(summary["summary_dir"]) / entry["report"]).is_file()


def test_bad_and_missing_selected_cases_do_not_block_later_case(tmp_path):
    good = saved_case(tmp_path / "cases", "good")
    bad = tmp_path / "bad.json"
    bad.write_text("{invalid")
    missing = tmp_path / "missing.json"
    summary = run_selected([bad, missing, good], tmp_path / "reports")
    assert [entry["status"] for entry in summary["entries"]] == ["inconclusive", "inconclusive", "verified"]
    assert summary["completed"] == 3
    assert summary["exit_code"] == 2
    assert persisted(summary) == summary


def test_symlink_case_is_recorded_without_running_target(tmp_path):
    good = saved_case(tmp_path / "cases", "good")
    link = tmp_path / "linked.json"
    link.symlink_to(good)
    summary = run_selected([link], tmp_path / "reports")
    assert summary["entries"][0]["status"] == "inconclusive"
    assert summary["entries"][0]["report"] is None
    assert "符号链接" in summary["entries"][0]["explanation"]


@pytest.mark.parametrize("last", [False, True])
def test_cancel_after_completed_case_preserves_verdict_and_parent_accepts_cancel(tmp_path, last):
    first = saved_case(tmp_path / "cases", "first")
    second = saved_case(tmp_path / "cases", "second")
    decisions, events = [], []
    def progress(phase, payload):
        events.append((phase, payload))
        if phase == "batch_case_done" and payload["index"] == (2 if last else 1):
            decisions.append(control.cancel())
    control = runner.RunControl(progress=progress)
    summary = run_selected([first, second], tmp_path / "reports", control=control)
    assert decisions == [True]
    assert summary["status"] == "cancelled"
    assert summary["exit_code"] == 130
    assert summary["completed"] == (2 if last else 1)
    assert [entry["status"] for entry in summary["entries"]] == (["verified", "verified"] if last else ["verified", "pending"])
    assert (Path(summary["summary_dir"]) / summary["entries"][0]["report"]).is_file()
    assert persisted(summary) == summary
    assert events[-1] == ("batch_done", summary)
    html = (Path(summary["summary_dir"]) / "summary.html").read_text()
    assert "本批次已取消" in html


@pytest.mark.parametrize("last", [False, True])
def test_cancel_current_second_case_kills_its_child_and_keeps_first_report(tmp_path, last):
    first = saved_case(tmp_path / "cases", "first")
    second = saved_case(tmp_path / "cases", "second")
    third = saved_case(tmp_path / "cases", "third")
    started, leaked = tmp_path / "started", tmp_path / "leaked"
    script = f"import pathlib,time; pathlib.Path({str(started)!r}).touch(); time.sleep(1.3); pathlib.Path({str(leaked)!r}).touch()"
    (second.parent / "fixtures/second/regression.py").write_text(
        f"import subprocess,sys,time\ndef test_hangs():\n    subprocess.Popen([sys.executable, '-c', {script!r}])\n    time.sleep(30)\n")
    control, results, errors = runner.RunControl(), [], []
    def execute():
        try:
            results.append(run_selected([first, second] if last else [first, second, third],
                                        tmp_path / "reports", control=control))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute)
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not started.exists() and time.monotonic() < deadline and thread.is_alive():
            time.sleep(.02)
        assert started.exists(), errors
        assert control.cancel() is True
        thread.join(3)
        assert not thread.is_alive()
        assert not errors
        summary = results[0]
        assert [entry["status"] for entry in summary["entries"]] == (["verified", "cancelled"] if last else ["verified", "cancelled", "pending"])
        assert summary["completed"] == 1
        assert summary["counts"]["cancelled"] == 1
        assert summary["status"] == "cancelled"
        assert summary["entries"][1]["report"] is None
        assert persisted(summary) == summary
        assert len(list(Path(summary["summary_dir"]).glob("runs/*/report.json"))) == 1
        time.sleep(1.4)
        assert not leaked.exists()
    finally:
        control.cancel()
        thread.join(3)


def test_precancelled_batch_has_no_verdict_or_run(tmp_path):
    case = saved_case(tmp_path / "cases", "first")
    control = runner.RunControl()
    control.cancel()
    summary = run_selected([case], tmp_path / "reports", control=control)
    assert summary["status"] == "cancelled"
    assert summary["completed"] == 0
    assert summary["counts"]["verified"] == 0
    assert summary["entries"][0]["status"] == "pending"
    assert not (Path(summary["summary_dir"]) / "runs").exists()
    assert persisted(summary) == summary


def test_progress_snapshots_keep_pending_start_and_forward_real_results(tmp_path):
    case = saved_case(tmp_path / "cases", "first")
    events = []
    control = runner.RunControl(progress=lambda phase, payload: events.append((phase, payload)))
    summary = run_selected([case], tmp_path / "reports", control=control)
    assert events[0] == ("batch_start", {"summary_dir": summary["summary_dir"], "total": 1})
    start = next(payload for phase, payload in events if phase == "batch_case_start")
    done = next(payload for phase, payload in events if phase == "batch_case_done")
    assert start["entry"]["status"] == "pending"
    assert done["entry"]["status"] == "verified"
    assert start["index"] == done["index"] == start["total"] == 1
    before = next(payload for phase, payload in events if phase == "batch_progress" and payload["phase"] == "before_done")
    assert before["index"] == before["total"] == 1
    assert before["result"]["kind"] == "failed"
    assert events[-1] == ("batch_done", summary)
    assert control.cancel() is False
    done["entry"]["status"] = "changed by consumer"
    assert summary["entries"][0]["status"] == "verified"


def test_keyboard_interrupt_keeps_partial_summary(tmp_path):
    first = saved_case(tmp_path / "cases", "first")
    second = saved_case(tmp_path / "cases", "second")
    def progress(phase, payload):
        if phase == "batch_case_start" and payload["index"] == 2:
            raise KeyboardInterrupt
    summary = run_selected([first, second], tmp_path / "reports", control=runner.RunControl(progress=progress))
    assert summary["exit_code"] == 130
    assert summary["completed"] == 1
    assert summary["entries"][0]["status"] == "verified"
    assert persisted(summary) == summary


@pytest.mark.parametrize("paths", [[], [f"case-{i}.json" for i in range(51)]])
def test_rejects_empty_and_oversized_selections_without_success(tmp_path, paths):
    with pytest.raises(ValueError):
        run_selected(paths, tmp_path / "reports")
    assert not (tmp_path / "reports").exists()


def test_case_runtime_error_is_recorded_and_later_case_still_runs(tmp_path):
    broken = saved_case(tmp_path / "cases", "broken")
    data = json.loads(broken.read_text())
    data["before"] = "missing-source"
    broken.write_text(json.dumps(data))
    good = saved_case(tmp_path / "cases", "good")
    summary = run_selected([broken, good], tmp_path / "reports")
    assert [entry["status"] for entry in summary["entries"]] == ["inconclusive", "verified"]
    assert summary["entries"][0]["explanation"]
    assert summary["completed"] == 2
    assert summary["exit_code"] == 2
    assert persisted(summary) == summary


def test_cancellation_after_current_case_commits_keeps_real_verdict(tmp_path):
    first = saved_case(tmp_path / "cases", "first")
    second = saved_case(tmp_path / "cases", "second")
    decisions = []
    def progress(phase, payload):
        if phase == "batch_progress" and payload["phase"] == "complete":
            decisions.append(control.cancel())
    control = runner.RunControl(progress=progress)
    summary = run_selected([first, second], tmp_path / "reports", control=control)
    assert decisions == [True]
    assert summary["status"] == "cancelled"
    assert [entry["status"] for entry in summary["entries"]] == ["verified", "pending"]
    assert summary["completed"] == 1
    assert (Path(summary["summary_dir"]) / summary["entries"][0]["report"]).is_file()
    assert persisted(summary) == summary


def test_run_batch_progress_callback_remains_available_and_gets_independent_entries(tmp_path):
    saved_case(tmp_path / "cases", "first")
    seen = []
    def progress(index, total, entry):
        seen.append((index, total, entry["status"]))
        entry["status"] = "consumer mutation"
    summary = batch.run_batch(tmp_path / "cases", tmp_path / "reports", progress=progress)
    assert seen == [(1, 1, "verified")]
    assert summary["status"] == summary["entries"][0]["status"] == "verified"


@pytest.mark.parametrize("timeout", [0, 301, float("nan"), float("inf"), "15"])
def test_invalid_batch_timeout_does_not_create_summary(tmp_path, timeout):
    case = saved_case(tmp_path / "cases", "first")
    with pytest.raises(ValueError):
        run_selected([case], tmp_path / "reports", timeout=timeout)
    assert not (tmp_path / "reports").exists()
