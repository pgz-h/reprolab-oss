"""Pack verified project runs and their exact ancestry without changing report files."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import unicodedata
import zipfile

from .runner import RunControl

_MAX_ENTRIES = 20000
_MAX_BYTES = 250 * 1024 * 1024
_MANIFEST = 'reprolab-history.json'


def _checked_chain(directory, *, include_sources, control, locations=None, bundled_only=False,
                   expected_report=None, series=False):
    from . import project_history as history
    from .processing_history import MAX_ROUNDS, MAX_REPORT_BYTES

    seen = set()
    total = 0
    reference = expected_report
    while True:
        control.check()
        if len(seen) >= (2000 if series else MAX_ROUNDS):
            raise ValueError(f'复查来源超过 {2000 if series else MAX_ROUNDS} 轮。')
        report = history._checked_report(directory, control=control)
        if reference is not None:
            history._match_reference(report, reference)
        identity = (report['project_id'], report['run_id'])
        if identity in seen:
            raise ValueError('复查来源重复或形成循环。')
        seen.add(identity)
        folder = Path(report['report_dir'])
        total += (folder / 'report.json').stat().st_size
        if include_sources and not series and total > MAX_REPORT_BYTES:
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
        raise ValueError(f'项目 ZIP 不支持此条目路径：{name!r}')
    return path


def source_zip_preflight(snapshots, *, runner='pytest', control=None):
    """Check frozen source paths against the standalone report format and common Windows limits."""
    prefix = {'pytest': 'project-run', 'go': 'go-run', 'vitest': 'vitest-run'}[runner]
    root = prefix + '-20000101T000000Z-00000000'
    blocking, portability = [], []
    blocked = review = 0

    def add(side, path, reason, *, conflict=None):
        nonlocal blocked, review
        if reason == 'archive_path':
            blocked += 1
            items = blocking
        else:
            review += 1
            items = portability
        if len(items) < 24:
            item = {'side': side, 'path': path, 'reason': reason}
            if conflict is not None:
                item['conflicts_with'] = conflict
            items.append(item)

    for side in ('before', 'after'):
        tree = Path(snapshots) / side
        names = {}
        for path in sorted(tree.rglob('*')):
            if control:
                control.check()
            relative = path.relative_to(tree).as_posix()
            member = f'{root}/evidence/project/inputs/{side}/{relative}'
            try:
                _archive_path(member)
            except ValueError:
                add(side, relative, 'archive_path')
            component = path.name
            device = component.partition('.')[0].rstrip().upper()
            if (any(character in component for character in '<>"|?*')
                    or component.endswith(('.', ' '))
                    or device in ('CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$')
                    or device[:3] in ('COM', 'LPT') and len(device) == 4 and device[3] in '123456789¹²³'):
                add(side, relative, 'windows_name')
            canonical = unicodedata.normalize('NFC', relative).casefold()
            previous = names.setdefault(canonical, relative)
            if previous != relative:
                add(side, relative, 'case_or_unicode_collision', conflict=previous)
    issues = (blocking + portability)[:24]
    return {'status': 'blocked' if blocked else 'review' if review else 'ready',
            'blocked_count': blocked, 'portability_count': review, 'issues': issues,
            'omitted': blocked + review - len(issues)}


def archive_preflight_text(preflight):
    if not preflight or preflight['status'] == 'ready':
        return ''
    parts = []
    if count := preflight['blocked_count']:
        parts.append(f'{count} 个快照路径阻碍证据 ZIP 导出')
    if count := preflight['portability_count']:
        parts.append(f'{count} 个跨平台路径问题需核对')
    return ' · '.join(parts)


def pack_project_report(directory, destination, *, include_sources=False, locations=None,
                        expected_report=None, control=None, _max_bytes=None):
    from . import project_history as history

    control = control or RunControl()
    byte_limit = _MAX_BYTES if _max_bytes is None else _max_bytes
    target = Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('目标路径已存在，请选择新的 ZIP 文件名或位置。')
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
            if len(files) > _MAX_ENTRIES or total > byte_limit:
                raise ValueError(f'项目证据超过 ZIP 交付限制（{_MAX_ENTRIES} 条目 / {byte_limit} 字节）。')
    manifest = None
    if len(references) > 1:
        manifest = json.dumps({'schema_version': 1, 'reports': [ref['report_sha256'] for ref in references]})
        if len(files) + 1 > _MAX_ENTRIES or total + len(manifest) > byte_limit:
            raise ValueError(f'项目证据超过 ZIP 交付限制（{_MAX_ENTRIES} 条目 / {byte_limit} 字节）。')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.project-pack-', dir=target.parent) as directory:
        staged = Path(directory) / 'evidence.zip'
        with zipfile.ZipFile(staged, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for path, name in files:
                control.check()
                archive.write(path, name)
            if manifest is not None:
                archive.writestr(f'{root}/{_MANIFEST}', manifest)
        if staged.stat().st_size > byte_limit:
            raise ValueError(f'项目证据 ZIP 超过 {byte_limit} 字节。')
        for reference in references:
            report = history._checked_report(reference['report_dir'], control=control)
            history._match_reference(report, reference)
        control.commit()
        try:
            os.link(staged, target)
        except FileExistsError:
            raise ValueError('目标路径已存在，请选择新的 ZIP 文件名或位置。') from None
    return {'status': 'packed', 'output': str(target), 'files': sum(not name.endswith('/') for _, name in files),
            'report_count': len(references),
            'notice': '解压后可使用 project check 核对并以 project replay 明确重放。'}


def _extract(bundle, stage, control, byte_limit):
    with zipfile.ZipFile(bundle) as archive:
        entries = archive.infolist()
        if len(entries) > _MAX_ENTRIES or sum(item.file_size for item in entries) > byte_limit:
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


def unpack_project_report(bundle, destination, *, control=None, _max_bytes=None):
    """Verify the archive in staging and publish only after checking all bundled runs."""
    from .project_history import BUNDLED_SOURCES

    control = control or RunControl()
    byte_limit = _MAX_BYTES if _max_bytes is None else _max_bytes
    bundle = Path(bundle).expanduser().resolve()
    target = Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('请选择一个不存在的项目证据导入目录。')
    if not bundle.is_file() or bundle.stat().st_size > byte_limit:
        raise ValueError(f'项目 ZIP 不存在或超过 {byte_limit} 字节。')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.project-import-', dir=target.parent) as temp:
        try:
            imported = _extract(bundle, Path(temp), control, byte_limit)
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
