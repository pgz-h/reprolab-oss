"""Read committed Git blobs without checkout or worktree changes."""
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from .runner import EXCLUDED, MAX_BYTES, MAX_ENTRIES, run_case
from .case import parse_test_target


def _git(repo, *args, data=None):
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0"),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"无法读取本地 Git 仓库：{exc}") from exc
    if result.returncode:
        raise ValueError("Git 操作失败：" + result.stderr.decode("utf-8", errors="replace").strip()[:2000])
    return result.stdout


def resolve_commit(repo, ref):
    if not ref or ref.startswith("-"):
        raise ValueError("Git 版本不能为空或以连字符开头。")
    sha = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("Git 没有返回有效的提交编号。")
    return sha


def export_revision(repo, sha, destination):
    # Raw blobs avoid git archive export-ignore silently removing tested code.
    entries, size_total = [], 0
    for record in _git(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", sha).split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, oid, raw_size = metadata.split()
        try:
            name = raw_name.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("Git 导入要求 UTF-8 文件路径。") from exc
        path = PurePosixPath(name)
        if path.is_absolute() or any(p in (".", "..") for p in path.parts):
            raise ValueError(f"不支持的 Git 文件路径：{name}")
        if any(p in EXCLUDED or p.startswith(".env") for p in path.parts):
            continue
        if mode == b"120000":
            raise ValueError(f"当前不支持 Git 源码中的符号链接：{name}")
        if mode == b"160000":
            raise ValueError(f"当前不支持 Git 子模块：{name}")
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError(f"不支持的 Git 条目：{name}")
        size = int(raw_size)
        size_total += size
        entries.append((name, oid, size, mode))
        if len(entries) > MAX_ENTRIES or size_total > MAX_BYTES:
            raise ValueError("Git 源码超过 5000 个文件或 100 MiB 的限制。")
    destination.mkdir()
    if not entries:
        return
    request = b"".join(oid + b"\n" for _, oid, _, _ in entries)
    stream = io.BytesIO(_git(repo, "cat-file", "--batch", data=request))
    for name, oid, size, mode in entries:
        if stream.readline().split() != [oid, b"blob", str(size).encode()]:
            raise ValueError("Git 对象读取结果与版本清单不一致。")
        content = stream.read(size)
        if len(content) != size or stream.read(1) != b"\n":
            raise ValueError("Git 文件内容不完整。")
        if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            raise ValueError(f"当前不自动下载 Git LFS 内容：{name}")
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if mode == b"100755" else 0o644)


def run_git_case(repository, before_ref, after_ref, test, output_root, *,
                 failure_type, failure_message, title=None, timeout=15,
                 test_target=None, python_executable=None, draft_origin=None, draft_review=None, control=None, previous_environment=None):
    if control:
        control.notify("preparing")
    root = Path(os.fsdecode(_git(Path(repository).resolve(), "rev-parse", "--show-toplevel")).strip()).resolve()
    output_root = Path(output_root).resolve()
    if bool(test) == bool(test_target):
        raise ValueError("请选择独立测试文件或项目测试目标其中一种。")
    if test_target:
        target_file = parse_test_target(test_target)
    else:
        test = Path(test).resolve()
    if output_root.is_relative_to(root):
        raise ValueError("报告输出目录必须放在被测 Git 仓库之外。")
    if test is not None and (not test.is_file() or test.suffix != ".py"):
        raise ValueError("请提供存在的 Python 回归测试文件。")
    before_sha, after_sha = resolve_commit(root, before_ref), resolve_commit(root, after_ref)
    revision = {"repository": root.name, "before_ref": before_ref, "after_ref": after_ref,
                "before_commit": before_sha, "after_commit": after_sha}
    with tempfile.TemporaryDirectory(prefix="reprolab-git-") as temp:
        folder = Path(temp)
        export_revision(root, before_sha, folder / "before")
        export_revision(root, after_sha, folder / "after")
        if test_target:
            test = folder / "after" / target_file
            if not test.is_file():
                raise ValueError("修复后版本不存在选中的测试文件。")
        shutil.copyfile(test, folder / "regression.py")
        config = {
            "title": title or f"Git 修复验收：{root.name}",
            "description": "从两个 Git 提交读取源码，再运行同一份测试；未提交修改不参与比较。",
            "before": "before", "after": "after", "test": "regression.py",
            "expected_failure": {"type": failure_type, "message_contains": failure_message},
            "git_revision": revision, "test_target": test_target, "draft_origin": draft_origin or {}, "draft_review": draft_review if draft_review is not None else {},
        }
        case = folder / "case.json"
        case.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return run_case(case, output_root, timeout=timeout, python_executable=python_executable, control=control, previous_environment=previous_environment)
