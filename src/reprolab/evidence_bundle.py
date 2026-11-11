"""Portable local evidence ZIPs with a bounded, non-executing import path."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile
import zlib

from .evidence import inspect_report, FIXED_FILES, MAX_EVIDENCE_BYTES, MAX_EVIDENCE_ENTRIES
from .runner import EXCLUDED

MAX_BUNDLE_BYTES = MAX_EVIDENCE_BYTES
MAX_BUNDLE_ENTRIES = MAX_EVIDENCE_ENTRIES
MANIFEST = "reprolab-bundle.json"
README = "README_REPLAY.txt"
REPLAY_TEXT = """ReproLab 本地证据包

打开 report.html 查看原验收报告。recorded_verdict 是原记录，不是导入时新跑出的结论。

仅在审阅并信任其中代码后重放：
  python -m reprolab verify evidence/case.json --python /path/to/env/bin/python --output /path/to/new-reports

先安装 ReproLab（当前从源码安装），再进入导入目录执行上面的命令。
请把输出目录放在本目录之外。需要 Python >=3.11、pytest >=8,<10 及项目依赖。
包不包含虚拟环境，也不会安装依赖或自动执行代码。报告中的原始机器路径仅作为记录。
文件包含源码、测试、日志和项目数据，请在决定分享前自行查看。本工具不会上传文件。
SHA-256 清单能检查内容一致性，不是数字签名，不能证明作者身份或测试语义正确。
"""


def _safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or any(ord(c)<32 for c in name):
        raise ValueError("证据包包含不支持的路径。")
    path = PurePosixPath(name.rstrip("/"))
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name.rstrip("/") or str(path) == ".":
        raise ValueError("证据包路径必须是规范的包内相对路径。")
    return path.as_posix()


def _allowed(name):
    if name in (*FIXED_FILES, "evidence", "evidence/before", "evidence/after", README):
        return True
    if name.startswith(("evidence/before/", "evidence/after/")):
        return not any(p in EXCLUDED or p.startswith(".env") for p in PurePosixPath(name).parts[2:])
    return False


def _zip_info(name, entry):
    info = zipfile.ZipInfo(name + ("/" if entry["type"] == "directory" else ""))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((stat.S_IFDIR if entry["type"] == "directory" else stat.S_IFREG) | entry["mode"]) << 16
    if entry["type"] == "directory":
        info.external_attr |= 0x10
    return info


def pack_report(directory, output):
    result, entries = inspect_report(directory)
    if result["status"] != "consistent":
        raise ValueError("证据不一致，未打包：" + "；".join(result["issues"]))
    root = Path(result["report_dir"])
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("证据包输出已存在，未覆盖。")
    output = output.resolve()
    if output.is_relative_to(root):
        raise ValueError("证据包输出必须放在原报告目录之外。")
    readme = REPLAY_TEXT.encode("utf-8")
    entries[README] = {"type": "file", "mode": 0o644, "size": len(readme), "sha256": hashlib.sha256(readme).hexdigest()}
    for name in entries:
        if not _allowed(_safe_name(name)):
            raise ValueError("报告包含不能放入证据包的路径。")
    if len(entries)+1 > MAX_BUNDLE_ENTRIES or sum(e["size"] for e in entries.values()) > MAX_BUNDLE_BYTES:
        raise ValueError("证据包超过条目或总大小限制。")
    manifest = {"kind": "reprolab-evidence", "schema_version": 2,
                "created_at": datetime.now(timezone.utc).isoformat(), "run_id": result["run_id"],
                "recorded_verdict": result["recorded_verdict"], "entries": entries}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(manifest_bytes) > 4 * 1024 * 1024 or sum(e["size"] for e in entries.values()) + len(manifest_bytes) > MAX_BUNDLE_BYTES:
        raise ValueError("含清单的证据包超过大小限制。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".reprolab-", suffix=".zip", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, entry in entries.items():
                info = _zip_info(name, entry)
                if entry["type"] == "directory":
                    archive.writestr(info, b"")
                    continue
                digest, size = hashlib.sha256(), 0
                with archive.open(info, "w") as target:
                    if name == README:
                        target.write(readme);digest.update(readme);size=len(readme)
                    else:
                        path = root / name
                        mode = path.lstat().st_mode
                        if not stat.S_ISREG(mode):
                            raise ValueError("打包时证据文件类型发生变化。")
                        if stat.S_IMODE(mode) & 0o777 != entry["mode"]:
                            raise ValueError("打包时证据文件权限发生变化，请重新检查。")
                        with path.open("rb") as source:
                            while block := source.read(128 * 1024):
                                size += len(block)
                                if size > entry["size"]:
                                    raise ValueError("打包时证据发生变化，请重新检查。")
                                digest.update(block);target.write(block)
                        if stat.S_IMODE(path.lstat().st_mode) & 0o777 != entry["mode"]:
                            raise ValueError("打包时证据文件权限发生变化，请重新检查。")
                if digest.hexdigest() != entry["sha256"] or size != entry["size"]:
                    raise ValueError("打包时证据发生变化，请重新检查。")
            archive.writestr(MANIFEST, manifest_bytes)
        if temporary.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("压缩包超过大小限制。")
        os.link(temporary, output)  # Atomic no-overwrite publication.
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "packed", "archive": str(output), "recorded_verdict": result["recorded_verdict"],
            "file_count": result["file_count"]+1, "archive_bytes": output.stat().st_size,
            "notice": "证据已打包，包含源码和项目数据；没有上传、没有重新运行测试。"}


def _members(archive):
    infos = archive.infolist()
    if len(infos) > MAX_BUNDLE_ENTRIES:
        raise ValueError("证据包超过 12000 个条目限制。")
    entries, total = {}, 0
    for info in infos:
        name = _safe_name(info.filename)
        if name in entries:
            raise ValueError("证据包包含重复路径。")
        if name != MANIFEST and not _allowed(name):
            raise ValueError("证据包包含范围外的文件。")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR) != info.is_dir() and kind != 0:
            raise ValueError("证据包包含符号链接、特殊文件或类型不一致的条目。")
        if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError("不支持加密或此压缩格式的证据包。")
        total += info.file_size
        if total > MAX_BUNDLE_BYTES or info.file_size < 0:
            raise ValueError("证据包解压后总大小超过限制。")
        entries[name] = info
    if MANIFEST not in entries or entries[MANIFEST].file_size > 4 * 1024 * 1024:
        raise ValueError("证据包缺少有效清单。")
    try:
        manifest = json.loads(archive.read(entries[MANIFEST]))
    except (ValueError, UnicodeError):
        raise ValueError("证据包清单不是有效 JSON。") from None
    if (not isinstance(manifest, dict) or manifest.get("kind") != "reprolab-evidence"
            or type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in (1, 2)):
        raise ValueError("不支持的证据包格式。")
    records = manifest.get("entries")
    if not isinstance(records, dict) or set(records) != set(entries)-{MANIFEST}:
        raise ValueError("证据包清单与文件列表不一致。")
    for name, record in records.items():
        info = entries[name]
        if (not isinstance(record, dict) or record.get("type") not in ("file", "directory")
                or type(record.get("size")) is not int or record["size"] != info.file_size
                or type(record.get("mode")) is not int or not 0 <= record["mode"] <= 0o777
                or (manifest["schema_version"] == 1 and record["mode"] not in (0o644, 0o755))
                or (record["type"] == "directory") != info.is_dir()):
            raise ValueError("证据包条目元数据不一致。")
        if record["type"] == "file" and (not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise ValueError("证据包文件缺少有效指纹。")
        if record["type"] == "directory" and record["size"] != 0:
            raise ValueError("目录条目不能包含文件内容。")
    return entries, manifest


def unpack_report(path, destination):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("请选择存在的普通 ZIP 文件，大小不超过 250 MiB。")
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("导入目标已存在，未覆盖。")
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(path) as archive:
            members, manifest = _members(archive)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".reprolab-import-") as temporary:
                stage = Path(temporary) / "content"
                stage.mkdir()
                for name, record in sorted(manifest["entries"].items(), key=lambda item: (item[0].count("/"), item[0])):
                    target = stage.joinpath(*PurePosixPath(name).parts)
                    if record["type"] == "directory":
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest, size = hashlib.sha256(), 0
                    with archive.open(members[name]) as source, target.open("xb") as output:
                        while block := source.read(128 * 1024):
                            size += len(block)
                            if size > record["size"]:
                                raise ValueError("证据包内容超过清单声明大小。")
                            digest.update(block);output.write(block)
                    if size != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise ValueError(f"证据包内容校验失败：{name}")
                    target.chmod(record["mode"])
                (stage / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                checked, _ = inspect_report(stage)
                if checked["status"] != "consistent":
                    raise ValueError("包内报告证据不一致：" + "；".join(checked["issues"]))
                if checked["recorded_verdict"] != manifest.get("recorded_verdict") or checked["run_id"] != manifest.get("run_id"):
                    raise ValueError("清单中的运行信息与报告不一致。")
                destination.mkdir(mode=0o700)  # Reserve without overwriting any existing directory.
                try:
                    stage.replace(destination)
                except BaseException:
                    try:
                        destination.rmdir()
                    except OSError:
                        pass
                    raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError, RuntimeError, zlib.error):
        raise ValueError("证据包损坏或使用了不支持的 ZIP 格式。") from None
    return {"status": "imported", "directory": str(destination), "recorded_verdict": checked["recorded_verdict"],
            "report": str(destination / "report.html"), "case": str(destination / "evidence/case.json"),
            "notice": "证据已校验并导入；没有执行测试。审阅可信后可用 verify 重放案例。"}
