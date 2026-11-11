"""Plan explicit retries from immutable, bounded saved batch evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


from .batch import ENTRY_STATUSES, SUCCESS_STATUSES

_STATUSES = ENTRY_STATUSES
_MODES = {"not_verified": _STATUSES - SUCCESS_STATUSES, "unfinished": {"cancelled", "pending"}}
_MAX_SUMMARY_BYTES = 2 * 1024 * 1024


def _text(value, name, limit=4096, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(char) < 32 and char not in "\n\t\r" for char in value)):
        raise ValueError(f"批次记录的 {name} 无效或过长。")
    return value


def _case_path(value):
    value = _text(value, "file")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(char in value for char in "\n\r\t"):
        raise ValueError("批次案例必须使用明确的绝对路径。")
    return str(path)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("批次 JSON 包含重复字段。")
        result[key] = value
    return result


def _read_source(summary_path):
    path = Path(summary_path).expanduser().absolute()
    if path.is_dir():
        path = path / "summary.json"
    if path.name != "summary.json" or path.is_symlink():
        raise ValueError("请选择批次文件夹或其 summary.json 文件，不能使用符号链接。")
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_SUMMARY_BYTES + 1)
        if len(raw) > _MAX_SUMMARY_BYTES:
            raise ValueError("批次汇总过大，不能安全规划重跑。")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"无法读取有效的批次汇总：{exc}") from exc
    if not isinstance(data, dict) or data.get("kind") not in ("batch", "suite") or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("仅支持 schema_version 1 的批次汇总。")
    batch_id = _text(data.get("batch_id"), "batch_id", 200)
    entries, total = data.get("entries"), data.get("total")
    if type(total) is not int or not 1 <= total <= 50 or not isinstance(entries, list) or len(entries) != total:
        raise ValueError("批次案例数量必须是 1–50，且与 entries 一致。")
    files, reports, validated = set(), set(), []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError("批次案例条目必须是对象。")
        file = _case_path(entry.get("file"))
        identity = str(Path(file).resolve())
        if identity in files:
            raise ValueError("批次记录有重复或指向同一文件的案例路径。")
        files.add(identity)
        title = _text(entry.get("title"), "title", 4000)
        status = entry.get("status")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("批次案例包含未知状态。")
        if entry.get("source") not in ("git", "snapshot", "unknown") or type(entry.get("readable")) is not bool:
            raise ValueError("批次案例来源或可读标记无效。")
        for field in ("test", "error", "explanation"):
            _text(entry.get(field), field, 4000, empty=True)
        report = entry.get("report")
        if report is not None:
            _text(report, "report")
            reference = Path(report)
            if (reference.is_absolute() or ".." in reference.parts or "\\" in report or ":" in report
                    or any(char in report for char in "\n\r\t") or reference.suffix != ".html"
                    or str(reference) in reports):
                raise ValueError("批次报告引用无效或重复。")
            reports.add(str(reference))
        validated.append({"index": index, "file": file, "title": title, "status": status})
    return path.resolve(), batch_id, hashlib.sha256(raw).hexdigest(), validated


def plan_retry(summary_path, mode="not_verified"):
    """Read and validate a saved batch without importing or running its cases."""
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError("重跑模式必须是 not_verified 或 unfinished。")
    path, batch_id, fingerprint, entries = _read_source(summary_path)
    selected = [entry for entry in entries if entry["status"] in _MODES[mode]]
    return {"source_summary": str(path), "source_batch_id": batch_id,
            "source_sha256": fingerprint, "mode": mode,
            "paths": [entry["file"] for entry in selected], "entries": selected,
            "notice": "只读取旧批次并保留原顺序；重跑会创建新批次，当前案例内容和环境可能已改变。"}


def _validated_origin(origin, paths):
    """Keep caller-provided lineage compact and consistent with this selection."""
    if not isinstance(origin, dict):
        raise ValueError("重跑来源必须是对象。")
    source = _case_path(origin.get("source_summary"))
    batch_id = _text(origin.get("source_batch_id"), "source_batch_id", 200)
    fingerprint = origin.get("source_sha256")
    mode, entries = origin.get("mode"), origin.get("entries")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None or not isinstance(mode, str) or mode not in _MODES:
        raise ValueError("重跑来源指纹或模式无效。")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50 or len(entries) != len(paths):
        raise ValueError("重跑来源选择数量无效。")
    clean, last = [], 0
    for entry, path in zip(entries, paths):
        if not isinstance(entry, dict):
            raise ValueError("重跑来源案例无效。")
        index = entry.get("index")
        file = _case_path(entry.get("file"))
        if type(index) is not int or not last < index <= 50 or file != str(path) or not isinstance(entry.get("status"), str) or entry["status"] not in _MODES[mode]:
            raise ValueError("重跑来源案例与所选路径不一致。")
        last = index
        clean.append({"index": index, "file": file,
                      "title": _text(entry.get("title"), "title", 4000), "status": entry["status"]})
    return {"source_summary": source, "source_batch_id": batch_id, "source_sha256": fingerprint,
            "mode": mode, "entries": clean}


def run_retry(plan, output_root, *, timeout=15, python_executable=None, control=None):
    """Revalidate the source and selection, then create a separate batch."""
    if not isinstance(plan, dict):
        raise ValueError("请先读取批次并生成重跑计划。")
    try:
        canonical = plan_retry(plan["source_summary"], plan["mode"])
    except (KeyError, TypeError) as exc:
        raise ValueError("重跑计划缺少有效的来源和模式。") from exc
    if any(plan.get(key) != canonical[key] for key in
           ("source_summary", "source_batch_id", "source_sha256", "mode", "paths", "entries")):
        raise ValueError("旧批次记录或重跑选择已改变，请重新生成计划。")
    if not canonical["paths"]:
        raise ValueError("当前模式没有需要重跑的案例。")
    if Path(output_root).resolve().is_relative_to(Path(canonical["source_summary"]).parent):
        raise ValueError("新批次必须保存在旧批次目录之外，保留原始证据。")
    from .batch import run_selected_cases
    origin = {key: deepcopy(canonical[key]) for key in
              ("source_summary", "source_batch_id", "source_sha256", "mode", "entries")}
    # Compare retries with the referenced batch's actual report environments.
    from .environment import read_report_environment
    source_path = Path(canonical["source_summary"])
    raw = source_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != canonical["source_sha256"]:
        raise ValueError("旧批次记录已改变，请重新生成计划。")
    source = json.loads(raw)
    references = {}
    for entry in source["entries"]:
        if entry["file"] in canonical["paths"] and entry.get("report"):
            report = source_path.parent / entry["report"]
            if report.resolve().is_relative_to(source_path.parent):
                references[entry["file"]] = read_report_environment(report.with_name("report.json"))
            else:
                references[entry["file"]] = {}
    return run_selected_cases(canonical["paths"], output_root, timeout=timeout,
                              python_executable=python_executable, control=control,
                              retry_origin=origin, previous_environments=references, suite_context=source.get("suite"))
