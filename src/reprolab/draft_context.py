"""Read a bounded, reviewable selection of committed text; never import it."""
import hashlib
from pathlib import Path, PurePosixPath
import re

from .git_source import _git, resolve_commit
from .recipes import repository_root
from .runner import EXCLUDED

MAX_FILE_BYTES = 24 * 1024
MAX_CONTEXT_BYTES = 96 * 1024
MAX_FILES = 12
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "pytest.ini", "README.md"}


def _allowed(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts and "\\" not in name
            and not any(ord(c) < 32 for c in name)
            and not any(p in EXCLUDED or p.startswith(".env") for p in path.parts)
            and (path.suffix in {".py", ".md", ".rst"} or path.name in CONFIG_FILES))


def _tree(root, sha):
    entries = {}
    for record in _git(root, "ls-tree", "-r", "-l", "-z", "--full-tree", sha).split(b"\0"):
        if not record:
            continue
        meta, raw_name = record.split(b"\t", 1)
        mode, kind, oid, size = meta.split()
        name = raw_name.decode("utf-8")
        if _allowed(name) and mode in (b"100644", b"100755") and kind == b"blob":
            entries[name] = (oid.decode(), int(size))
    return entries


def collect_context(repository, before_ref, after_ref, issue, paths=None):
    root = repository_root(repository)
    before, after = resolve_commit(root, before_ref), resolve_commit(root, after_ref)
    trees = {"before": _tree(root, before), "after": _tree(root, after)}
    available = set(trees["before"]) | set(trees["after"])
    explicit = bool(paths)
    notes = []
    if explicit:
        chosen = list(dict.fromkeys(paths))
        if len(chosen) > MAX_FILES:
            raise ValueError("上下文最多选择 12 个文件。")
        for name in chosen:
            if not _allowed(name) or name not in available:
                raise ValueError(f"上下文路径不存在、不支持或属于排除项：{name}")
    else:
        changed = set(_git(root, "diff", "--name-only", "-z", before, after, "--").decode("utf-8").split("\0"))
        words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", issue.lower()))
        sources = [p for p in available if p.endswith(".py")]
        sources.sort(key=lambda p: (p not in changed, -sum(w in p.lower() for w in words), p))
        chosen = sources[:8] + sorted(available & CONFIG_FILES)
        if len(sources) > 8:
            notes.append(f"自动选择仅取前 8 个相关/变更 Python 文件，另有 {len(sources)-8} 个未加入；可用 --context 指定。")
    files, total = [], 0
    for name in chosen:
        sizes = [tree[name][1] for tree in trees.values() if name in tree]
        if max(sizes) > MAX_FILE_BYTES or total + sum(sizes) > MAX_CONTEXT_BYTES:
            reason = f"{name} 超过单版本文件 24 KiB 或上下文总计 96 KiB 限制。"
            if explicit:
                raise ValueError(reason)
            notes.append(reason + " 已省略。")
            continue
        item = {"path": name}
        try:
            for label, tree in trees.items():
                content = _git(root, "cat-file", "blob", tree[name][0]) if name in tree else None
                item[label] = content.decode("utf-8") if content is not None else None
                if content is not None and b"\0" in content:
                    raise UnicodeError("binary text")
                item[label + "_sha256"] = hashlib.sha256(content).hexdigest() if content is not None else None
        except UnicodeError:
            if explicit:
                raise ValueError(f"上下文必须是 UTF-8 文本：{name}") from None
            notes.append(f"{name} 不是 UTF-8 文本，已省略。")
            continue
        files.append(item)
        total += sum(sizes)
    if not any(item["path"].endswith(".py") for item in files):
        raise ValueError("没有可用的 Python 代码上下文；请用 --context 指定相关文件。")
    return root, {"schema_version": 1, "repository": root.name, "issue": issue,
                  "before_commit": before, "after_commit": after,
                  "selected_refs": {"before": before_ref, "after": after_ref},
                  "selection": "explicit" if explicit else "automatic", "files": files,
                  "source_bytes": total, "notes": notes}
