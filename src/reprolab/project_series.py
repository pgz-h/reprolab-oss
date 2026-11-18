"""Transport a verified report lineage as bounded segments rather than one archive."""
import hashlib
import json
import shutil
from pathlib import Path
import tempfile

from .persistence import save_json
from .project_archive import _checked_chain, pack_project_report, unpack_project_report
from .project_history import BUNDLED_SOURCES, _hash, _match_reference, read_report_snapshot

_INDEX = 'series.json'
_BLOCK = 128 * 1024
_DEFAULT_PART = 64 * 1024 * 1024
_LIMIT = 2 * 1024 * 1024 * 1024


def _part_name(report, part):
    return f'run-{report:04d}-part-{part:04d}.bin'


def _transfer(source, output, size, control):
    digest = hashlib.sha256()
    written = 0
    while written < size:
        control.check()
        data = source.read(min(_BLOCK, size - written))
        if not data:
            break
        output.write(data)
        digest.update(data)
        written += len(data)
    return written, digest.hexdigest()


def pack_series(location, output, *, locations, part_mib=64, control):
    if not 1 <= part_mib <= 250:
        raise ValueError('每段大小需为 1–250 MiB。')
    limit = part_mib * 1024 * 1024
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('请选择尚不存在的分段交付目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reprolab-series-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'series'
        stage.mkdir()
        rows = []
        for report in _checked_chain(location, include_sources=True, control=control, locations=locations, series=True):
            folder = Path(report['report_dir'])
            if output.resolve().is_relative_to(folder):
                raise ValueError('请将分段交付目录保存到报告之外。')
            archive = Path(temporary) / 'single.zip'
            pack_project_report(folder, archive, control=control.child(), _max_bytes=_LIMIT)
            # Reopen the exact staged bytes. This also detects an edit that happened during export.
            probe = Path(temporary) / 'probe'
            unpack_project_report(archive, probe, control=control.child(), _max_bytes=_LIMIT)
            checked = read_report_snapshot(probe)
            if checked['_report_sha256'] != report['_report_sha256']:
                raise ValueError('分段期间来源报告发生变化。')
            shutil.rmtree(probe)
            row = {'report_sha256': report['_report_sha256'], 'run_id': report['run_id'], 'parts': []}
            with archive.open('rb') as stream:
                remaining = archive.stat().st_size
                while remaining:
                    name = _part_name(len(rows) + 1, len(row['parts']) + 1)
                    size = min(remaining, limit)
                    with (stage / name).open('xb') as handle:
                        actual, digest = _transfer(stream, handle, size, control)
                    if actual != size:
                        raise ValueError('分段期间 ZIP 长度发生变化。')
                    row['parts'].append({'file': name, 'bytes': size, 'sha256': digest})
                    remaining -= size
            archive.unlink()
            rows.append(row)
        index = {'kind': 'reprolab_report_series', 'schema_version': 1, 'reports': rows}
        save_json(stage / _INDEX, index)
        if (stage / _INDEX).stat().st_size > 16 * 1024 * 1024:
            raise ValueError('分段交付索引超过 16 MiB。')
        control.commit()
        if output.exists() or output.is_symlink():
            raise ValueError('分段交付目标已存在。')
        stage.rename(output)
    return {**index, 'status': 'packed', 'output': str(output)}


def _index(root):
    path = root / _INDEX
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('请选择有效的分段交付索引。')
    data = json.loads(path.read_bytes())
    if (not isinstance(data, dict) or data.keys() != {'kind', 'schema_version', 'reports'}
            or data['kind'] != 'reprolab_report_series' or data['schema_version'] != 1
            or not isinstance(data['reports'], list) or not 1 <= len(data['reports']) <= 2000):
        raise ValueError('分段交付索引格式无效。')
    return data


def _restore(location, temporary, control):
    root = Path(location).expanduser().resolve()
    index = _index(root)
    original = parent = None
    seen = set()
    for number, row in enumerate(index['reports'], 1):
        if (not isinstance(row, dict) or row.keys() != {'report_sha256', 'run_id', 'parts'}
                or not _hash(row['report_sha256']) or not isinstance(row['run_id'], str)
                or not isinstance(row['parts'], list) or not 1 <= len(row['parts']) <= 2048
                or row['report_sha256'] in seen or number > 1 and parent is None):
            raise ValueError('分段交付的报告行不完整或重复。')
        digest = row['report_sha256']
        seen.add(digest)
        archive = temporary / 'join.zip'
        total = 0
        with archive.open('xb') as output:
            for index_number, part in enumerate(row['parts'], 1):
                name = _part_name(number, index_number)
                if (not isinstance(part, dict) or part.keys() != {'file', 'bytes', 'sha256'} or part['file'] != name
                        or type(part['bytes']) is not int or not 1 <= part['bytes'] <= 250 * 1024 * 1024
                        or not _hash(part['sha256'])):
                    raise ValueError('分段描述无效。')
                total += part['bytes']
                path = root / name
                if (total > _LIMIT or path.is_symlink() or not path.is_file()
                        or path.stat().st_size != part['bytes']):
                    raise ValueError('分段缺失或大小不符：' + name)
                with path.open('rb') as stream:
                    actual, fingerprint = _transfer(stream, output, part['bytes'], control)
                    if actual != part['bytes'] or fingerprint != part['sha256'] or stream.read(1):
                        raise ValueError('分段指纹不符：' + name)
        report_path = temporary / 'report'
        unpack_project_report(archive, report_path, control=control.child(), _max_bytes=_LIMIT)
        archive.unlink()
        report = read_report_snapshot(report_path)
        if report['_report_sha256'] != digest or report['run_id'] != row['run_id']:
            raise ValueError('分段索引与还原报告身份不符。')
        if parent is not None:
            _match_reference(report, parent)
        parent = report.get('review_source')
        if original is None:
            original = temporary / 'restored'
            report_path.rename(original)
        else:
            sources = original / BUNDLED_SOURCES
            sources.mkdir(exist_ok=True)
            report_path.rename(sources / digest)
    if parent is not None:
        raise ValueError('分段交付缺少最后的来源报告。')
    return original, index


def receive_series(location, *, output=None, control):
    target = Path(output).expanduser().absolute() if output is not None else None
    if target is not None:
        if target.exists() or target.is_symlink():
            raise ValueError('请选择尚不存在的导入目录。')
        target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reprolab-unseries-', dir=target.parent if target else None) as tmp:
        restored, index = _restore(location, Path(tmp), control)
        if target is not None:
            control.commit()
            if target.exists() or target.is_symlink():
                raise ValueError('导入目录已存在。')
            restored.rename(target)
    return {'status': 'imported' if target else 'consistent', 'report_count': len(index['reports']),
            'report_sha256': index['reports'][0]['report_sha256'],
            **({'report_dir': str(target)} if target else {})}
