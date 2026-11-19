"""Frozen Git comparisons for small native test runners."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from .git_source import export_revision, resolve_commit
from .persistence import save_json
from .project_archive import _MAX_BYTES, _MAX_ENTRIES, _archive_path, _extract
from .recipes import repository_root
from .runner import source_entries, source_fingerprint, source_mode_fingerprint

SIDES = ('before', 'after')
RUNNERS = ('cargo', 'cmake')
_RECORD = 'native-comparison.json'
_OUTCOMES = {'passed', 'failed', 'skipped', 'blocked'}
_EXIT = {'passed': 0, 'failed': 1, 'blocked': 2}
_RECORD_LIMIT = 16 * 1024 * 1024


def _adapter(runner, *, replay):
    if runner == 'cargo':
        from .native_cargo import read_cargo, run_cargo
        return read_cargo if replay else run_cargo
    if runner == 'cmake':
        from . import native_cmake
        return native_cmake.read_cmake if replay else native_cmake.run_cmake
    raise ValueError('原生执行器须为 cargo 或 cmake。')


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _RECORD_LIMIT:
        raise ValueError('请选择有效的原生比较证据记录。')
    with path.open('rb') as stream:
        raw = stream.read(_RECORD_LIMIT + 1)
    if len(raw) > _RECORD_LIMIT:
        raise ValueError('原生比较证据记录过大。')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('原生比较证据记录需要 JSON 对象。')
    return value


def _targets(selected):
    if selected is None:
        return None
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 500
            or any(not isinstance(name, str) or not name.strip() or len(name) > 1000
                   or any(ord(char) < 32 for char in name) for name in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('请选择 1–500 个唯一的完整测试名。')
    return selected


def _definitions(paths):
    if (not isinstance(paths, list) or len(paths) > 100
            or any(not isinstance(path, str) or _archive_path(path).as_posix() != path for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError('共享测试定义清单须为最多 100 个唯一的仓库相对文件路径。')
    return paths


def _source_record(tree):
    if tree.is_symlink() or not tree.is_dir():
        raise ValueError('固定源码目录不存在或不是普通目录。')
    entries, digest = source_fingerprint(tree)
    return {'sha256': digest, 'mode_sha256': source_mode_fingerprint(tree, entries)}


def _files(tree):
    if tree.is_symlink() or not tree.is_dir():
        raise ValueError('原生运行证据目录无效。')
    files, total = {}, 0
    def fail_walk(error):
        raise error
    for current, dirs, names in os.walk(tree, followlinks=False, onerror=fail_walk):
        directory = Path(current)
        for name in dirs + names:
            path = directory / name
            if path.is_symlink() or not (path.is_dir() if name in dirs else path.is_file()):
                raise ValueError('原生证据须为普通文件和目录。')
        for name in sorted(names):
            path = directory / name
            total += path.stat().st_size
            if len(files) >= _MAX_ENTRIES or total > _MAX_BYTES:
                raise ValueError('原生证据超过归档限制。')
            relative = _archive_path(path.relative_to(tree).as_posix()).as_posix()
            files[relative] = _sha(path)
    return dict(sorted(files.items()))


def _run_facts(result):
    if not isinstance(result, dict) or result.get('status') not in ('completed', 'blocked'):
        raise ValueError('原生执行器返回无效状态。')
    tests = result.get('tests')
    if (not isinstance(tests, dict) or len(tests) > 10000
            or any(not isinstance(name, str) or not name.strip() or len(name) > 1000
                   or any(ord(char) < 32 for char in name) or outcome not in _OUTCOMES
                   for name, outcome in tests.items())):
        raise ValueError('原生测试结果包含无效身份或结果。')
    # Adapters may include structured diagnostic metadata. Keep raw logs bound separately.
    return result


def _definition_record(root, paths):
    records = []
    for path in paths:
        sides = {}
        for side in SIDES:
            tree = root / 'inputs' / side
            source = tree / path
            if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(tree.resolve()):
                sides[side] = {'sha256': _sha(source), 'mode': stat.S_IMODE(source.stat().st_mode)}
            else:
                sides[side] = None
        records.append({'path': path, **sides, 'matches': sides['before'] is not None and sides['before'] == sides['after']})
    return records


def _compare(runs, definitions, selected, *, runner=None):
    source_status = ('matched' if definitions and all(row['matches'] for row in definitions)
                     else 'changed_or_missing' if definitions else 'undeclared')
    before, after = (runs[side]['tests'] for side in SIDES)
    scope = selected if selected is not None else sorted(before.keys() | after.keys())
    unexpected = {side: sorted(runs[side]['tests'].keys() - set(scope)) for side in SIDES}
    transitions = {('failed', 'passed'): 'improved', ('passed', 'failed'): 'regression_detected',
                   ('passed', 'passed'): 'regression_passed', ('failed', 'failed'): 'baseline_failure'}
    entries, counts = [], {}
    for identity in scope:
        left, right = before.get(identity), after.get(identity)
        shared = source_status == 'matched' and left is not None and right is not None
        configuration = None
        if runner == 'cmake' and left is not None and right is not None:
            settings = []
            for side in SIDES:
                recorded = runs[side].get('test_settings')
                settings.append(recorded.get(identity) if isinstance(recorded, dict) else None)
            if not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in settings):
                configuration = 'unverified'
            else:
                configuration = 'matched' if settings[0] == settings[1] else 'changed'
            shared = shared and configuration == 'matched'
        if left is None or right is None:
            observation = 'missing_both' if left is right else 'only_after' if left is None else 'only_before'
        elif not shared:
            observation = 'native_only'
        else:
            observation = transitions.get((left, right), 'inconclusive')
        entry = {'test_id': identity, 'before': left, 'after': right, 'shared': shared, 'status': observation}
        if configuration:
            entry['test_configuration'] = configuration
        entries.append(entry)
        counts[observation] = counts.get(observation, 0) + 1
    if counts.get('regression_detected'):
        status = 'failed'
    elif (not scope or source_status != 'matched' or any(unexpected.values())
          or any(runs[side]['status'] != 'completed' for side in SIDES)
          or set(counts) - {'improved', 'regression_passed'}):
        status = 'blocked'
    else:
        status = 'passed'
    return {
        'shared_test_sources': source_status, 'shared_count': sum(row['shared'] for row in entries),
        'unexpected_tests': unexpected, 'counts': counts, 'entries': entries,
        'gate': {'status': status, 'exit_code': _EXIT[status]},
    }


def run_native(repository, before, after, output, *, runner, selected=None, shared_sources=(),
               timeout=120, cargo=None, confirmed=False, control):
    selected = _targets(selected)
    paths = _definitions(list(shared_sources))
    if runner not in RUNNERS or cargo is not None and runner != 'cargo':
        raise ValueError('请选择有效的原生执行器；--cargo 仅用于 cargo。')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 3600:
        raise ValueError('原生运行时限须为 0.1–3600 秒。')
    if runner == 'cargo' and paths and not any(Path(path).suffix == '.rs' for path in paths):
        raise ValueError('Cargo 的共享测试定义清单须至少声明一个 .rs 文件。')
    root = repository_root(repository)
    commits = {side: resolve_commit(root, rev) for side, rev in (('before', before), ('after', after))}
    if not confirmed:
        return {'status': 'ready', 'runner': runner, 'commits': commits, 'selected_tests': selected}, 0
    requested = Path(output).expanduser()
    target = requested.resolve()
    if target.is_relative_to(root) or target.exists() or requested.is_symlink():
        raise ValueError('请选择仓库外尚不存在的证据目录。')
    adapter = _adapter(runner, replay=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-stage-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'native-comparison'
        (stage / 'inputs').mkdir(parents=True)
        sources, runs, hashes = {}, {}, {}
        for side in SIDES:
            control.check()
            fixed = stage / 'inputs' / side
            export_revision(root, commits[side], fixed)
            sources[side] = _source_record(fixed)
            evidence = stage / side
            evidence.mkdir()
            with tempfile.TemporaryDirectory(prefix='reprolab-native-run-') as run_directory:
                disposable = Path(run_directory) / 'source'
                shutil.copytree(fixed, disposable)
                options = {'cargo': cargo} if runner == 'cargo' else {}
                runs[side] = _run_facts(adapter(disposable, evidence, selected=selected,
                                                timeout=timeout, control=control, **options))
            hashes[side] = _files(evidence)
        definitions = _definition_record(stage, paths)
        comparison = _compare(runs, definitions, selected, runner=runner)
        record = {
            'kind': 'project_native_comparison', 'schema_version': 1,
            'runner': runner, 'commits': commits, 'selected_tests': selected,
            'source_snapshots': sources, 'shared_test_files': definitions, 'runs': runs,
            'evidence_sha256': hashes, 'comparison': comparison,
        }
        save_json(stage / _RECORD, record)
        _checked_native(stage, control)
        control.commit()
        if target.exists() or target.is_symlink():
            raise ValueError('证据目标已存在。')
        stage.rename(target)
    return {**record, 'directory': str(target)}, comparison['gate']['exit_code']


def _checked_native(folder, control):
    folder = Path(folder).resolve()
    record = _read_json(folder / _RECORD)
    if (record.get('kind') != 'project_native_comparison' or type(record.get('schema_version')) is not int
            or record['schema_version'] != 1 or record.get('runner') not in RUNNERS
            or set(record['commits']) != set(SIDES)
            or any(not isinstance(commit, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit)
                   for commit in record['commits'].values())):
        raise ValueError('原生比较证据格式无效。')
    selected = _targets(record['selected_tests'])
    paths = _definitions([row['path'] for row in record['shared_test_files']])
    reader = _adapter(record['runner'], replay=True)
    if record['runner'] == 'cargo' and paths and not any(Path(path).suffix == '.rs' for path in paths):
        raise ValueError('Cargo 共享源证据缺少 Rust 测试定义。')
    if (folder / 'inputs').is_symlink():
        raise ValueError('原生源码快照目录不能是链接。')
    for side in SIDES:
        control.check()
        if (_source_record(folder / 'inputs' / side) != record['source_snapshots'][side]
                or _files(folder / side) != record['evidence_sha256'][side]):
            raise ValueError('原生比较源码或运行证据指纹不一致。')
        facts = _run_facts(record['runs'][side])
        if _run_facts(reader(folder / side, selected=selected)) != facts:
            raise ValueError('原生运行记录与原始证据重算结果不一致。')
    definitions = _definition_record(folder, paths)
    if (definitions != record['shared_test_files']
            or _compare(record['runs'], definitions, selected, runner=record['runner']) != record['comparison']):
        raise ValueError('原生对照结果与固定记录不一致。')
    return record


def check_native(location, *, control):
    path = Path(location).expanduser()
    if path.is_symlink():
        raise ValueError('请选择普通证据目录或 ZIP。')
    if path.is_dir():
        return {**_checked_native(path, control), 'integrity': 'consistent'}
    if not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ValueError('请选择有效的原生证据目录或 ZIP。')
    with tempfile.TemporaryDirectory(prefix='reprolab-native-evidence-') as temporary:
        try:
            root = _extract(path, Path(temporary), control, _MAX_BYTES)
        except (BadZipFile, NotImplementedError) as exc:
            raise ValueError('无法读取原生证据 ZIP。') from exc
        return {**_checked_native(root, control), 'integrity': 'consistent'}


def pack_native(directory, output, *, control):
    folder = Path(directory).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    if target.exists() or Path(output).expanduser().is_symlink() or target.is_relative_to(folder):
        raise ValueError('请选择证据目录外尚不存在的 ZIP 路径。')
    record = _checked_native(folder, control)
    members = [Path(_RECORD)]
    for side in SIDES:
        tree = Path('inputs') / side
        members.extend([tree, *(tree / entry for entry in source_entries(folder / tree))])
        members.extend([Path(side), *(Path(side) / name for name in record['evidence_sha256'][side])])
    if len(members) > _MAX_ENTRIES or sum((folder / path).stat().st_size for path in members
                                          if (folder / path).is_file()) > _MAX_BYTES:
        raise ValueError('原生证据超过 ZIP 交付限制。')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-pack-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'evidence.zip'
        with ZipFile(stage, 'x', compression=ZIP_DEFLATED) as archive:
            for path in members:
                control.check()
                name = 'native-evidence/' + path.as_posix()
                _archive_path(name)
                archive.write(folder / path, name)
        restored = check_native(stage, control=control)
        if restored != {**record, 'integrity': 'consistent'}:
            raise ValueError('打包期间原生证据发生变化。')
        digest = _sha(stage)
        control.commit()
        try:
            os.link(stage, target)
        except FileExistsError as exc:
            raise ValueError('证据 ZIP 已存在。') from exc
    return {'status': 'packed', 'output': str(target), 'sha256': digest}
