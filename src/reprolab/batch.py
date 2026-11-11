"""Read saved cases and run each once, retaining a usable batch summary."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import uuid

from .batch_report import write_batch_report
from .recipes import repository_root, run_saved_case
from .report import write_report
from .persistence import save_json
from .runner import RunCancelled, RunControl

SUCCESS_STATUSES = {"verified", "regression_passed", "improved"}
ENTRY_STATUSES = SUCCESS_STATUSES | {"regression_detected", "baseline_failure", "still_failing", "not_reproduced", "inconclusive", "pending", "cancelled"}


def _collect_case(path):
    """Read one config without resolving away a rejected final symlink."""
    entry = {"file": str(path), "title": path.stem, "source": "unknown",
             "test": "", "readable": False, "error": ""}
    data = None
    try:
        if path.is_symlink():
            raise ValueError("不自动运行符号链接指向的案例文件。")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("案例必须是 JSON 对象。")
        source = data.get("source") or "snapshot"
        if source not in ("git", "snapshot"):
            raise ValueError("不支持的案例 source 类型。")
        required = ("repository", "before_ref", "after_ref") if source == "git" else ("before", "after", "test")
        if any(not isinstance(data.get(key), str) or not data[key] for key in required):
            raise ValueError("案例缺少必要的项目、版本或测试字段。")
        if source == "git":
            test, target = data.get("test"), data.get("test_target")
            if bool(test) == bool(target) or any(v is not None and (not isinstance(v, str) or not v) for v in (test, target)):
                raise ValueError("Git 案例必须选择 test 或 test_target 其中一种。")
        if not isinstance(data.get("title"), str) or not data["title"].strip():
            raise ValueError("案例需要非空 title。")
        expected = data.get("expected_failure")
        mode = data.get("check_mode", "repair")
        if mode not in ("repair", "regression"):
            raise ValueError("检查用途无效。")
        if mode == "repair" and not isinstance(expected, dict):
            raise ValueError("案例缺少 expected_failure。")
        if mode == "regression" and (expected is not None or source != "snapshot"):
            raise ValueError("回归保护需要固定快照案例和空故障条件。")
        entry.update(title=data["title"], source=source,
                     test=data.get("test_target") or data.get("test", ""), readable=True, check_mode=mode)
    except (OSError, ValueError, UnicodeError) as exc:
        entry["error"] = str(exc)[:4000]
    return entry, data


def _collect(directory):
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError("请选择存放案例 JSON 的文件夹。")
    return directory, [_collect_case(path) for path in sorted(directory.glob("*.json"))
                       if not path.is_dir()]


def list_cases(directory):
    directory, collected = _collect(directory)
    return {"directory": str(directory), "count": len(collected),
            "cases": [entry for entry, _ in collected],
            "notice": "只读取顶层 JSON 配置；可读取不代表环境已准备好或测试已通过。"}


def _guard_output(collected, output):
    for entry, data in collected:
        if not entry["readable"]:
            continue
        base = Path(entry["file"]).parent
        if entry["source"] == "git":
            candidate = (base / data["repository"]).resolve()
            try:
                sources = [repository_root(candidate)]
            except ValueError:
                sources = [candidate]
        else:
            sources = [(base / data[key]).resolve() for key in ("before", "after")]
        if any(output.is_relative_to(source) for source in sources):
            raise ValueError("批次报告必须保存在所有被测源码目录之外。")


def _save(summary, started, *, interrupted=False, cancelled=False):
    counts = Counter(entry["status"] for entry in summary["entries"])
    summary["counts"] = {key: counts[key] for key in
                         ("verified", "regression_passed", "regression_detected", "baseline_failure", "improved", "still_failing", "not_reproduced", "inconclusive", "pending", "cancelled")}
    summary["passed"] = sum(counts[status] for status in SUCCESS_STATUSES)
    summary["completed"] = summary["total"] - counts["pending"] - counts["cancelled"]
    summary["duration_seconds"] = round(time.monotonic() - started, 2)
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    if cancelled:
        summary["status"], summary["exit_code"] = "cancelled", 130
    elif interrupted:
        summary["status"], summary["exit_code"] = "interrupted", 130
    elif summary["completed"] < summary["total"]:
        summary["status"], summary["exit_code"] = "running", None
    elif summary["passed"] == summary["total"]:
        has_repair = any(entry.get("check_mode", "repair") == "repair" for entry in summary["entries"])
        summary["status"], summary["exit_code"] = ("verified" if has_repair else "checks_passed"), 0
    else:
        summary["status"] = "attention"
        summary["exit_code"] = 2 if counts["inconclusive"] else 1
    folder = Path(summary["summary_dir"])
    write_batch_report(summary)
    save_json(folder / "summary.json", summary)


def run_batch(directory, output_root, *, timeout=15, python_executable=None, progress=None):
    """Run a directory in sorted order; retain the CLI progress callback."""
    directory, collected = _collect(directory)
    if not collected:
        raise ValueError("案例文件夹里没有顶层 JSON 案例。")
    return _run_collected(collected, str(directory), output_root, timeout=timeout,
                          python_executable=python_executable, progress=progress)


def run_selected_cases(paths, output_root, *, timeout=15, python_executable=None, control=None, retry_origin=None, previous_environments=None, suite_context=None):
    """Run up to 50 distinct selected configs, preserving their supplied order."""
    selected = list(dict.fromkeys(Path(path).absolute() for path in paths))
    if not selected:
        raise ValueError("请至少选择一个案例。")
    if len(selected) > 50:
        raise ValueError("一次最多运行 50 个案例，请分批选择。")
    if retry_origin is not None:
        from .retry import _validated_origin
        retry_origin = _validated_origin(retry_origin, selected)
    collected = [_collect_case(path) for path in selected]
    return _run_collected(collected, "手动选择的案例", output_root, timeout=timeout,
                          python_executable=python_executable, control=control,
                          selected=True, retry_origin=retry_origin, previous_environments=previous_environments, suite_context=suite_context)


def _run_collected(collected, case_directory, output_root, *, timeout,
                   python_executable=None, progress=None, control=None, selected=False, retry_origin=None, previous_environments=None, suite_context=None):
    if len(collected) > 50:
        raise ValueError("一次最多运行 50 个案例，请分到不同文件夹。")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.1 <= timeout <= 300:
        raise ValueError("timeout 必须是 0.1–300 秒之间的有限数字。")
    control = control or RunControl()
    output_root = Path(output_root).resolve()
    _guard_output(collected, output_root)
    batch_id = "batch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    folder = output_root / batch_id
    folder.mkdir(parents=True, exist_ok=False)
    summary = {
        "schema_version": 1, "kind": "batch", "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(), "case_directory": case_directory,
        "total": len(collected), "completed": 0, "summary_dir": str(folder),
        "entries": [{**entry, "status": "pending", "report": None,
                     "explanation": "等待运行。"} for entry, _ in collected],
    }
    if suite_context is not None:
        summary["kind"] = "suite"
        summary["suite"] = deepcopy(suite_context)
    if retry_origin is not None:
        summary["retry_origin"] = deepcopy(retry_origin)
    started = time.monotonic()
    _save(summary, started)
    active = None

    def emit(phase, payload):
        # Qt signals may be queued; never publish a dict we mutate later.
        control.progress(phase, deepcopy(payload))

    try:
        emit("batch_start", {"summary_dir": str(folder), "total": summary["total"]})
        for index, entry in enumerate(summary["entries"], 1):
            control.check()
            active = entry
            emit("batch_case_start", {"index": index, "total": summary["total"], "entry": entry})
            control.check()
            if not entry["readable"]:
                entry.update(status="inconclusive", explanation=entry["error"])
            else:
                def case_progress(phase, result, case_index=index):
                    emit("batch_progress", {"index": case_index, "total": summary["total"],
                                            "phase": phase, "result": result})
                child = control.child(progress=case_progress)
                try:
                    from .preflight import check_case
                    reference = (previous_environments or {}).get(entry["file"])
                    child.notify("preflight")
                    preflight = check_case(entry["file"], folder / "runs", timeout=timeout,
                                           python_executable=python_executable, previous_environment=reference, control=child)
                    control.check()
                    checks = [{key: str(check.get(key, ""))[:limit] for key, limit in
                               (("id", 100), ("status", 40), ("message", 500), ("suggestion", 500))}
                              for check in preflight.get("checks", [])[:20]]
                    entry["preflight"] = {"status": preflight["status"], "checks": checks,
                                           "tests_executed": False,
                                           "notice": str(preflight.get("notice", ""))[:1000]}
                    if preflight["status"] != "ready":
                        problems = [" ".join(filter(None, (check["message"], check["suggestion"])))
                                    for check in checks if check["status"] == "error"]
                        entry.update(status="inconclusive", explanation=("预检未通过；未运行测试。 " +
                                     "；".join(problems or [entry["preflight"]["notice"]]))[:4000])
                    else:
                        result = run_saved_case(entry["file"], folder / "runs", timeout=timeout,
                                                python_executable=python_executable, control=child, previous_environment=reference)
                        write_report(result)
                        diagnosis = result.get("diagnosis", {})
                        explanation = result["explanation"]
                        if result["status"] != "verified" and diagnosis:
                            explanation += " 旧版：" + diagnosis.get("before", {}).get("summary", "")
                            explanation += " 新版：" + diagnosis.get("after", {}).get("summary", "")
                        entry["diagnosis"] = {"code": diagnosis.get("code"), "next_steps": diagnosis.get("next_steps", [])[:3]}
                        comparison = result.get("environment_comparison") or {}
                        entry["environment_comparison"] = {key: comparison.get(key) for key in ("status", "notice")}
                        entry.update(
                            title=result["title"], status=result["status"],
                            explanation=explanation,
                            before=result["before"]["kind"], after=result["after"]["kind"],
                            report=(Path(result["report_dir"]) / "report.html").relative_to(folder).as_posix(),
                        )
                except RunCancelled:
                    raise
                except Exception as exc:
                    # A case error is independent of the remaining selections.
                    entry.update(status="inconclusive",
                                 explanation=f"{type(exc).__name__}: {exc}"[:4000])
            active = None
            _save(summary, started)
            emit("batch_case_done", {"index": index, "total": summary["total"], "entry": entry})
            if progress is not None:
                progress(index, summary["total"], deepcopy(entry))
        # Child commits do not commit the parent. Cancellation stays available
        # through the last case notification, with one final atomic boundary.
        control.commit()
        _save(summary, started)
    except (RunCancelled, KeyboardInterrupt) as exc:
        if active is not None and active["status"] == "pending":
            active.update(status="cancelled", explanation="已取消；本案例没有产生验收结论。")
        _save(summary, started, cancelled=selected or isinstance(exc, RunCancelled),
              interrupted=not selected and isinstance(exc, KeyboardInterrupt))
    except Exception:
        # Preserve the latest entries if a consumer callback or saving fails.
        _save(summary, started)
        raise
    emit("batch_done", summary)
    return summary
