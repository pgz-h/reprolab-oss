"""Desktop persistence and read-only catalogs. No GUI and no model calls."""
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
from pathlib import Path

from .platform_support import application_directories
from .ai import load_api_config
from .model_reasoning import reasoning_description
from .batch import list_cases, ENTRY_STATUSES, SUCCESS_STATUSES
from .drafts import _save, _read_bytes
from .draft_management import list_drafts
from .git_source import resolve_commit
from .runner import source_entries, fingerprint_manifest, source_mode_fingerprint

LABELS = {
    "checks_passed": "所选回归检查通过",
    "regression_passed": "回归通过", "regression_detected": "新增失败",
    "baseline_failure": "两版均失败", "improved": "已改善",
    "pending": "待验收", "running": "运行中", "verified": "修复验证通过",
    "not_reproduced": "未复现预期故障", "still_failing": "修复后仍失败",
    "inconclusive": "无法验收", "cancelled": "已取消", "interrupted": "上次运行未完成",
    "stale": "历史结果 · 输入已变化", "error": "操作失败", "invalid": "记录无法读取",
    "prepared": "待生成", "generation_failed": "生成未完成", "draft_generated": "草稿已生成",
    "ready": "环境就绪 · 尚未验收", "attention": "环境需处理", "consistent": "证据内容一致",
}


def default_settings():
    work, output = application_directories()
    return {
        "cases": str(work / "cases"), "drafts": str(work / "drafts"),
        "reports": str(output / "reports"), "imports": str(output / "imported"),
        "python": "", "timeout": 15.0,
        "model_config": str(work / "ai.local.json") if (work / "ai.local.json").is_file() else "",
        "extra_cases": [], "last_case": "", "last_project": "", "last_project_title": "",
        "project_environment_groups": {}, "project_report_views": {}, "project_report_locations": {},
        "project_defaults": {},
    }


def default_state_directory():
    directory = application_directories()[0] / 'desktop'
    if sys.platform == 'darwin' and not (directory / 'settings.json').exists():
        legacy = Path.home() / '开源项目研究/work/reprolab/desktop'
        if (legacy / 'settings.json').is_file():
            return legacy
    return directory


def read_json(path, limit=4 * 1024 * 1024):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError("记录过大，未载入。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("记录必须是 JSON 对象。")
    return data


def input_stamp(path):
    path = Path(path).resolve()
    data = read_json(path)
    records = [["case", hashlib.sha256(path.read_bytes()).hexdigest()]]
    if data.get("source") == "git":
        root = path.parent / data["repository"]
        for key in ("before_ref", "after_ref"):
            records.append([key, resolve_commit(root, data[key])])
        if data.get("test"):
            records.append(["test", hashlib.sha256((path.parent / data["test"]).read_bytes()).hexdigest()])
    else:
        records.append(["test", hashlib.sha256((path.parent / data["test"]).read_bytes()).hexdigest()])
        for label in ("before", "after"):
            source = (path.parent / data[label]).resolve()
            if not source.is_dir():
                raise ValueError("源码目录不存在。")
            manifest = []
            for relative in source_entries(source):
                item = source / relative
                manifest.append([str(relative), "directory"] if item.is_dir() else
                                [str(relative), "file", hashlib.sha256(item.read_bytes()).hexdigest()])
            records.append([label, fingerprint_manifest(manifest)])
            records.append([label + "_mode", source_mode_fingerprint(source)])
    return hashlib.sha256(json.dumps(records, ensure_ascii=False).encode()).hexdigest()


class DesktopStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or default_state_directory())
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings = default_settings()
        self.activity = {}
        self.runtime_values = {}  # Memory only; never included in settings or activity files.
        self.case_runtime_sources = {}
        self.case_runtime_values = {}
        self.warning = ""
        if (self.directory / "settings.json").exists():
            try:
                saved = read_json(self.directory / "settings.json")
                for key, default in self.settings.items():
                    value = saved.get(key, default)
                    if isinstance(default, str) and not isinstance(value, str):
                        raise ValueError()
                    if key == "timeout" and (type(value) not in (int, float) or not .1 <= value <= 300):
                        raise ValueError()
                    if key == "extra_cases" and (not isinstance(value, list) or any(not isinstance(p, str) for p in value)):
                        raise ValueError()
                    if isinstance(default, dict):
                        if (not isinstance(value, dict) or len(value) > 200
                                or any(not isinstance(name, str) or not name or len(name) > 8192 for name in value)):
                            raise ValueError()
                        if key == 'project_environment_groups' and any(
                                not isinstance(groups, list) or len(groups) > 128
                                or any(not isinstance(group, str) or len(group) > 140 for group in groups)
                                for groups in value.values()):
                            raise ValueError()
                        if key == 'project_defaults' and any(
                                not isinstance(options, dict) or set(options) != {'python', 'timeout'}
                                or not isinstance(options.get('python'), str) or len(options['python']) > 8192
                                or type(options.get('timeout')) not in (int, float) or not .1 <= options['timeout'] <= 3600
                                for options in value.values()):
                            raise ValueError()
                        if key == 'project_report_views' and any(not isinstance(reference, dict) for reference in value.values()):
                            raise ValueError()
                        if key == 'project_report_locations' and any(not isinstance(path, str) or not path or len(path) > 8192 for path in value.values()):
                            raise ValueError()
                    self.settings[key] = value
            except (OSError, ValueError, TypeError):
                raise ValueError(f"桌面设置无法读取，请检查 {self.directory / 'settings.json'}；原文件未覆盖。") from None
        if (self.directory / "activity.json").exists():
            try:
                self.activity = read_json(self.directory / "activity.json")
                if any(not isinstance(v, dict) for v in self.activity.values()):
                    raise ValueError()
            except (OSError, ValueError):
                self.activity = {}
                self.warning = "活动记录无法读取；历史报告仍保留在报告目录。"
        for record in self.activity.values():
            if record.get("status") == "running":
                record["status"] = "interrupted"

    def local_runtime_values(self, repository):
        if not repository:
            return {}
        try:
            key = str(Path(repository).expanduser().resolve())
        except (OSError, ValueError):
            return {}
        return dict(self.runtime_values.get(key, {}))

    def set_runtime_values(self, repository, values):
        key = str(Path(repository).expanduser().resolve())
        self.runtime_values[key] = dict(values)

    def project_defaults(self, repository):
        if not str(repository).strip():
            return None
        try:
            key = str(Path(repository).expanduser().resolve())
        except (OSError, ValueError):
            # Text input may be incomplete; actual preparation validates the path.
            return None
        value = self.settings['project_defaults'].get(key)
        return dict(value) if value is not None else None

    def remember_project_defaults(self, repository, python, timeout):
        key = str(Path(repository).expanduser().resolve())
        options = {'python': str(python), 'timeout': float(timeout)}
        if self.settings['project_defaults'].get(key) == options:
            return
        values = dict(self.settings['project_defaults'])
        values.pop(key, None)
        values[key] = options
        self.settings['project_defaults'] = dict(list(values.items())[-200:])
        self.save()

    def save(self):
        _save(self.directory / "settings.json", self.settings)
        _save(self.directory / "activity.json", self.activity)

    def record(self, key, status, **fields):
        previous = self.activity.get(str(key), {})
        if not fields.get("report"):
            fields.setdefault("reference_report", previous.get("report") or previous.get("reference_report"))
        self.activity[str(key)] = {"status": status, "at": datetime.now(timezone.utc).isoformat(), **fields}
        self.save()

    def case_status(self, path):
        record = self.activity.get(str(path), {})
        status = record.get("status", "pending")
        if status in ENTRY_STATUSES - {"pending", "cancelled"}:
            try:
                if not record.get("stamp") or input_stamp(path) != record["stamp"]:
                    return "stale"
            except (OSError, ValueError, KeyError, TypeError):
                return "stale"
        return status

    def catalog(self):
        entries, warnings = [], []
        folder = Path(self.settings["cases"]).expanduser()
        if folder.is_dir():
            entries = list_cases(folder)["cases"]
        else:
            warnings.append("尚未添加项目案例；内置示例已就绪，可从“开始使用”体验。")
        builtins = Path(__file__).parent
        paths = [builtins / "demo_case/case.json", builtins / "history_cases/humanize250/case.json"]
        paths.extend(Path(p) for p in self.settings["extra_cases"])
        known = {item["file"] for item in entries}
        for path in paths:
            path = path.resolve()
            if str(path) in known:
                continue
            known.add(str(path))
            try:
                data = read_json(path)
                entries.append({"file": str(path), "title": data.get("title", path.stem),
                                "source": data.get("source", "snapshot"), "readable": True,
                                "test": data.get("test_target") or data.get("test", ""), "error": ""})
            except (OSError, ValueError) as exc:
                entries.append({"file": str(path), "title": path.stem, "source": "unknown",
                                "readable": False, "test": "", "error": str(exc)})
        for entry in entries:
            entry["status"] = self.case_status(entry["file"]) if entry["readable"] else "invalid"
        drafts = []
        draft_root = Path(self.settings["drafts"]).expanduser()
        if draft_root.is_dir():
            drafts = list_drafts(draft_root)["drafts"]
        reports = []
        invalid_batches = 0
        incomplete_runs = 0
        root = Path(self.settings["reports"]).expanduser()
        # A bounded scan supports existing release-test subfolders without following symlinks.
        if root.is_dir():
            for current, dirs, files in os.walk(root, followlinks=False):
                relative = Path(current).relative_to(root)
                dirs[:] = [d for d in dirs if d not in {"evidence", "before", "after"} and not (Path(current) / d).is_symlink()]
                if len(relative.parts) >= 3:
                    dirs[:] = []
                if "summary.json" in files:
                    try:
                        summary = read_json(Path(current) / "summary.json")
                        if summary.get("kind") in ("batch", "suite"):
                            if summary.get("kind") == "suite" and (not isinstance(summary.get("suite"), dict)
                                    or not isinstance(summary["suite"].get("title"), str)):
                                raise ValueError("项目验收记录无效。")
                            batch_entries = summary.get("entries")
                            states = ENTRY_STATUSES
                            if (not isinstance(batch_entries, list) or type(summary.get("total")) is not int
                                    or not 1 <= summary["total"] <= 50 or len(batch_entries) != summary["total"]
                                    or any(not isinstance(e, dict) or not isinstance(e.get("status"), str)
                                           or e["status"] not in states for e in batch_entries)
                                    or not isinstance(summary.get("status"), str)
                                    or not isinstance(summary.get("updated_at"), str)):
                                raise ValueError("批次记录字段无效。")
                            reports.append({"directory": current, "title": ("项目验收 · " + summary.get("suite", {}).get("title", "") if summary.get("kind") == "suite" else f"批次 · {summary.get('total', 0)} 个案例"),
                                            "status": summary.get("status", "invalid"), "kind": summary.get("kind", "batch"),
                                            "retry_count": sum(e.get("status") not in SUCCESS_STATUSES for e in summary.get("entries", [])),
                                            "unfinished_count": sum(e.get("status") in {"pending", "cancelled"} for e in summary.get("entries", [])),
                                            "at": summary.get("updated_at", "")})
                    except (OSError, ValueError):
                        invalid_batches += 1
                if "operation.json" in files and "report.json" not in files:
                    incomplete_runs += 1
                if "report.json" in files:
                    try:
                        metadata = Path(current) / ('catalog.json' if 'catalog.json' in files else 'report.json')
                        result = read_json(metadata)
                        project = result.get('kind') == 'project_regression'
                        reports.append({"directory": current,
                                        "title": ('项目回归 · ' if project else '') + result.get("title", Path(current).name),
                                        "status": result.get("status", "invalid"),
                                        'kind': 'project_regression' if project else result.get('kind', 'report'),
                                        "at": result.get("created_at", result.get("generated_at", ""))})
                    except (OSError, ValueError):
                        reports.append({"directory": current, "title": Path(current).name, "status": "invalid", "at": ""})
                if len(reports) >= 500:
                    warnings.append("最多显示 500 份报告；可更换报告目录。")
                    break
        if incomplete_runs:
            warnings.append(f"发现 {incomplete_runs} 次未完成或取消的验收；过程材料保留在报告目录，未发布最终报告，不能视为验证通过。")
        if invalid_batches:
            warnings.append(f"跳过 {invalid_batches} 份损坏的批次记录，其他案例和报告仍可使用。")
        return {"cases": entries, "drafts": drafts, "reports": sorted(reports, key=lambda r: r["at"], reverse=True),
                "warnings": warnings}


def generation_preview(folder, config_file):
    prompt = _read_bytes(Path(folder) / "prompt.txt").decode("utf-8")
    settings = load_api_config(config_file)
    key = settings["api_key"]
    # Only this digest binds credentials; no key leaves the loader.
    binding = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    return {"prompt": prompt, "endpoint": settings["endpoint"].replace(key, "[已隐藏]"),
            "model": settings["model"].replace(key, "[已隐藏]"), "protocol": settings["protocol"],
            "model_definition": settings["model_definition"], "output_limit": settings["max_tokens"],
            "reasoning": settings["reasoning"], "reasoning_description": reasoning_description(settings["reasoning"]),
            "binding": binding, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}


def check_preview(folder, config_file, preview):
    if generation_preview(folder, config_file) != preview:
        raise ValueError("上下文或模型配置已经改变，请重新预览后再发送。")
