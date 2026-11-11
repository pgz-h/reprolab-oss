"""Pack verified project runs and their exact ancestry without changing report files."""
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from .runner import RunControl

_MAX_ENTRIES = 20000
_MAX_BYTES = 250 * 1024 * 1024
_MANIFEST = 'reprolab-history.json'


def _checked_chain(directory, *, include_sources, control, locations=None, bundled_only=False,
                   expected_report=None):
    from . import project_history as history
    from .processing_history import MAX_ROUNDS, MAX_REPORT_BYTES

    seen = set()
    total = 0
    reference = expected_report
    while True:
        control.check()
        if len(seen) >= MAX_ROUNDS:
            raise ValueError(f'复查来源超过 {MAX_ROUNDS} 轮；可以改为仅导出本次报告。')
        report = history._checked_report(directory, control=control)
        if reference is not None:
            history._match_reference(report, reference)
        identity = (report['project_id'], report['run_id'])
        if identity in seen:
            raise ValueError('复查来源重复或形成循环。')
        seen.add(identity)
        folder = Path(report['report_dir'])
        total += (folder / 'report.json').stat().st_size
        if include_sources and total > MAX_REPORT_BYTES:
            raise ValueError('复查链的报告 JSON 合计超过 64 MiB；可以改为仅导出本次报告。')
        yield report
        reference = report.get('review_source')
        if not include_sources or reference is None:
            return
        history.validate_report_reference(reference)
        if bundled_only:
            directory = history.bundled_source_directory(report, reference)
            if directory is None:
                raise ValueError(f'证据包缺少来源运行：{reference["run_id"]}')
        else:
            directory = history.source_report_directory(report, reference, (locations or {}).get(reference['report_sha256']))
            if not Path(directory).expanduser().exists():
                raise ValueError(f'未找到来源运行 {reference["run_id"]}；请重新定位，或仅导出本次报告。')


def _archive_path(name):
    parts = name.rstrip('/').split('/')
    path = PurePosixPath(name)
    if (not name or len(name) > 4000 or path.is_absolute() or '\\' in name or ':' in name
            or any(part in ('', '.', '..') for part in parts)
            or any(ord(character) < 32 for character in name)):
        raise ValueError('项目 ZIP 的条目路径无效。')
    return path


def pack_project_report(directory, destination, *, include_sources=False, locations=None,
                        expected_report=None, control=None):
    from . import project_history as history

    control = control or RunControl()
    target = Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('请选择报告目录外的新 ZIP 文件。')
    files = []
    references = []
    total = 0
    root = None
    for report in _checked_chain(directory, include_sources=include_sources, locations=locations,
                                 expected_report=expected_report, control=control):
        folder = Path(report['report_dir'])
        if target.resolve().is_relative_to(folder):
            raise ValueError('请选择报告及复查来源目录外的新 ZIP 文件。')
        reference = history.report_reference(report)
        if root is None:
            root = folder.name
            prefix = root
        else:
            prefix = f'{root}/{history.BUNDLED_SOURCES}/{reference["report_sha256"]}'
        references.append(reference)
        empty_dirs = (path for path in (folder / 'evidence/project/inputs').rglob('*')
                      if path.is_dir() and not any(path.iterdir()))
        members = [(folder / name, prefix + '/' + name)
                   for name in ('report.json', *report['persistence']['sha256'])]
        members.extend((path, prefix + '/' + path.relative_to(folder).as_posix() + '/') for path in empty_dirs)
        for path, name in members:
            control.check()
            _archive_path(name)
            files.append((path, name))
            total += 0 if name.endswith('/') else path.stat().st_size
            if len(files) > _MAX_ENTRIES or total > _MAX_BYTES:
                raise ValueError('项目证据超过 ZIP 交付限制（20000 条目 / 250 MiB）。')
    manifest = None
    if len(references) > 1:
        manifest = json.dumps({'schema_version': 1, 'reports': [ref['report_sha256'] for ref in references]})
        if len(files) + 1 > _MAX_ENTRIES or total + len(manifest) > _MAX_BYTES:
            raise ValueError('项目证据超过 ZIP 交付限制（20000 条目 / 250 MiB）。')
    target.parent.mkdir(parents=True, exist_ok=True)
    owned = False
    try:
        with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            owned = True
            for path, name in files:
                control.check()
                archive.write(path, name)
            if manifest is not None:
                archive.writestr(f'{root}/{_MANIFEST}', manifest)
        if target.stat().st_size > _MAX_BYTES:
            raise ValueError('项目证据 ZIP 超过 250 MiB。')
        for reference in references:
            report = history._checked_report(reference['report_dir'], control=control)
            history._match_reference(report, reference)
        control.commit()
    except BaseException:
        if owned:
            target.unlink(missing_ok=True)
        raise
    return {'status': 'packed', 'output': str(target), 'files': sum(not name.endswith('/') for _, name in files),
            'report_count': len(references),
            'notice': '解压后可使用 project check 核对并以 project replay 明确重放。'}


def _extract(bundle, stage, control):
    with zipfile.ZipFile(bundle) as archive:
        entries = archive.infolist()
        if len(entries) > _MAX_ENTRIES or sum(item.file_size for item in entries) > _MAX_BYTES:
            raise ValueError('项目 ZIP 超过条目或解压总大小限制。')
        seen, roots = set(), set()
        for item in entries:
            control.check()
            path = _archive_path(item.filename)
            mode = item.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                raise ValueError('项目 ZIP 包含链接、特殊文件或加密条目。')
            canonical = path.as_posix()
            if canonical in seen:
                raise ValueError('项目 ZIP 含重复条目。')
            seen.add(canonical)
            roots.add(path.parts[0])
            local = stage.joinpath(*path.parts)
            if item.is_dir():
                local.mkdir(parents=True, exist_ok=True)
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, local.open('xb') as stream:
                    shutil.copyfileobj(source, stream, length=64 * 1024)
                local.chmod(mode & 0o777 if mode else 0o644)
        if len(roots) != 1 or not (stage / next(iter(roots))).is_dir():
            raise ValueError('项目 ZIP 需要包含一个完整报告根目录。')
        return stage / next(iter(roots))


def unpack_project_report(bundle, destination, *, control=None):
    """Verify the archive in staging and publish only after checking all bundled runs."""
    from .project_history import BUNDLED_SOURCES

    control = control or RunControl()
    bundle = Path(bundle).expanduser().resolve()
    target = Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('请选择一个不存在的项目证据导入目录。')
    if not bundle.is_file() or bundle.stat().st_size > _MAX_BYTES:
        raise ValueError('项目 ZIP 不存在或超过 250 MiB。')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.project-import-', dir=target.parent) as temp:
        try:
            imported = _extract(bundle, Path(temp), control)
        except (zipfile.BadZipFile, NotImplementedError) as exc:
            raise ValueError('无法读取项目证据 ZIP。') from exc
        sources = imported / BUNDLED_SOURCES
        manifest = imported / _MANIFEST
        has_sources = manifest.exists()
        if has_sources != sources.is_dir() or (sources.exists() and not sources.is_dir()):
            raise ValueError('项目 ZIP 的复查来源清单或目录缺失。')
        if has_sources:
            if not manifest.is_file() or manifest.stat().st_size > 4096:
                raise ValueError('项目 ZIP 的复查来源清单无效。')
            expected = json.loads(manifest.read_bytes())
        fingerprints = [report['_report_sha256'] for report in _checked_chain(imported,
            include_sources=has_sources, bundled_only=True, control=control)]
        if has_sources and (len(fingerprints) < 2
                or expected != {'schema_version': 1, 'reports': fingerprints}
                or {path.name for path in sources.iterdir()} != set(fingerprints[1:])):
            raise ValueError('项目 ZIP 的复查来源与清单不一致。')
        control.commit()
        if target.exists() or target.is_symlink():
            raise ValueError('导入目标已被其他操作创建，未覆盖。')
        imported.rename(target)
    return {'status': 'imported', 'directory': str(target), 'report_dir': str(target),
            'report_count': len(fingerprints), 'notice': '项目证据已校验并导入，未执行代码；审阅后可明确重放。'}
