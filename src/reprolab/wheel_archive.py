"""Portable, read-only handoff of a verified downstream wheel comparison."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import zipfile

from .project_archive import _archive_path, _extract, _MAX_BYTES, _MAX_ENTRIES
from .project_wheels import check_wheels
from .runner import source_entries


@contextmanager
def open_wheel_evidence(location, *, control):
    path = Path(location).expanduser()
    if path.is_dir():
        yield path
        return
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ValueError('请选择下游 wheel 证据目录，或不超过 250 MiB 的证据 ZIP。')
    with tempfile.TemporaryDirectory(prefix='wheel-evidence-') as stage:
        try:
            root = _extract(path, Path(stage), control, _MAX_BYTES)
        except (zipfile.BadZipFile, NotImplementedError) as exc:
            raise ValueError('无法读取 wheel 证据 ZIP。') from exc
        yield root


def _members(folder, record):
    snapshot = folder / 'downstream'
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError('wheel 证据缺少下游源码快照。')
    yield Path('wheel-comparison.json')
    yield Path('downstream')
    for relative in source_entries(snapshot):
        yield Path('downstream') / relative
    for side in ('before', 'after'):
        if (folder / side).is_symlink():
            raise ValueError('wheel 证据目录不能是链接。')
        artifact = Path(record['wheel_artifacts'][side]['file'])
        if artifact.parent != Path(side) or artifact.suffix != '.whl':
            raise ValueError('wheel 证据中的安装包路径无效。')
        yield artifact
        for name in record['runs'][side]['evidence_sha256']:
            if Path(name).name != name:
                raise ValueError('wheel 证据中的运行记录路径无效。')
            yield Path(side) / name


def pack_wheel_evidence(directory, destination, *, control):
    folder = Path(directory).expanduser().resolve()
    target = Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink() or target.resolve().is_relative_to(folder):
        raise ValueError('请在来源证据目录外选择一个尚不存在的 ZIP 路径。')
    record = check_wheels(folder, control=control)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wheel-pack-', dir=target.parent) as stage:
        staged = Path(stage) / 'wheel-evidence.zip'
        files = size = 0
        with zipfile.ZipFile(staged, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for index, relative in enumerate(_members(folder, record), start=1):
                control.check()
                source = folder / relative
                name = 'wheel-evidence/' + relative.as_posix()
                _archive_path(name)
                if source.is_symlink() or not source.resolve().is_relative_to(folder):
                    raise ValueError('wheel 证据包含不受支持的文件路径。')
                if source.is_file():
                    files += 1
                    size += source.stat().st_size
                elif not source.is_dir():
                    raise ValueError('wheel 证据需要普通文件或目录。')
                if index > _MAX_ENTRIES or size > _MAX_BYTES:
                    raise ValueError('wheel 证据超过 ZIP 交付限制。')
                archive.write(source, name)
        # Validate the actual exported bytes, including the snapshot permissions.
        with open_wheel_evidence(staged, control=control) as restored:
            if check_wheels(restored, control=control) != record:
                raise ValueError('归档期间 wheel 证据发生变化。')
        with staged.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        control.commit()
        try:
            os.link(staged, target)
        except FileExistsError:
            raise ValueError('目标 ZIP 已存在。') from None
    return {'status': 'packed', 'output': str(target), 'files': files, 'sha256': digest,
            'notice': '证据包包含安装制品、下游源码和日志。接收方可以离线使用 project check-wheels 核对。'}
