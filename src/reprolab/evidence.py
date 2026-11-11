"""Check saved evidence without importing projects or running tests."""
import hashlib
import json
from pathlib import Path
import stat

from .case import load_case, valid_source_mode_hashes, legacy_mode_notice
from .runner import fingerprint_manifest, source_entries, source_mode_fingerprint

MAX_EVIDENCE_BYTES = 250 * 1024 * 1024
MAX_EVIDENCE_ENTRIES = 12000
ROOT_FILES = ("report.json", "report.html", "before.log", "after.log", "before.events.json", "after.events.json")
FIXED_FILES = (*ROOT_FILES, "evidence/case.json", "evidence/regression.py")
VERDICTS = {"regression_passed", "regression_detected", "baseline_failure", "improved", "verified", "not_reproduced", "still_failing", "inconclusive"}


def file_digest(path, limit=MAX_EVIDENCE_BYTES):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(128 * 1024):
            size += len(block)
            if size > limit:
                raise ValueError("证据超过大小限制。")
            digest.update(block)
    return digest.hexdigest(), size


def _json(path):
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"证据 JSON 过大：{path.name}")
    try:
        data = json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise ValueError(f"证据 JSON 无法读取：{path.name}") from None
    if not isinstance(data, dict):
        raise ValueError(f"证据 JSON 必须是对象：{path.name}")
    return data


def inspect_report(directory):
    candidate = Path(directory)
    if candidate.is_symlink():
        raise ValueError("报告目录不能是符号链接。")
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError("报告目录不存在。")
    inventory = {}
    total = 0

    def add(name, directory=False):
        nonlocal total
        path = root / name
        mode = path.lstat().st_mode
        valid = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
        if not valid:
            raise ValueError(f"证据路径不是普通{'目录' if directory else '文件'}：{name}")
        if directory:
            inventory[name] = {"type": "directory", "mode": 0o755, "size": 0, "sha256": None}
        else:
            digest, size = file_digest(path, MAX_EVIDENCE_BYTES-total)
            total += size
            inventory[name] = {"type": "file", "mode": stat.S_IMODE(mode) & 0o777,
                               "size": size, "sha256": digest}
        if len(inventory) > MAX_EVIDENCE_ENTRIES:
            raise ValueError("证据超过 12000 个条目限制。")

    add("evidence", True)
    for name in FIXED_FILES:
        add(name)
    report = _json(root / "report.json")
    if (type(report.get("schema_version")) is not int or report["schema_version"] not in (3, 4)
            or not isinstance(report.get("status"), str) or report["status"] not in VERDICTS):
        raise ValueError("当前只支持 schema_version=3 或 4 的单案例报告；批次总览请分别导出各案例。")
    replay = _json(root / "evidence/case.json")
    if (replay.get("source") not in (None, "snapshot")
            or (report["schema_version"] == 4 and replay.get("replay_contract_version") != 1)
            or any(replay.get(k) != v for k, v in {"before": "before", "after": "after", "test": "regression.py"}.items())):
        raise ValueError("回放案例必须引用包内 before、after 和 regression.py，不能指向外部路径。")
    for label in ("before", "after"):
        add("evidence/" + label, True)
    load_case(root / "evidence/case.json")  # Validation only; no Python probe or execution.
    issues = []
    persistence = report.get("persistence")
    if persistence is not None:
        names = set(FIXED_FILES) - {"report.json"}
        digests = persistence.get("sha256") if isinstance(persistence, dict) else None
        if (not isinstance(persistence, dict) or persistence.get("status") != "complete"
                or not isinstance(digests, dict) or set(digests) != names):
            issues.append("报告缺少完整的保存校验记录。")
        else:
            for name in sorted(names):
                if digests[name] != inventory[name]["sha256"]:
                    issues.append(f"{name} 与保存时的内容指纹不一致。")
    test_hash = inventory["evidence/regression.py"]["sha256"]
    if test_hash != report.get("test_sha256"):
        issues.append("测试文件与报告的 test_sha256 不一致。")
    hashes = report.get("source_sha256")
    if not isinstance(hashes, dict):
        raise ValueError("报告缺少源码指纹。")
    mode_hashes = report.get("source_mode_sha256")
    if (report["schema_version"] == 4 or "source_mode_sha256" in report) and not valid_source_mode_hashes(mode_hashes):
        raise ValueError("报告中的源码文件权限指纹无效。")
    actual_modes = {}
    for label in ("before", "after"):
        source = root / "evidence" / label
        manifest = []
        for relative in source_entries(source):
            name = "evidence/" + label + "/" + relative.as_posix()
            directory = (source / relative).is_dir()
            add(name, directory)
            manifest.append([relative.as_posix(), "directory"] if directory else
                            [relative.as_posix(), "file", inventory[name]["sha256"]])
        if fingerprint_manifest(manifest) != hashes.get(label):
            issues.append(f"{label} 源码与报告的 source_sha256 不一致。")
        actual_modes[label] = source_mode_fingerprint(source)
        if mode_hashes is not None and actual_modes[label] != mode_hashes[label]:
            issues.append(f"{label} 源码文件权限与报告记录不一致。")
    defaults = {"description": "", "demo": False, "test_target": None, "provenance": {},
                "git_revision": {}, "draft_origin": {}, "draft_review": {}, "workspace_origin": {}, "failure_origin": {}, "project_failure_origin": {}, "check_mode": "repair", "frozen_inputs": {}}
    for field in ("title", "description", "demo", "expected_failure", "test_target", "provenance", "git_revision", "draft_origin", "draft_review", "workspace_origin", "failure_origin", "project_failure_origin", "check_mode", "frozen_inputs"):
        if replay.get(field, defaults.get(field)) != report.get(field, defaults.get(field)):
            issues.append(f"回放案例的 {field} 与报告不一致。")
    if report["schema_version"] == 4 or replay.get("replay_contract_version") == 1 or "replay_inputs" in report:
        expected_replay = {"source_sha256": hashes, "source_mode_sha256": mode_hashes, "test_sha256": test_hash}
        if replay.get("replay_inputs") != expected_replay or report.get("replay_inputs") != expected_replay:
            issues.append("回放案例固定输入与报告证据不一致。")
    origin = report.get("workspace_origin")
    if isinstance(origin, dict) and origin:
        if (any(origin.get(label + "_sha256") != hashes.get(label) for label in ("before", "after"))
                or origin.get("test_sha256") != test_hash):
            issues.append("工作区快照的固定指纹与报告证据不一致。")
    failure_origin = report.get("failure_origin")
    if isinstance(failure_origin, dict) and failure_origin:
        if failure_origin.get("source_sha256") != hashes or failure_origin.get("test_sha256") != test_hash:
            issues.append("故障试运行来源的固定指纹与报告证据不一致。")
    project_origin = report.get("project_failure_origin")
    if isinstance(project_origin, dict) and project_origin:
        if (project_origin.get("source_sha256") != hashes or project_origin.get("test_sha256") != test_hash
                or project_origin.get("source_mode_sha256") != actual_modes):
            issues.append("项目报告故障来源与本次固定输入证据不一致。")
    frozen = report.get("frozen_inputs")
    if isinstance(frozen, dict) and frozen:
        if frozen.get("source_sha256") != hashes or frozen.get("test_sha256") != test_hash:
            issues.append("计划的固定输入与报告证据不一致。")
    for origin in (report.get("workspace_origin"), failure_origin, frozen):
        if isinstance(origin, dict) and "source_mode_sha256" in origin:
            if not valid_source_mode_hashes(origin["source_mode_sha256"]) or origin["source_mode_sha256"] != actual_modes:
                issues.append("固定输入的源码文件权限与报告证据不一致。")
    if 'pytest_profile' in report:
        from .pytest_profile import read_pytest_profile
        expected_profile = read_pytest_profile(root / 'evidence/after') if report.get('test_target') else None
        from .runtime_environment import variable_record
        variables = variable_record(expected_profile)
        if 'runtime_environment' in report or variables['values'] or variables['local_names']:
            if report.get('runtime_environment') != variables:
                issues.append('运行变量记录与固定输入不一致。')
        if report['pytest_profile'] != expected_profile:
            issues.append('pytest 运行配置与固定输入不一致。')
        for label in ('before', 'after'):
            raw = _json(root / (label + '.events.json'))
            cached = report.get(label) or {}
            if 'runtime_environment' in report and (cached.get('runtime_environment') != raw.get('runtime_environment')
                    or cached.get('worker_record_complete') and raw.get('runtime_environment') != variables):
                issues.append(label + ' 的运行变量与工作进程记录不一致。')
            if cached.get('pytest_profile') != raw.get('pytest_profile'):
                issues.append(label + ' 的 pytest 运行配置与执行记录不一致。')
            if cached.get('worker_record_complete') and raw.get('pytest_profile') != expected_profile:
                issues.append(label + ' 未使用本次声明的 pytest 运行配置。')
    runtime = report.get("runtime")
    if isinstance(runtime, dict) and runtime.get("executable") and replay.get("python") != runtime["executable"]:
        issues.append("回放案例的 python 与报告记录不一致。")
    result = {"status": "inconsistent" if issues else "consistent", "recorded_verdict": report["status"],
              "report_dir": str(root), "run_id": report.get("run_id"), "issues": issues,
              "file_count": sum(item["type"] == "file" for item in inventory.values()), "total_bytes": total,
              "notice": (("核对保存时的源码、测试、案例、HTML、日志和事件内容；" if persistence is not None else
                          "旧报告仅核对测试/源码指纹和案例元数据；HTML、日志和事件文件只有存在性检查；")
                         + ("已核对源码普通文件权限；" if mode_hashes is not None else "旧报告未记录源码文件权限，无法核对；")
                         + legacy_mode_notice(report.get("workspace_origin"), failure_origin, frozen)
                         + "没有重新运行测试，也不证明来源真实。")}
    return result, inventory


def check_report(directory):
    try:
        return inspect_report(directory)[0]
    except (OSError, ValueError) as exc:
        return {"status": "inconsistent", "recorded_verdict": None,
                "report_dir": str(Path(directory).absolute()), "issues": [str(exc)],
                "notice": "证据检查未通过；没有运行测试。"}
