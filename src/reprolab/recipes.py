"""Create and run reusable Git cases; discover tests without importing projects."""
import ast
import json
import os
from pathlib import Path
import re
import tempfile

from .case import parse_test_target
from .git_source import _git, export_revision, resolve_commit, run_git_case
from .runner import run_case
from .runtime import choose_runtime


def repository_root(repository):
    return Path(os.fsdecode(_git(Path(repository).resolve(), "rev-parse", "--show-toplevel")).strip()).resolve()


def list_project_tests(repository, ref="HEAD"):
    root = repository_root(repository)
    sha = resolve_commit(root, ref)
    tests, notes = [], []
    with tempfile.TemporaryDirectory(prefix="reprolab-discover-") as temp:
        source = Path(temp) / "source"
        export_revision(root, sha, source)
        for path in sorted(source.rglob("*.py")):
            if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
                continue
            relative = path.relative_to(source).as_posix()
            try:
                tree = ast.parse(path.read_bytes(), filename=relative)
            except (SyntaxError, ValueError) as exc:
                notes.append(f"{relative}: 无法静态解析（{type(exc).__name__}）")
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                    tests.append(f"{relative}::{node.name}")
                elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    for method in node.body:
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test"):
                            tests.append(f"{relative}::{node.name}::{method.name}")
    return {"repository": root.name, "commit": sha, "tests": tests, "notes": notes,
            "notice": "静态列表不执行测试；参数化测试请指定具体 [参数标识]，最终须只收集一个测试。"}


def create_recipe(path, repository, before_ref, after_ref, test_target, *,
                  failure_type, failure_message, title=None, python_executable=None):
    path = Path(path).resolve()
    if path.exists() or path.is_symlink():
        raise ValueError(f"案例文件已经存在，未覆盖：{path}")
    if not isinstance(failure_type, str) or not re.fullmatch(r"[A-Za-z_]\w*", failure_type):
        raise ValueError("请输入有效的异常类型，例如 AssertionError 或 ValueError。")
    if not isinstance(failure_message, str) or not failure_message.strip():
        raise ValueError("预期故障消息不能为空。")
    target_file = parse_test_target(test_target)
    root = repository_root(repository)
    before, after = resolve_commit(root, before_ref), resolve_commit(root, after_ref)
    found = False
    for entry in _git(root, "ls-tree", "-r", "-z", after).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        if name == target_file.encode("utf-8"):
            mode, kind, _ = metadata.split()
            found = mode in (b"100644", b"100755") and kind == b"blob"
            break
    if not found:
        raise ValueError("修复后版本不存在选中的普通 Python 测试文件。")
    runtime = choose_runtime(python_executable)
    config = {
        "source": "git", "title": title or f"{root.name} 修复验收",
        "repository": os.path.relpath(root, path.parent),
        "before_ref": before, "after_ref": after,
        "selected_refs": {"before": before_ref, "after": after_ref},
        "test_target": test_target, "python": runtime["executable"],
        "expected_failure": {"type": failure_type, "message_contains": failure_message},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"case": str(path), "before_commit": before, "after_commit": after,
            "test_target": test_target, "runtime": runtime}


def run_saved_case(path, output_root, timeout=15, *, python_executable=None, control=None, previous_environment=None):
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"无法读取案例：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("案例必须是 JSON 对象。")
    if data.get("source") in (None, "snapshot"):
        return run_case(path, output_root, timeout=timeout, python_executable=python_executable, control=control, previous_environment=previous_environment)
    if data.get("source") != "git":
        raise ValueError("不支持的案例 source 类型。")
    required = ("repository", "before_ref", "after_ref")
    if any(not isinstance(data.get(k), str) or not data[k] for k in required):
        raise ValueError("Git 案例缺少仓库、版本或测试目标。")
    test, target = data.get("test"), data.get("test_target")
    if bool(test) == bool(target) or any(v is not None and (not isinstance(v, str) or not v) for v in (test, target)):
        raise ValueError("Git 案例必须选择独立 test 或项目 test_target 其中一种。")
    expected = data.get("expected_failure")
    if not isinstance(expected, dict):
        raise ValueError("Git 案例缺少预期故障条件。")
    chosen = python_executable
    if chosen is None and data.get("python") is not None:
        if not isinstance(data["python"], str):
            raise ValueError("python 必须是解释器路径。")
        chosen = os.path.abspath(path.parent / data["python"])
    return run_git_case(
        path.parent / data["repository"], data["before_ref"], data["after_ref"], path.parent / test if test else None, output_root,
        failure_type=expected.get("type"), failure_message=expected.get("message_contains"),
        title=data.get("title"), timeout=timeout, test_target=target,
        python_executable=chosen, control=control, previous_environment=previous_environment, draft_origin=data.get("draft_origin"), draft_review=data.get("draft_review"),
    )
