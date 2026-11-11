"""Local draft discovery and explicit verification; never contacts a model."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .drafts import _read_bytes, _read_json, _read_state, _save, draft_lock, saved_response
from .recipes import run_saved_case, repository_root
from .case import parse_test_target
from .git_source import _git, resolve_commit
from .report import write_report

VERDICTS = {"verified", "not_reproduced", "still_failing", "inconclusive"}


def _case_fingerprints(folder):
    if (folder / "cases").is_symlink():
        raise ValueError("草稿 cases 目录不能是符号链接。")
    raw = _read_bytes(folder / "cases/case.json")
    result = {"case_sha256": hashlib.sha256(raw).hexdigest(), "test_sha256": None}
    try:
        config = json.loads(raw)
        root = None
        if config.get("source") == "git":
            root = repository_root(folder / "cases" / config["repository"])
            result["before_commit"] = resolve_commit(root, config["before_ref"])
            result["after_commit"] = resolve_commit(root, config["after_ref"])
        if root is not None and config.get("test_target") and not config.get("test"):
            target = parse_test_target(config["test_target"])
            raw_test = _git(root, "cat-file", "blob", result["after_commit"] + ":" + target)
        else:
            raw_test = _read_bytes(folder / "cases" / config["test"])
        result["test_sha256"] = hashlib.sha256(raw_test).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # The verifier reports the configuration error; this is only a fingerprint.
    return result


def draft_status(directory):
    folder, state = _read_state(directory)
    can_recover, recovery_error = False, ""
    if state["status"] != "draft_generated":
        try:
            saved_response(folder, state)
            can_recover = True
        except (OSError, ValueError) as exc:
            recovery_error = str(exc)
    verification = None
    if (folder / "verification.json").exists() or (folder / "verification.json").is_symlink():
        try:
            previous = _read_json(folder / "verification.json")
            if previous.get("schema_version") != 1 or not isinstance(previous.get("status"), str) or previous["status"] not in VERDICTS:
                raise ValueError("无法识别保存的验收记录。")
            try:
                current = _case_fingerprints(folder)
            except (OSError, ValueError):
                current = None
            stale = current != previous.get("inputs") or (current is None and previous["status"] != "inconclusive")
            if previous.get("test_sha256") and current and current["test_sha256"] != previous["test_sha256"]:
                stale = True
            verification = {"status": "stale" if stale else previous["status"],
                            "last_verdict": previous["status"], "stale": stale,
                            "report": previous.get("report"), "error": previous.get("error", ""),
                            "recorded_at": previous.get("recorded_at"),
                            "notice": "测试或案例已改动，旧结论不代表当前文件。" if stale else "最近一次验收记录；未重新运行测试或检查依赖变化。"}
        except (OSError, ValueError) as exc:
            verification = {"status": "invalid", "stale": True, "report": None, "error": str(exc)}
    try:
        title = _read_bytes(folder / "issue.txt", 16 * 1024).decode("utf-8").strip().splitlines()[0][:100]
    except (OSError, ValueError, IndexError):
        title = folder.name
    if can_recover:
        next_action = "执行 draft recover 复用已保存响应，不调用模型。"
    elif state["status"] == "draft_generated":
        next_action = "审阅测试后执行 draft verify；旧报告只代表当时运行的文件。"
    elif (folder / "response.json").exists():
        next_action = "检查恢复说明；旧版本无记录响应可用 draft import 显式声明来源。"
    else:
        next_action = "查看 prompt.txt，配置模型后执行 draft generate。"
    origin = state.get("origin", {})
    model = origin.get("model") if isinstance(origin, dict) else None
    return {"draft_dir": str(folder), "title": title, "next_action": next_action, "readable": True, "generation_status": state["status"],
            "before_commit": state["before_commit"], "after_commit": state["after_commit"],
            "model": model, "recoverable": can_recover, "recovery_error": recovery_error,
            "case": str(folder / "cases/case.json") if state["status"] == "draft_generated" else None,
            "error": state.get("error", ""), "verification": verification,
            "notice": "生成状态与验收结果分别记录；查看状态不会运行代码或调用模型。"}


def list_drafts(directory):
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError("请选择草稿根目录。")
    entries = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() and not folder.is_symlink():
            continue
        if not folder.is_symlink() and not (folder / "draft.json").exists():
            continue
        try:
            entries.append(draft_status(folder))
        except (OSError, ValueError) as exc:
            entries.append({"draft_dir": str(folder), "readable": False,
                            "generation_status": "invalid", "recoverable": False,
                            "verification": None, "error": str(exc)})
    return {"directory": str(root), "count": len(entries), "drafts": entries,
            "notice": "只读取直接子目录的草稿状态；不执行代码、不联网，坏配置单独列出。"}


def verify_draft(directory, output_root, *, timeout=15, python_executable=None, control=None, previous_environment=None):
    with draft_lock(directory) as directory:
        folder, state = _read_state(directory)
        if state["status"] != "draft_generated":
            raise ValueError("草稿尚未生成；请先 generate、import 或 recover，再审阅测试并验收。")
        if (folder / "verification.json").is_symlink() or (folder / "cases").is_symlink():
            raise ValueError("验收记录或案例目录不能是符号链接。")
        inputs = None
        try:
            inputs = _case_fingerprints(folder)
            result = run_saved_case(folder / "cases/case.json", output_root,
                                    timeout=timeout, python_executable=python_executable, control=control, previous_environment=previous_environment)
            write_report(result)
        except (OSError, ValueError) as exc:
            result = {"status": "inconclusive", "error": str(exc), "explanation": str(exc)}
        report = str(Path(result["report_dir"]) / "report.html") if result.get("report_dir") else None
        _save(folder / "verification.json", {"schema_version": 1, "status": result["status"],
              "recorded_at": datetime.now(timezone.utc).isoformat(), "inputs": inputs,
              "test_sha256": result.get("test_sha256"), "report": report,
              "error": result.get("error", "")})
        return result
