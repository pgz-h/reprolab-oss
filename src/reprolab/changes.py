"""Bounded text diffs from the exact saved source snapshots."""

import difflib
import hashlib
from itertools import islice
from pathlib import Path
import stat


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def describe_changes(before: Path, after: Path) -> dict:
    trees = [{p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}
             for root in (before, after)]
    changes = []
    total = 0
    for name in sorted(trees[0].keys() | trees[1].keys()):
        old, new = trees[0].get(name), trees[1].get(name)
        old_mode = stat.S_IMODE(old.stat().st_mode) & 0o777 if old else None
        new_mode = stat.S_IMODE(new.stat().st_mode) & 0o777 if new else None
        same_content = bool(old and new and _digest(old) == _digest(new))
        if same_content and old_mode == new_mode:
            continue
        total += 1
        if len(changes) >= 20:
            continue
        change = {"path": name, "kind": "added" if old is None else "removed" if new is None else "modified",
                  "diff": "", "note": ""}
        if old and new and old_mode != new_mode:
            change["note"] = f"文件权限变化：{old_mode:04o} → {new_mode:04o}。"
        if same_content:
            changes.append(change)
            continue
        if any(p and p.stat().st_size > 128 * 1024 for p in (old, new)):
            change["note"] += "文件超过 128 KiB，省略文本预览。"
        else:
            try:
                texts = [p.read_text(encoding="utf-8") if p else "" for p in (old, new)]
                if any("\x00" in t for t in texts):
                    raise ValueError("binary data")
                lines = list(islice(difflib.unified_diff(texts[0].splitlines(), texts[1].splitlines(),
                                                       fromfile=f"before/{name}", tofile=f"after/{name}", lineterm=""), 201))
                change["diff"] = "\n".join(lines[:200])[:20000]
                if len(lines) > 200 or len("\n".join(lines[:200])) > 20000:
                    change["note"] += "差异预览已截断；完整文件保存在 evidence 目录。"
            except (UnicodeError, ValueError):
                change["note"] += "非 UTF-8 文本或二进制文件，省略文本预览。"
        changes.append(change)
    return {"files": changes, "total": total, "truncated": total > len(changes)}
