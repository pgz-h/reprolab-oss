"""Copy, execute and conservatively evaluate two trusted Python snapshots."""
from .process_tree import spawn, terminate_tree

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import threading
import uuid

from .case import load_case, parse_test_target, legacy_mode_notice
from .runtime import choose_runtime, worker_command, snapshot_process_environment
from .pytest_profile import read_pytest_profile, profile_args
from .runtime_environment import resolve_variables, variable_record
from .changes import describe_changes
from .environment import snapshot_environment, compare_environments
from .diagnostics import diagnose_result, diagnose_run
from .persistence import RunJournal, publish_report, atomic_write


EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"}
MAX_ENTRIES = 5000
MAX_BYTES = 100 * 1024 * 1024



class RunCancelled(Exception):
    """Explicitly incomplete; never converted to a verification verdict."""


class RunControl:
    def __init__(self, progress=None, *, runtime_values=None):
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._committed = False
        self.progress = progress or (lambda phase, result: None)
        self.runtime_values = dict(runtime_values or {})

    def child(self, progress=None):
        """Share cancellation atomically while keeping a separate commit boundary."""
        child = RunControl(progress=progress, runtime_values=self.runtime_values)
        child._cancelled = self._cancelled
        child._lock = self._lock
        return child

    def cancel(self):
        with self._lock:
            if self._committed:
                return False
            self._cancelled.set()
            return True

    def commit(self):
        # Atomic cancellation boundary: after both tests, before publishing a verdict.
        with self._lock:
            self.check()
            self._committed = True

    def check(self):
        if self._cancelled.is_set():
            raise RunCancelled("已取消验收；没有产生新的验收结论。")

    def notify(self, phase, result=None):
        self.check()
        self.progress(phase, result)


def source_entries(source: Path) -> list[Path]:
    entries, total = [], 0
    def fail_walk(error):
        raise error
    for directory, dirs, names in os.walk(source, followlinks=False, onerror=fail_walk):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith(".env"))
        for name in [*dirs, *sorted(names)]:
            if name in EXCLUDED or name.startswith(".env"):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"源码中包含不支持的符号链接：{path}")
            if not path.is_dir() and not path.is_file():
                raise ValueError(f"源码中包含特殊文件：{path}")
            if path.is_file():
                total += path.stat().st_size
            entries.append(path.relative_to(source))
            if len(entries) > MAX_ENTRIES or total > MAX_BYTES:
                raise ValueError("源码超过首版限制：5000 个文件/目录或 100 MiB。")
    return sorted(entries)


def source_fingerprint(source: Path) -> tuple[list[Path], str]:
    entries = source_entries(source)
    manifest = []
    for relative in entries:
        path = source / relative
        if path.is_symlink():
            raise ValueError(f"源码准备期间出现符号链接：{path}")
        if path.is_dir():
            manifest.append([relative.as_posix(), "directory"])
        else:
            manifest.append([relative.as_posix(), "file", hashlib.sha256(path.read_bytes()).hexdigest()])
    return entries, fingerprint_manifest(manifest)


def source_mode_fingerprint(source: Path, entries=None) -> str:
    """Fingerprint regular-file permission bits separately from legacy content hashes."""
    entries = source_entries(source) if entries is None else entries
    manifest = []
    for relative in entries:
        path = source / relative
        if path.is_symlink():
            raise ValueError(f"源码准备期间出现符号链接：{path}")
        if path.is_file():
            manifest.append([relative.as_posix(), stat.S_IMODE(path.stat().st_mode) & 0o777])
    return fingerprint_manifest(manifest)


def snapshot(source: Path, destination: Path, entries: list[Path], *, control=None) -> str:
    destination.mkdir()
    manifest = []
    for relative in entries:
        if control:
            control.check()
        target = destination / relative
        if (source / relative).is_dir():
            target.mkdir(parents=True, exist_ok=True)
            manifest.append([relative.as_posix(), "directory"])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
        manifest.append([relative.as_posix(), "file", hashlib.sha256(target.read_bytes()).hexdigest()])
    return fingerprint_manifest(manifest)


def fingerprint_manifest(manifest):
    """Hash ordered path/type/content records; shared by snapshot and evidence checks."""
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _valid_worker_record(raw):
    if (not isinstance(raw, dict) or type(raw.get("collected")) is not int or raw["collected"] < 0
            or type(raw.get("exit_code")) is not int or not isinstance(raw.get("collection_errors"), list)
            or not isinstance(raw.get("events"), list)):
        return False
    for event in raw["events"]:
        if (not isinstance(event, dict) or event.get("phase") not in ("setup", "call", "teardown")
                or event.get("outcome") not in ("passed", "failed", "skipped")
                or not isinstance(event.get("test_id"), str) or not event["test_id"]):
            return False
        exception = event.get("exception")
        if exception is not None and (not isinstance(exception, dict)
                or not isinstance(exception.get("type"), str) or not exception["type"]
                or not isinstance(exception.get("message"), str)):
            return False
    return True


def _classify(raw: dict, code: int) -> tuple[str, str, dict | None, str | None]:
    if not _valid_worker_record(raw):
        return "inconclusive", "工作进程记录缺失或字段无效。", None, None
    guard = raw.get("selection_guard")
    if isinstance(guard, dict) and guard.get("status") == "rejected":
        return "inconclusive", str(guard.get("reason", "执行选择记录拒绝了本次测试。")), None, None
    if code not in (0, 1) or raw.get("exit_code") != code or raw.get("collection_errors"):
        return "inconclusive", "测试收集或执行环境出现错误。", None, None
    if raw.get("collected") != 1:
        return "inconclusive", "首版要求恰好一个测试；未收集到测试或收集到多个。", None, None
    events = raw.get("events", [])
    if raw.get("has_xfail") or any(e.get("xfail") or e.get("outcome") == "skipped" for e in events):
        return "inconclusive", "测试被跳过或标记为预期失败，不能用来验收。", None, None
    if any(e.get("phase") != "call" and e.get("outcome") != "passed" for e in events):
        return "inconclusive", "测试准备或清理阶段失败，不能认定复现了目标问题。", None, None
    if len(events) != 3 or [e.get("phase") for e in events] != ["setup", "call", "teardown"]:
        return "inconclusive", "测试事件不完整。", None, None
    call = events[1]
    if any(event["test_id"] != call["test_id"] for event in events):
        return "inconclusive", "准备、执行、清理阶段的测试标识不一致。", None, None
    if guard is not None and (not isinstance(guard, dict) or guard.get("status") != "allowed"
            or type(guard.get("collected")) is not int or guard["collected"] != 1
            or guard.get("test_ids") != [call["test_id"]]):
        return "inconclusive", "实际执行与选中的测试记录不一致。", None, None
    if call["outcome"] == "passed" and code == 0:
        return "passed", "测试实际执行并通过。", None, call["test_id"]
    if call["outcome"] == "failed" and code == 1 and call.get("exception"):
        return "failed", "测试主体实际执行并失败。", call["exception"], call["test_id"]
    return "inconclusive", "测试返回了无法验收的状态。", None, None


def run_snapshot(source: Path, test: Path, run_dir: Path, label: str, timeout: float, *, test_target=None, python_executable=None, control=None, pytest_profile=None, execution_environment=None) -> dict:
    started = time.monotonic()
    log_path = run_dir / f"{label}.log"
    event_path = run_dir / f"{label}.events.json"
    timed_out = False
    # Only baseline operational variables and explicit project declarations are forwarded.
    # Local values stay in the subprocess environment, outside serialized options.
    with tempfile.TemporaryDirectory(prefix="reprolab-") as temp:
        workspace = Path(temp)
        copied = workspace / "source"
        shutil.copytree(source, copied)
        control_dir = workspace / "control"
        control_dir.mkdir()
        copied_test = control_dir / "regression.py"
        shutil.copyfile(test, copied_test)
        if test_target:
            overlay = copied / parse_test_target(test_target)
            overlay.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(test, overlay)
        raw_path = control_dir / "events.json"
        profile = (pytest_profile if pytest_profile is not None else read_pytest_profile(copied)) if test_target else None
        child_env, variables = execution_environment or resolve_variables(profile, getattr(control, "runtime_values", None))
        child_env = snapshot_process_environment(child_env, copied, python_executable or sys.executable)
        options = {"test_target": test_target, "pytest_profile": profile, "runtime_environment": variables,
                   "pytest_args": profile_args(profile) if profile else []}
        command = worker_command(python_executable or sys.executable, [str(copied), str(copied_test), str(raw_path), json.dumps(options)])
        with log_path.open("wb") as log:
            process = spawn(command, stdout=log, stderr=subprocess.STDOUT, env=child_env,
                                       cwd=copied, start_new_session=(os.name == "posix"))
            try:
                deadline = time.monotonic() + timeout
                while True:
                    if control:
                        control.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
                if control:
                    control.check()
            finally:
                # A completed test can leave helper processes alive. Own the entire
                # worker group for the full lifecycle, including normal exit/Ctrl-C.
                code = terminate_tree(process)
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        record_complete = _valid_worker_record(raw) and raw.get("runtime_environment") == variables
        event_bytes = json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write(event_path, event_bytes)
        if not isinstance(raw.get("events", []), list):
            raw["events"] = []
    if timed_out:
        kind, reason, failure, test_id = "inconclusive", f"超过 {timeout:g} 秒，已终止本次执行。", None, None
    elif not record_complete:
        kind, reason, failure, test_id = "inconclusive", "工作进程没有返回完整记录，无法确定测试是否完成。", None, None
    else:
        kind, reason, failure, test_id = _classify(raw, code)
    with log_path.open("rb") as log:
        log_sha256 = hashlib.file_digest(log, "sha256").hexdigest()
        length = log.seek(0, 2)
        log.seek(max(0, length - 12000))
        excerpt = log.read().decode("utf-8", errors="replace")
    return {"kind": kind, "reason": reason, "failure": failure, "test_id": test_id,
            "exit_code": code, "timed_out": timed_out, "duration_seconds": round(time.monotonic() - started, 3),
            "collected": raw.get("collected", 0), "log": log_path.name, "events": event_path.name,
            "log_excerpt": excerpt, "pytest_version": raw.get("pytest_version"),
            "python_version": raw.get("python_version"), "python_executable": raw.get("python_executable"),
            "worker_record_complete": record_complete,
            "events_sha256": hashlib.sha256(event_bytes).hexdigest(), "log_sha256": log_sha256,
            "selection_guard": raw.get("selection_guard"), "pytest_profile": raw.get("pytest_profile"),
            "runtime_environment": raw.get("runtime_environment"),
            "diagnostic_exceptions": raw.get("diagnostic_exceptions", []),
            "phase_events": [{key: event.get(key) for key in ("phase", "outcome", "exception", "xfail")}
                             for event in raw.get("events", []) if isinstance(event, dict)],
            "collection_errors": raw.get("collection_errors", []), "has_xfail": raw.get("has_xfail", False),
            "observations": next((event.get("observations", []) for event in raw.get("events", [])
                                  if isinstance(event, dict) and event.get("phase") == "call"), [])}


def evaluate(before: dict, after: dict, expected: dict) -> tuple[str, str]:
    if before["kind"] == "inconclusive" or after["kind"] == "inconclusive":
        return "inconclusive", "存在环境、测试或执行问题，证据不足，无法验收。"
    if before["kind"] == "passed":
        return "not_reproduced", "旧代码也通过了这份测试；本次未复现声明的问题。"
    failure = before["failure"]
    if failure["type"] != expected["type"] or expected["message_contains"] not in failure["message"]:
        return "inconclusive", "旧代码确实失败，但异常类型或消息与预先声明的问题不一致。"
    if before["test_id"] != after["test_id"]:
        return "inconclusive", "两次运行的测试标识不一致，不能比较。"
    if after["kind"] == "passed":
        return "verified", "旧代码出现预声明的故障，新代码通过同一份测试；本案例的修复验证通过。"
    return "still_failing", "旧代码复现了声明的故障，但新代码仍未通过同一份测试。"


def evaluate_regression(before, after):
    if before["kind"] == "inconclusive" or after["kind"] == "inconclusive":
        return "inconclusive", "存在收集、环境或执行问题，回归检查尚未完成。"
    if before.get("test_id") != after.get("test_id"):
        return "inconclusive", "两版测试标识不一致，无法比较回归表现。"
    outcomes = {
        ("passed", "passed"): ("regression_passed", "旧版和新版均通过这项回归检查。"),
        ("passed", "failed"): ("regression_detected", "旧版通过，新版失败，本次修改引入了这项失败。"),
        ("failed", "failed"): ("baseline_failure", "旧版已有失败，新版仍失败，请比较两版异常。"),
        ("failed", "passed"): ("improved", "旧版失败，新版通过，这项检查的表现得到改善。"),
    }
    return outcomes[(before["kind"], after["kind"])]


def run_case(case_path: Path, output_root: Path, timeout: float = 15, *, python_executable=None, control=None, previous_environment=None) -> dict:
    journal = RunJournal()
    try:
        return _run_case(case_path, output_root, timeout, python_executable=python_executable,
                         control=control, previous_environment=previous_environment, journal=journal)
    except Exception as exc:
        if journal.directory is None:
            raise
        cancelled = isinstance(exc, RunCancelled)
        try:
            journal.record(status="cancelled" if cancelled else "incomplete", error=str(exc))
            journal_error = ""
        except OSError as save_error:
            journal_error = f"；过程记录也未能保存：{save_error}"
        if cancelled:
            raise
        finished = "、".join(journal.completed) or "无"
        raise ValueError(f"未完成验收（阶段：{journal.phase}；已返回运行记录：{finished}）。"
                         f"{exc}；已保留材料：{journal.directory}{journal_error}") from exc


def _run_case(case_path: Path, output_root: Path, timeout: float = 15, *, python_executable=None, control=None, previous_environment=None, journal) -> dict:
    from . import __version__
    control = control or RunControl()
    control.notify("preparing")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.1 <= timeout <= 300:
        raise ValueError("timeout 必须是 0.1–300 秒之间的有限数字。")
    case_path, output_root = Path(case_path).resolve(), Path(output_root).resolve()
    case_bytes = case_path.read_bytes()
    case = load_case(case_path)
    execution_environment = resolve_variables(read_pytest_profile(case.after) if case.test_target else None, control.runtime_values)
    test_bytes = case.test.read_bytes()
    runtime = choose_runtime(python_executable or case.python,
                             base=Path.cwd() if python_executable is not None else case_path.parent)
    environment = snapshot_environment(runtime["executable"])
    control.check()
    for source in (case.before, case.after):
        if output_root.is_relative_to(source) or source.is_relative_to(output_root):
            raise ValueError("输出目录不能与源码目录重叠。")
    if case.test.is_relative_to(output_root) or case_path.is_relative_to(output_root):
        raise ValueError("输出目录不能包含原始案例或测试文件。")
    inputs = {label: source_fingerprint(source) for label, source in (("before", case.before), ("after", case.after))}
    input_modes = {label: source_mode_fingerprint(source, inputs[label][0])
                   for label, source in (("before", case.before), ("after", case.after))}
    if case.workspace_origin:
        origin = case.workspace_origin
        if (any(inputs[label][1] != origin[label + "_sha256"] for label in ("before", "after"))
                or ("source_mode_sha256" in origin and input_modes != origin["source_mode_sha256"])
                or hashlib.sha256(test_bytes).hexdigest() != origin["test_sha256"]):
            raise ValueError("已保存的工作区快照发生变化；请从当前项目重新创建案例，原报告保留。")
    if case.failure_origin:
        origin = case.failure_origin
        if (any(inputs[label][1] != origin["source_sha256"][label] for label in ("before", "after"))
                or ("source_mode_sha256" in origin and input_modes != origin["source_mode_sha256"])
                or hashlib.sha256(test_bytes).hexdigest() != origin["test_sha256"]):
            raise ValueError("已确认的试运行输入发生变化，请重新试运行并确认。")
    if case.project_failure_origin:
        origin = case.project_failure_origin
        if (any(inputs[label][1] != origin["source_sha256"][label] for label in ("before", "after"))
                or input_modes != origin["source_mode_sha256"]
                or hashlib.sha256(test_bytes).hexdigest() != origin["test_sha256"]):
            raise ValueError("项目报告修复案例的固定输入已变化，请重新准备；没有开始测试。")
    if case.frozen_inputs:
        if (any(inputs[label][1] != case.frozen_inputs["source_sha256"][label] for label in ("before", "after"))
                or ("source_mode_sha256" in case.frozen_inputs and input_modes != case.frozen_inputs["source_mode_sha256"])
                or hashlib.sha256(test_bytes).hexdigest() != case.frozen_inputs["test_sha256"]):
            raise ValueError("计划中的固定源码或测试发生变化，请重新准备并审阅。")
    if case.replay_inputs:
        if (any(inputs[label][1] != case.replay_inputs["source_sha256"][label] for label in ("before", "after"))
                or input_modes != case.replay_inputs["source_mode_sha256"]
                or hashlib.sha256(test_bytes).hexdigest() != case.replay_inputs["test_sha256"]):
            raise ValueError("回放源码、文件权限或测试与保存的证据不一致；请检查证据。")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    journal.directory = run_dir
    journal.record("preparing")
    evidence = run_dir / "evidence"
    evidence.mkdir()
    hashes = {label: snapshot(source, evidence / label, inputs[label][0])
              for label, source in (("before", case.before), ("after", case.after))}
    test_copy = evidence / "regression.py"
    test_copy.write_bytes(test_bytes)
    # Compare the saved tree as well as the inputs after preparation. A changing
    # source must never silently become a mixed snapshot accepted as the input.
    for label, source in (("before", case.before), ("after", case.after)):
        if (hashes[label] != inputs[label][1] or source_fingerprint(source)[1] != inputs[label][1]
                or source_mode_fingerprint(evidence / label) != input_modes[label]
                or source_mode_fingerprint(source) != input_modes[label]):
            raise ValueError(f"{label} 源码在准备期间发生变化；未开始测试。请停止修改后重试。材料：{run_dir}")
    if case_path.read_bytes() != case_bytes or case.test.read_bytes() != test_bytes:
        raise ValueError(f"案例或测试在准备期间发生变化；未开始测试。请停止修改后重试。材料：{run_dir}")
    expected = {"type": case.failure_type, "message_contains": case.failure_message} if case.check_mode == "repair" else None
    replay_inputs = {"source_sha256": hashes, "source_mode_sha256": input_modes,
                     "test_sha256": hashlib.sha256(test_bytes).hexdigest()}
    replay_case = {"source": "snapshot", "replay_contract_version": 1,
                   "title": case.title, "description": case.description, "before": "before", "after": "after",
                   "test": "regression.py", "demo": case.demo, "expected_failure": expected,
                   "check_mode": case.check_mode, "frozen_inputs": case.frozen_inputs, "replay_inputs": replay_inputs,
                   "provenance": case.provenance, "git_revision": case.git_revision, "workspace_origin": case.workspace_origin, "failure_origin": case.failure_origin,
                   "project_failure_origin": case.project_failure_origin,
                   "test_target": case.test_target, "python": runtime["executable"], "draft_origin": case.draft_origin, "draft_review": case.draft_review}
    replay_bytes = json.dumps(replay_case, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write(evidence / "case.json", replay_bytes)
    profile = read_pytest_profile(evidence / "after") if case.test_target else None
    journal.record("before")
    control.notify("before")
    before = run_snapshot(evidence / "before", test_copy, run_dir, "before", timeout,
                          test_target=case.test_target, python_executable=runtime["executable"], control=control, pytest_profile=profile, execution_environment=execution_environment)
    journal.completed["before"] = before
    before["diagnosis"] = diagnose_run(before, label="before", expected=expected)
    control.notify("before_done", before)
    journal.record("after")
    control.notify("after")
    after = run_snapshot(evidence / "after", test_copy, run_dir, "after", timeout,
                         test_target=case.test_target, python_executable=runtime["executable"], control=control, pytest_profile=profile, execution_environment=execution_environment)
    journal.completed["after"] = after
    after["diagnosis"] = diagnose_run(after, label="after", expected=expected)
    control.notify("after_done", after)
    status, explanation = evaluate(before, after, expected) if case.check_mode == "repair" else evaluate_regression(before, after)
    result = {"schema_version": 4, "reprolab_version": __version__, "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
              "title": case.title, "description": case.description, "demo": case.demo,
              "status": status, "explanation": explanation, "before": before, "after": after,
              "expected_failure": expected, "source_sha256": hashes, "source_mode_sha256": input_modes,
              "check_mode": case.check_mode, "frozen_inputs": case.frozen_inputs, "replay_inputs": replay_inputs,
              "input_integrity_notice": legacy_mode_notice(case.workspace_origin, case.failure_origin, case.frozen_inputs),
              "test_sha256": hashlib.sha256(test_bytes).hexdigest(),
              "preparation": {"status": "stable", "case_sha256": hashlib.sha256(case_bytes).hexdigest(),
                              "evidence_case_sha256": hashlib.sha256(replay_bytes).hexdigest()},
              "python_version": runtime["python_version"], "timeout_seconds": timeout,
              "runtime": runtime, "test_target": case.test_target, "pytest_profile": profile,
              "runtime_environment": execution_environment[1],
              "environment": environment, "environment_reference_present": previous_environment is not None,
              "environment_comparison": compare_environments(previous_environment, environment),
              "report_dir": str(run_dir), "evidence": "evidence/case.json",
              "provenance": case.provenance,
              "git_revision": case.git_revision, "workspace_origin": case.workspace_origin, "failure_origin": case.failure_origin,
              "project_failure_origin": case.project_failure_origin,
              "draft_origin": case.draft_origin,
              "draft_review": case.draft_review,
              "source_changes": describe_changes(evidence / "before", evidence / "after"),
              "limitations": ["仅验证这一份测试，不能证明其他功能正确或确定根因。",
                              "本地可信代码执行；子进程不是安全沙箱。",
                              "验收阶段不生成测试、不修复代码或安装被测项目依赖；测试可能来自待审阅的 AI 草稿。"]}
    result["diagnosis"] = diagnose_result(result)
    before["diagnosis"] = result["diagnosis"]["before"]
    after["diagnosis"] = result["diagnosis"]["after"]
    journal.record("saving", status="awaiting_report")
    control.commit()
    publish_report(result)
    control.progress("complete", result)
    return result
