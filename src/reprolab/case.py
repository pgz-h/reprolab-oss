"""Validate a small, explicit contract for a trusted local regression case."""

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Case:
    title: str
    description: str
    before: Path
    after: Path
    test: Path
    failure_type: str | None
    failure_message: str | None
    demo: bool = False
    provenance: dict = field(default_factory=dict)
    git_revision: dict = field(default_factory=dict)
    test_target: str | None = None
    python: str | None = None
    draft_origin: dict = field(default_factory=dict)
    draft_review: dict = field(default_factory=dict)
    workspace_origin: dict = field(default_factory=dict)
    failure_origin: dict = field(default_factory=dict)
    check_mode: str = "repair"
    frozen_inputs: dict = field(default_factory=dict)
    replay_inputs: dict = field(default_factory=dict)
    project_failure_origin: dict = field(default_factory=dict)



def validate_draft_review(review):
    if not isinstance(review, dict):
        raise ValueError("draft_review 必须是对象。")
    if not review:
        return review
    if (set(review) != {"checks", "expected_behavior", "assumptions"}
            or any(not isinstance(review[k], str) or not review[k].strip() or len(review[k]) > 32000
                   for k in ("checks", "expected_behavior"))
            or not isinstance(review["assumptions"], list) or not 1 <= len(review["assumptions"]) <= 30
            or any(not isinstance(s, str) or not s.strip() or len(s) > 4000 for s in review["assumptions"])
            or len(json.dumps(review, ensure_ascii=False).encode("utf-8")) > 192 * 1024):
        raise ValueError("draft_review 的检查目标、预期行为和假设字段无效或过大。")
    return review


def valid_source_mode_hashes(value):
    return (isinstance(value, dict) and set(value) == {"before", "after"}
            and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in value.values()))


def legacy_mode_notice(*origins):
    if any(isinstance(origin, dict) and origin and "source_mode_sha256" not in origin for origin in origins):
        return "旧固定输入未记录冻结时的文件权限，无法核对当时的权限；当前权限可在新的验收运行中记录。"
    return ""


def parse_test_target(target):
    if not isinstance(target, str) or not target:
        raise ValueError("项目测试目标必须是相对路径，可追加 ::测试名称。")
    file_name = target.split("::", 1)[0]
    # Parameter IDs may contain escaped backslashes; only this prefix is a path.
    path = PurePosixPath(file_name)
    forbidden = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__"}
    if ("\\" in file_name or path.is_absolute() or ".." in path.parts or path.suffix != ".py"
            or any(p in forbidden or p.startswith(".env") for p in path.parts)
            or ":" in file_name or any(ord(c) < 32 for c in target)):
        raise ValueError("项目测试必须位于源码目录内，且不能使用排除目录。")
    return path.as_posix()


def validate_project_failure_origin(origin, target, expected):
    if not isinstance(origin, dict):
        raise ValueError("project_failure_origin 必须是对象。")
    if not origin:
        return origin
    from .project_history import validate_report_reference
    from .project_failure import repair_eligibility
    try:
        validate_report_reference(origin['source_report'])
        confirmation = origin['confirmation']
        observation = origin['observation']
        historical = origin['historical_execution']
        if (origin.get('schema_version') != 1 or origin.get('kind') != 'project_report_failure'
                or not isinstance(origin['preparation_id'], str) or not origin['preparation_id']
                or not re.fullmatch(r'[0-9a-f]{64}', origin['preparation_sha256'])
                or not valid_source_mode_hashes(origin['source_sha256'])
                or not valid_source_mode_hashes(origin['source_mode_sha256'])
                or not re.fullmatch(r'[0-9a-f]{64}', origin['test_sha256'])
                or origin['test_target'] != target or observation['test_id'] != target
                or origin['failure_snapshot']['side'] != 'after'
                or origin['failure_snapshot']['sha256'] != origin['source_sha256']['before']
                or origin['candidate']['sha256'] != origin['source_sha256']['after']
                or any(not isinstance(value, str) or not value for value in
                       (origin['failure_snapshot']['frozen_at'], origin['failure_snapshot']['observed_at'],
                        origin['candidate']['frozen_at'], origin['candidate']['repository']))
                or not re.fullmatch(r'[0-9a-f]{64}', origin['baseline_background']['sha256'])
                or origin['baseline_background']['status'] != observation['before']['status']
                or origin['test_change']['status'] not in ('same', 'changed', 'missing')
                or not isinstance(origin['test_change']['diff'], str)
                or (origin['test_change']['status'] == 'missing' and origin['test_change']['sha256'] is not None)
                or (origin['test_change']['status'] != 'missing'
                    and not re.fullmatch(r'[0-9a-f]{64}', origin['test_change']['sha256']))
                or not repair_eligibility(observation, historical)[0]
                or confirmation.get('confirmed') is not True
                or any(not isinstance(confirmation.get(k), str) or not confirmation[k].strip()
                       or len(confirmation[k].encode('utf-8')) > 8000 for k in ('problem', 'expected_behavior'))
                or expected != {k: confirmation[k] for k in ('type', 'message_contains')}
                or expected['type'] != observation['after']['exception']['type']
                or not expected['message_contains'].strip()
                or expected['message_contains'] not in observation['after']['exception']['message']
                or len(json.dumps(origin).encode('utf-8')) > 256 * 1024):
            raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError('项目报告故障来源、固定输入或用户确认条件不一致，请重新准备并确认。') from None
    return origin


def load_case(path: Path) -> Case:
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取案例 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("案例必须是 JSON 对象。")
    title = data.get("title")
    description = data.get("description", "")
    if not isinstance(title, str) or not title.strip() or not isinstance(description, str):
        raise ValueError("title 必须是非空字符串，description 必须是字符串。")
    check_mode = data.get("check_mode", "repair")
    if check_mode not in ("repair", "regression"):
        raise ValueError("check_mode 必须是 repair 或 regression。")
    expected = data.get("expected_failure")
    failure_type = message = None
    if check_mode == "repair":
        if not isinstance(expected, dict):
            raise ValueError("expected_failure 必须声明 type 和 message_contains。")
        failure_type, message = expected.get("type"), expected.get("message_contains")
        if not isinstance(failure_type, str) or not re.fullmatch(r"[A-Za-z_]\w*", failure_type):
            raise ValueError("expected_failure.type 必须是异常类名，例如 ValueError。")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("expected_failure.message_contains 不能为空。")
    elif expected is not None:
        raise ValueError("回归保护检查使用前后基线比较，请将 expected_failure 留空。")
    if not isinstance(data.get("demo", False), bool):
        raise ValueError("demo 必须是布尔值。")
    provenance = data.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError("provenance 必须是对象。")
    if provenance:
        required = {"project", "issue_url", "before_ref", "after_ref", "scope"}
        if set(provenance) != required or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in provenance.values()):
            raise ValueError("provenance 需要 project、issue_url、before_ref、after_ref、scope 五个非空文本字段。")
        url = provenance["issue_url"]
        try:
            parts = urlsplit(url)
            valid = parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
        except ValueError:
            valid = False
        if not valid or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError("来源链接必须是不含凭据或空白的有效 HTTPS URL。")

    revision = data.get("git_revision", {})
    if not isinstance(revision, dict):
        raise ValueError("git_revision 必须是对象。")
    if revision:
        required = {"repository", "before_ref", "after_ref", "before_commit", "after_commit"}
        if set(revision) != required or any(not isinstance(v, str) or not v for v in revision.values()):
            raise ValueError("Git 版本元数据不完整。")
        if any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision[k]) for k in ("before_commit", "after_commit")):
            raise ValueError("Git 提交编号不正确。")

    workspace = data.get("workspace_origin", {})
    if not isinstance(workspace, dict):
        raise ValueError("workspace_origin 必须是对象。")
    if workspace:
        workspace_fields = {"repository", "before_ref", "before_commit", "before_sha256", "after_sha256", "test_sha256", "file_count"}
        if (set(workspace) not in (workspace_fields, workspace_fields | {"source_mode_sha256"})
                or ("source_mode_sha256" in workspace and not valid_source_mode_hashes(workspace["source_mode_sha256"]))
                or any(not isinstance(workspace.get(k), str) or not workspace[k] or len(workspace[k]) > 2000
                       for k in ("repository", "before_ref", "before_commit", "before_sha256", "after_sha256", "test_sha256"))
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", workspace["before_commit"])
                or any(not re.fullmatch(r"[0-9a-f]{64}", workspace[k]) for k in ("before_sha256", "after_sha256", "test_sha256"))
                or type(workspace["file_count"]) is not int or not 0 <= workspace["file_count"] <= 5000):
            raise ValueError("工作区来源记录不完整或无效。")

    failure_origin = data.get("failure_origin", {})
    if not isinstance(failure_origin, dict):
        raise ValueError("failure_origin 必须是对象。")
    if failure_origin:
        required = {"probe_id", "probe_sha256", "source_sha256", "test_sha256", "test_target", "observed_failure", "confirmed"}
        hashes = failure_origin.get("source_sha256")
        observed = failure_origin.get("observed_failure")
        if (set(failure_origin) not in (required, required | {"source_mode_sha256"})
                or ("source_mode_sha256" in failure_origin and not valid_source_mode_hashes(failure_origin["source_mode_sha256"]))
                or failure_origin.get("confirmed") is not True
                or not isinstance(failure_origin.get("probe_id"), str) or len(failure_origin["probe_id"]) > 200
                or not isinstance(hashes, dict) or set(hashes) != {"before", "after"}
                or not isinstance(observed, dict) or set(observed) != {"type", "message"}
                or any(not isinstance(observed[k], str) for k in ("type", "message"))
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in [failure_origin.get("probe_sha256"), failure_origin.get("test_sha256"), *hashes.values()])
                or len(json.dumps(failure_origin)) > 32000):
            raise ValueError("故障试运行来源记录不完整。")
        if (not isinstance(failure_origin.get("test_target"), str)
                or data.get("test_target") != failure_origin["test_target"]
                or failure_type != observed["type"] or message not in observed["message"]):
            raise ValueError("案例条件与已确认的试运行目标或故障不一致，请重新试运行并确认。")

    project_failure = validate_project_failure_origin(data.get("project_failure_origin", {}),
                                                       data.get("test_target"), expected)

    frozen = data.get("frozen_inputs", {})
    if not isinstance(frozen, dict):
        raise ValueError("固定输入记录必须是对象。")
    if frozen:
        hashes = frozen.get("source_sha256")
        if (not isinstance(hashes, dict) or set(hashes) != {"before", "after"}
                or ("source_mode_sha256" in frozen and not valid_source_mode_hashes(frozen["source_mode_sha256"]))
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                       for v in [frozen.get("test_sha256"), *hashes.values()])
                or frozen.get("check_mode") != check_mode or frozen.get("expected_failure") != expected
                or frozen.get("test_target") != data.get("test_target")):
            raise ValueError("检查条件与已审阅的固定输入不一致，请重新准备计划。")

    replay = data.get("replay_inputs", {})
    replay_version = data.get("replay_contract_version")
    if (replay_version is not None and (type(replay_version) is not int or replay_version != 1)):
        raise ValueError("不支持的回放案例版本。")
    if (not isinstance(replay, dict) or (replay_version == 1 and not replay) or (replay and replay_version != 1)
            or (replay and (set(replay) != {"source_sha256", "source_mode_sha256", "test_sha256"}
                or not valid_source_mode_hashes(replay.get("source_sha256"))
                or not valid_source_mode_hashes(replay.get("source_mode_sha256"))
                or not isinstance(replay.get("test_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", replay["test_sha256"])))):
        raise ValueError("回放案例的固定输入记录不完整或无效。")

    def local_path(key: str, directory: bool) -> Path:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} 必须指定本地路径。")
        candidate = path.parent / value
        if candidate.is_symlink():
            raise ValueError(f"不支持符号链接：{candidate}")
        candidate = candidate.resolve()
        if not (candidate.is_dir() if directory else candidate.is_file()):
            raise ValueError(f"{key} 路径不存在或类型不正确：{candidate}")
        return candidate

    draft_origin = data.get("draft_origin", {})
    if (not isinstance(draft_origin, dict) or len(json.dumps(draft_origin)) > 8000
            or (draft_origin and any(not isinstance(draft_origin.get(key), str)
                                     for key in ("provider", "model", "test_sha256")))):
        raise ValueError("draft_origin 必须包含 provider、model、test_sha256，且不超过 8000 字符。")
    draft_review = validate_draft_review(data.get("draft_review", {}))
    target = data.get("test_target")
    if target is not None:
        parse_test_target(target)
    python = data.get("python")
    if python is not None and (not isinstance(python, str) or not python):
        raise ValueError("python 必须是解释器路径。")
    before, after, test = local_path("before", True), local_path("after", True), local_path("test", False)
    if test.suffix != ".py":
        raise ValueError("test 必须是 Python 文件，并包含一个 pytest 测试。")
    return Case(title, description, before, after, test, failure_type, message, data.get("demo", False), provenance, revision, target, python, draft_origin, draft_review, workspace, failure_origin, check_mode, frozen, replay, project_failure)
