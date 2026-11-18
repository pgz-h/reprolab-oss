"""Run trusted migration sequences against independent, frozen SQLite seed copies."""
from datetime import datetime, timezone
import hashlib
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from .persistence import save_json
from .project_collection import normalize_collection_path

_KIND = 'reprolab_sqlite_matrix'
_RESULT = 'migration-matrix.json'
_MAX_SEED = 100 * 1024 * 1024


def _fingerprint(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def _recipe(value):
    if (not isinstance(value, dict) or value.keys() != {'schema_version', 'paths', 'probes'}
            or value.get('schema_version') != 1 or not isinstance(value['paths'], list)
            or not 1 <= len(value['paths']) <= 100 or not isinstance(value['probes'], dict)
            or not 1 <= len(value['probes']) <= 100):
        raise ValueError('SQLite 迁移方案须有版本 1、paths 与 probes。')
    names = set()
    for path in value['paths']:
        if (not isinstance(path, dict) or path.keys() != {'name', 'seed', 'before', 'after'}
                or not _text(path['name']) or path['name'] in names or not isinstance(path['seed'], str)
                or any(not isinstance(path[side], list) or len(path[side]) > 100
                       or any(not isinstance(item, str) for item in path[side]) for side in ('before', 'after'))):
            raise ValueError('SQLite 迁移路径须有唯一名称、初始数据库和两份 SQL 步骤清单。')
        names.add(path['name'])
    if any(not _text(name) or not isinstance(sql, str) or not sql.strip() or len(sql) > 16384
           for name, sql in value['probes'].items()):
        raise ValueError('必须提供有名称的单条 SQL 读探针。')
    return value


def _source(folder, name):
    relative = normalize_collection_path(name)
    path = folder / relative
    if not relative or not path.resolve().is_relative_to(folder) or path.is_symlink() or not path.is_file():
        raise ValueError(f'请选择方案目录内的普通输入文件：{name}')
    return path


def _read_recipe(path):
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.stat().st_size > (128 if source.name == _RESULT else 2) * 1024 * 1024:
        raise ValueError('请选择 2 MiB 以内的 SQLite 方案。')
    value = json.loads(source.read_bytes())
    if isinstance(value, dict) and value.get('kind') == _KIND:
        checked = check_sqlite_matrix(source.parent)
        return source.parent, checked['recipe']
    return source.parent, _recipe(value)


def _freeze(folder, stage, recipe, control):
    mapped = {}
    paths = []
    for row in recipe['paths']:
        control.check()
        seed = _source(folder, row['seed'])
        if any(seed.with_name(seed.name + suffix).exists() for suffix in ('-wal', '-journal')):
            raise ValueError('初始数据库须使用已关闭、已完成检查点的 SQLite 副本。')
        references = [row['seed'], *row['before'], *row['after']]
        for name in references:
            if name not in mapped:
                source = _source(folder, name)
                if source.stat().st_size > _MAX_SEED:
                    raise ValueError('SQLite 输入单文件超过 100 MiB。')
                target = stage / 'inputs' / f'{len(mapped) + 1:04d}'
                shutil.copyfile(source, target)
                if _fingerprint(source) != _fingerprint(target):
                    raise ValueError('冻结期间 SQLite 输入发生变化。')
                mapped[name] = target.relative_to(stage).as_posix()
        paths.append({'name': row['name'], 'seed': mapped[row['seed']],
                      **{side: [mapped[name] for name in row[side]] for side in ('before', 'after')}})
    return {**recipe, 'paths': paths}


def _value(value):
    return {'blob_hex': value.hex()} if isinstance(value, bytes) else value


def _run_side(stage, path, side, probes, timeout, control):
    phase = 'seed'
    try:
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='sqlite-cell-') as work:
            database = Path(work) / 'seed.db'
            shutil.copyfile(stage / path['seed'], database)
            def progress():
                control.check()
                return int(time.monotonic() - start > timeout)

            with closing(sqlite3.connect(database, timeout=min(timeout, 5))) as connection:
                connection.set_progress_handler(progress, 1000)
                for name in path[side]:
                    phase = name
                    control.check()
                    script = stage / name
                    if script.stat().st_size > 1024 * 1024:
                        raise ValueError('SQL 步骤超过 1 MiB。')
                    connection.executescript(script.read_text(encoding='utf-8'))
                connection.commit()
            observations = {}
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=min(timeout, 5))) as connection:
                connection.set_progress_handler(progress, 1000)
                for name, statement in probes.items():
                    phase = name
                    control.check()
                    cursor = connection.execute(statement)
                    if cursor.description is None:
                        raise ValueError('探针须返回数据。')
                    rows = cursor.fetchmany(1001)
                    if len(rows) > 1000:
                        raise ValueError('探针超过 1000 行，请缩小读取范围。')
                    value = {'columns': [column[0] for column in cursor.description],
                             'rows': [[_value(cell) for cell in row] for row in rows]}
                    payload = json.dumps(value, ensure_ascii=True, allow_nan=False)
                    if len(payload) > 1024 * 1024:
                        raise ValueError('探针结果超过 1 MiB。')
                    observations[name] = value
        return {'status': 'completed', 'probes': observations}
    except (OSError, sqlite3.Error, ValueError) as exc:
        control.check()
        return {'status': 'failed', 'phase': phase, 'error': {'type': type(exc).__name__, 'message': str(exc)[:500]}}


def _comparison(cells):
    statuses = []
    for row in cells:
        before, after = row['before'], row['after']
        if before['status'] != 'completed':
            status = 'blocked'
        elif after['status'] != 'completed' or before['probes'] != after['probes']:
            status = 'failed'
        else:
            status = 'passed'
        statuses.append(status)
    return statuses, ('failed' if 'failed' in statuses else 'blocked' if 'blocked' in statuses else 'passed')


def run_sqlite_matrix(recipe, output, *, confirmed, timeout, control):
    root, plan = _read_recipe(recipe)
    if not confirmed:
        return {'status': 'ready', 'paths': [path['name'] for path in plan['paths']],
                'probes': list(plan['probes'])}, 0
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('请选择新的 SQLite 矩阵目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sqlite-matrix-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'prepared'
        (stage / 'inputs').mkdir(parents=True)
        frozen = _freeze(root, stage, plan, control)
        cells = []
        for path in frozen['paths']:
            cells.append({'name': path['name'], **{side: _run_side(stage, path, side, frozen['probes'], timeout, control)
                                                  for side in ('before', 'after')}})
        states, status = _comparison(cells)
        record = {'kind': _KIND, 'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'status': status, 'recipe': frozen,
                  'source_sha256': {path.relative_to(stage).as_posix(): _fingerprint(path)
                                    for path in sorted((stage / 'inputs').iterdir())},
                  'paths': [{**row, 'verdict': state} for row, state in zip(cells, states)]}
        save_json(stage / _RESULT, record)
        control.commit()
        if output.exists() or output.is_symlink():
            raise ValueError('SQLite 矩阵目标已存在。')
        stage.rename(output)
    return {**record, 'directory': str(output)}, {'passed': 0, 'failed': 1, 'blocked': 2}[status]


def check_sqlite_matrix(directory):
    root = Path(directory).expanduser().resolve()
    index = root / _RESULT
    if index.is_symlink() or not index.is_file() or index.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('请选择完整的 SQLite 矩阵。')
    record = json.loads(index.read_bytes())
    if not isinstance(record, dict) or record.get('kind') != _KIND or record.get('schema_version') != 1:
        raise ValueError('SQLite 矩阵格式不受支持。')
    recipe = _recipe(record['recipe'])
    expected = {name for row in recipe['paths'] for name in (row['seed'], *row['before'], *row['after'])}
    if set(record['source_sha256']) != expected:
        raise ValueError('SQLite 矩阵缺少固定输入。')
    for name, digest in record['source_sha256'].items():
        if _fingerprint(_source(root, name)) != digest:
            raise ValueError(f'SQLite 矩阵输入指纹不符：{name}')
    if [row.get('name') for row in record['paths']] != [row['name'] for row in recipe['paths']]:
        raise ValueError('SQLite 矩阵结果与路径清单不一致。')
    states, status = _comparison(record['paths'])
    if status != record['status'] or states != [row['verdict'] for row in record['paths']]:
        raise ValueError('SQLite 矩阵判定与记录不一致。')
    return {**record, 'integrity': 'consistent'}
