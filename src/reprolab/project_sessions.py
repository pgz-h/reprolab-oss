from .process_tree import spawn, terminate_tree
from .platform_support import exclusive_lock
"""Frozen, persistent project regression sessions with one pytest process per version."""
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

from . import __version__
from .case import parse_test_target, valid_source_mode_hashes
from .changes import describe_changes
from .environment import snapshot_environment
from .git_source import export_revision, resolve_commit
from .persistence import save_json, atomic_write
from .project_selections import reconcile_selection, requested_selection
from .project_archive import pack_project_report, unpack_project_report
from .recipes import repository_root
from .repair_suite import _new_id
from .runner import source_entries, source_fingerprint, source_mode_fingerprint, snapshot, RunControl, RunCancelled
from .runtime import snapshot_process_environment, choose_runtime
from .pytest_profile import read_pytest_profile, profile_args
from .project_progress import ProgressReader
from .project_collection import collection_diagnosis, normalize_collection_path, require_collection_target
from .runtime_environment import resolve_variables, variable_record
from .workspace import WORKTREE, copy_workspace

MAX_SELECTED = 2000
MAX_RECORD_BYTES = 128 * 1024 * 1024
NOTICE = ('项目回归结果覆盖本次所选测试。两版在独立副本中使用修复后版本的同一份目标测试文件，'
          '各版本的项目配置和 fixture 分别保留。执行前后各一个 pytest 进程；原始源码证据保持完整。')


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError(f'记录不存在、过大或不是普通文件：{path.name}')
    try:
        data = json.loads(path.read_bytes())
    except (ValueError, RecursionError):
        raise ValueError(f'记录不是有效 JSON：{path.name}') from None
    if not isinstance(data, dict):
        raise ValueError(f'记录需要 JSON 对象：{path.name}')
    return data


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not .1 <= value <= 3600:
        raise ValueError('项目运行时限须为 0.1–3600 秒，按每版全部所选测试计。')
    return value


@contextmanager
def _lock(directory):
    with exclusive_lock(Path(directory) / '.project.lock', '这个项目会话已有操作，请等待完成或取消。'):
        yield

def _identity(state):
    identity = {key: state[key] for key in ('project_id', 'source_sha256', 'source_mode_sha256')}
    if 'runner' in state:
        identity['runner'] = state['runner']
    return identity


def _check_sources(folder, state):
    for label in ('before', 'after'):
        root = Path(folder) / 'inputs' / label
        if (source_fingerprint(root)[1] != state['source_sha256'][label]
                or source_mode_fingerprint(root) != state['source_mode_sha256'][label]):
            raise ValueError('项目固定源码或普通文件权限发生变化，请重新准备。')


def read_project(directory):
    candidate = Path(directory).expanduser()
    if candidate.is_symlink():
        raise ValueError('项目会话目录不能是符号链接。')
    folder = candidate.resolve()
    state = _read(folder / 'project.json')
    if (state.get('kind') != 'project_session' or state.get('schema_version') != 1
            or any(not isinstance(state.get(key), str) or not state[key]
                   for key in ('project_id', 'title', 'repository', 'before_commit', 'after_commit'))
            or not valid_source_mode_hashes(state.get('source_sha256'))
            or not valid_source_mode_hashes(state.get('source_mode_sha256'))
            or not isinstance(state.get('runtime'), dict)):
        raise ValueError('项目会话记录不完整。')
    collection_path = state.get('collection_path', '')
    if normalize_collection_path(collection_path) != collection_path:
        raise ValueError('项目会话的发现范围记录无效。')
    _check_sources(folder, state)
    state['directory'] = str(folder)
    return state


def _inventory(before, after):
    old = {p.relative_to(before).as_posix(): p for p in before.rglob('*') if p.is_file()}
    new = {p.relative_to(after).as_posix(): p for p in after.rglob('*') if p.is_file()}
    changed = []
    for name in sorted(old.keys() | new.keys()):
        left, right = old.get(name), new.get(name)
        if left and right and _digest(left) == _digest(right) and stat.S_IMODE(left.stat().st_mode) == stat.S_IMODE(right.stat().st_mode):
            continue
        changed.append({'path': name, 'kind': 'added' if not left else 'removed' if not right else 'modified'})
    files, tests = [], []
    for name, path in sorted(new.items()):
        if not name.endswith('.py') or not (path.name.startswith('test_') or path.name.endswith('_test.py')):
            continue
        files.append(name)
        if path.stat().st_size > 512 * 1024:
            continue
        try:
            tree = ast.parse(path.read_bytes())
        except (SyntaxError, ValueError, RecursionError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
                tests.append(name + '::' + node.name)
            elif isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
                tests.extend(name + '::' + node.name + '::' + child.name for child in node.body
                             if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith('test'))
    return changed, files, tests[:10000]


def _recommend(changed, tests):
    changed_names = {item['path'] for item in changed}
    modules = {Path(name).stem for name in changed_names if name.endswith('.py')}
    fixtures = [Path(name).parent for name in changed_names if Path(name).name == 'conftest.py']
    result = {}
    for identity in tests:
        name = identity.split('::', 1)[0]
        path = Path(name)
        module = path.stem.removeprefix('test_').removesuffix('_test')
        if name in changed_names:
            result[identity] = '本次修改了这个测试文件'
        elif module in modules:
            result[identity] = '测试文件与修改模块对应'
        elif any(path.is_relative_to(parent) for parent in fixtures):
            result[identity] = '该目录的 fixture 或测试配置发生变化'
    return result


def prepare_project(repository, before_ref, after_ref, output_root, *, python_executable=None, title=None,
                    issue='', collection_path=''):
    root = repository_root(repository)
    before = resolve_commit(root, before_ref)
    after = WORKTREE if after_ref == WORKTREE else resolve_commit(root, after_ref)
    return _prepare_project(root, before, after, output_root,
                            python_executable=python_executable, title=title, issue=issue,
                            collection_path=collection_path)


def _prepare_project(root, before, after, output_root, *, python_executable, title, issue,
                     source=None, refresh_metadata=None, control=None, collection_path=''):
    collection_path = normalize_collection_path(collection_path)
    output = Path(output_root).expanduser().resolve()
    if output.is_relative_to(root):
        raise ValueError('项目会话须保存在被测仓库之外。')
    if not isinstance(issue, str) or len(issue) > 16000:
        raise ValueError('问题描述最多 16000 字符。')
    runtime = choose_runtime(python_executable)
    identity = _new_id('project')
    output.mkdir(parents=True, exist_ok=True)
    final = output / identity
    with tempfile.TemporaryDirectory(prefix='.project-prepare-', dir=output) as temp:
        folder = Path(temp) / identity
        (folder / 'inputs').mkdir(parents=True)
        old, new = folder / 'inputs/before', folder / 'inputs/after'
        if source is None:
            export_revision(root, before, old)
            before_hash = source_fingerprint(old)[1]
            before_mode = source_mode_fingerprint(old)
        else:
            baseline = Path(source['directory']) / 'inputs/before'
            before_hash = source['source_sha256']['before']
            before_mode = source['source_mode_sha256']['before']
            if snapshot(baseline, old, source_entries(baseline), control=control) != before_hash:
                raise ValueError('复制期间来源基线发生变化，请重新打开来源。')
        if after == WORKTREE:
            after_hash = copy_workspace(root, new, control=control)['sha256']
        else:
            export_revision(root, after, new)
            after_hash = source_fingerprint(new)[1]
        changed, files, tests = _inventory(old, new)
        state = {'schema_version': 1, 'kind': 'project_session', 'project_id': identity,
                 'title': title.strip() if isinstance(title, str) and title.strip() else root.name + ' 项目回归',
                 'issue': issue, 'directory': str(final), 'repository': str(root),
                 'before_commit': before, 'after_commit': after, 'runtime': runtime, 'created_at': _now(),
                 'source_sha256': {'before': before_hash, 'after': after_hash},
                 'source_mode_sha256': {'before': before_mode, 'after': source_mode_fingerprint(new)},
                 'changed_files': changed, 'changes': describe_changes(old, new), 'test_files': files,
                 'static_tests': tests, 'recommendations': _recommend(changed, tests),
                 'collection_path': collection_path, 'collection': None, 'runs': [],
                 'status': 'prepared', 'notice': NOTICE}
        if refresh_metadata is not None:
            state.update(refresh_metadata)
        _check_sources(folder, state)
        save_json(folder / 'project.json', state)
        if control:
            control.commit()
        folder.rename(final)
    return state


def list_projects(root):
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    projects = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not (folder / 'project.json').is_file():
            continue
        try:
            state = _read(folder / 'project.json')
            if state.get('kind') == 'project_session':
                projects.append({key: state.get(key) for key in ('project_id', 'title', 'created_at', 'status', 'runs')}
                                | {'directory': str(folder.resolve())})
        except (OSError, ValueError):
            continue
    return sorted(projects, key=lambda item: item.get('created_at') or '', reverse=True)


def _command(runtime, source, destination, options):
    arguments = [str(source), str(destination), str(options)]
    if getattr(sys, 'frozen', False) and runtime['executable'] == sys.executable:
        return [runtime['executable'], '--reprolab-project-worker', *arguments]
    return [runtime['executable'], '-I', '-X', 'utf8', str(Path(__file__).with_name('_project_worker.py')), *arguments]


def _launch(source, shared_after, selected, folder, label, runtime, timeout, control, *, collecting=False,
            collection_path='', execution_environment=None):
    log_path, event_path = folder / (label + '.log'), folder / (label + '.events.json')
    started = time.monotonic()
    progress_path = folder / (label + '.progress.jsonl')
    atomic_write(progress_path, b'')
    reader = ProgressReader(progress_path, selected, collecting=collecting)
    last_notice = last_event = started
    timed_out = cancelled = False
    with tempfile.TemporaryDirectory(prefix='reprolab-project-') as temp:
        work = Path(temp)
        copied = work / 'source'
        shutil.copytree(source, copied)
        if not collecting:
            for name in dict.fromkeys(parse_test_target(identity) for identity in selected):
                target = copied / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                shutil.copy2(shared_after / name, target)
        options = work / 'options.json'
        profile = read_pytest_profile(shared_after)
        child_env, variables = execution_environment or resolve_variables(profile, control.runtime_values)
        child_env = snapshot_process_environment(child_env, copied, runtime['executable'])
        save_json(options, {'collect_only': collecting, 'selected_tests': selected,
                            'collection_path': collection_path,
                            'pytest_profile': profile, 'pytest_args': profile_args(profile), 'runtime_environment': variables,
                            'progress_path': str(progress_path.resolve())})
        raw_path = work / 'events.json'
        control.check()
        with log_path.open('wb') as log:
            process = spawn(_command(runtime, copied, raw_path, options), cwd=copied,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       env=child_env, start_new_session=(os.name == 'posix'))
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    changed = reader.poll()
                    now = time.monotonic()
                    if changed:
                        last_event = now
                    if changed or now - last_notice >= 1:
                        payload = {**reader.summary(), 'side': label, 'elapsed_seconds': now - started,
                                   'quiet_seconds': now - last_event,
                                   'tests': {identity: _outcome(reader.events.get(identity)) for identity in reader.changed} if reader.valid else {}}
                        control.notify('project_progress', payload)
                        reader.changed.clear()
                        last_notice = now
                    control.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(.1, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                control.check()
            except (RunCancelled, KeyboardInterrupt):
                cancelled = True
                control.cancel()
            finally:
                code = terminate_tree(process)
        try:
            raw = _read(raw_path)
        except (OSError, ValueError):
            raw = {}
    reader.poll(final=True)
    control.progress('project_progress', {**reader.summary(), 'side': label,
        'elapsed_seconds': time.monotonic() - started, 'quiet_seconds': time.monotonic() - last_event,
        'tests': {identity: _outcome(events) for identity, events in reader.events.items()} if reader.valid else {}})
    save_json(event_path, raw)
    with log_path.open('rb') as log:
        size = log.seek(0, 2)
        log.seek(max(0, size - 20000))
        excerpt = log.read().decode('utf-8', 'replace')
    valid = (raw.get('kind') == 'project_worker' and raw.get('schema_version') == 1
             and raw.get('mode') == ('collect' if collecting else 'run')
             and raw.get('requested_tests') == selected and raw.get('exit_code') == code
             and raw.get('collection_path', '') == collection_path
             and raw.get('runtime_environment') == variables
             and isinstance(raw.get('events'), dict) and isinstance(raw.get('collected_tests'), list)
             and all(isinstance(item, str) for item in raw['collected_tests']) and reader.matches(raw))
    return {'status': 'cancelled' if cancelled else 'timed_out' if timed_out else 'completed' if valid else 'worker_error',
            'process_id': process.pid,
            'exit_code': code, 'timed_out': timed_out, 'cancelled': cancelled, 'worker_record_complete': valid,
            'raw': raw, 'progress_schema': 1, 'progress': reader.summary(),
            'observations': reader.events if reader.valid else None,
            'log_excerpt': excerpt, 'log': log_path.name, 'events': event_path.name,
            'log_sha256': _digest(log_path), 'events_sha256': _digest(event_path),
            'duration_seconds': round(time.monotonic() - started, 3),
            'collection_errors': raw.get('collection_errors', []) if valid else ['工作进程没有返回完整记录。'],
            'python_version': raw.get('python_version') or (reader.header or {}).get('python_version'),
            'pytest_version': raw.get('pytest_version') or (reader.header or {}).get('pytest_version'),
            'pytest_profile': raw.get('pytest_profile', (reader.header or {}).get('pytest_profile')),
            'runtime_environment': raw.get('runtime_environment', (reader.header or {}).get('runtime_environment'))}


def collect_project(directory, *, python_executable=None, timeout=120, control=None,
                    execution_environment=None, collection_path=None):
    _timeout(timeout)
    control = control or RunControl()
    control.check()
    with _lock(directory):
        state = read_project(directory)
        if state.get('runner') in ('vitest', 'go'):
            raise ValueError(f"请使用 project {state['runner']} 或 project replay。")
        requested = requested_selection(state)
        folder = Path(state['directory'])
        snapshot = folder / 'inputs/after'
        scope = normalize_collection_path(state.get('collection_path', '') if collection_path is None else collection_path)
        require_collection_target(snapshot, scope)
        runtime = choose_runtime(python_executable or state['runtime']['executable'])
        execution_environment = execution_environment or resolve_variables(read_pytest_profile(snapshot), control.runtime_values)
        collection_dir = folder / 'collections' / _new_id('collection')
        collection_dir.mkdir(parents=True)
        control.notify('project_collect', {'directory': str(folder)})
        result = _launch(snapshot, snapshot, [], collection_dir, 'collection', runtime, timeout, control,
                         collecting=True, collection_path=scope, execution_environment=execution_environment)
        if result['cancelled']:
            raise RunCancelled('已取消项目测试发现。')
        raw = result['raw']
        tests = list(dict.fromkeys(raw.get('collected_tests', []))) if result['worker_record_complete'] else []
        valid = (result['status'] == 'completed' and not raw.get('events') and len(tests) <= 10000
                 and (result['exit_code'] in (0, 5) or result['exit_code'] == 1 and bool(result['collection_errors'])))
        scope_mismatch = False
        for identity in tests:
            try:
                name = parse_test_target(identity)
                if '::' not in identity or len(identity) > 4000 or not (snapshot / name).is_file():
                    valid = False
                if scope and not PurePosixPath(name).is_relative_to(scope):
                    scope_mismatch = True
            except ValueError:
                valid = False
        valid = valid and not scope_mismatch and len(json.dumps(tests).encode()) <= 8 * 1024 * 1024
        status = 'collected' if valid and tests else 'collection_failed'
        record = {'schema_version': 1, 'kind': 'project_collection', **_identity(state),
                  'status': status, 'tests': tests if valid else [], 'count': raw.get('collected', 0),
                  'collection_path': scope, 'scope_mismatch': scope_mismatch,
                  'truncated': bool(raw.get('collection_truncated')), 'runtime': runtime,
                  'pytest_profile': read_pytest_profile(folder / 'inputs/after'),
                  'runtime_environment': execution_environment[1],
                  'progress': result.get('progress'),
                  'worker_status': result['status'], 'exit_code': result['exit_code'],
                  'timed_out': result['timed_out'], 'worker_record_complete': result['worker_record_complete'],
                  'log_excerpt': result['log_excerpt'], 'collection_errors': result['collection_errors'],
                  'partial': bool(result['collection_errors']), 'directory': str(folder),
                  'evidence_dir': str(collection_dir), 'created_at': _now(),
                  'recommendations': _recommend(state['changed_files'], tests if valid else []),
                  'notice': '实际发现会加载可信项目、测试与 conftest，不运行测试主体。' if status == 'collected'
                            else '测试发现未完成，请核对所选环境、项目配置和日志。'}
        record['diagnosis'] = collection_diagnosis(record)
        record['notice'] = record['diagnosis']['summary'] + ' 实际发现会加载可信项目、测试与 conftest，不运行测试主体。'
        if requested is not None and status == 'collected':
            record['selection_review'] = reconcile_selection(requested, record['tests'],
                collection_complete=not (record['partial'] or record['truncated']))
        _check_sources(folder, state)
        save_json(collection_dir / 'collection.json', record)
        state.update(collection=record, collection_sha256=_digest(collection_dir / 'collection.json'),
                     collection_path=scope, status=status)
        state['runtime'] = runtime
        state['collection_record'] = (collection_dir / 'collection.json').relative_to(folder).as_posix()
        state['recommendations'].update(record['recommendations'])
        save_json(folder / 'project.json', state)
        return record


def _collection(state, runtime):
    name = state.get('collection_record')
    if not isinstance(name, str):
        return None
    path = Path(state['directory']) / name
    if not path.resolve().is_relative_to(Path(state['directory'])):
        raise ValueError('测试发现记录路径不正确。')
    record = _read(path)
    if (_digest(path) != state.get('collection_sha256') or record.get('runtime') != runtime
            or any(record.get(key) != value for key, value in _identity(state).items())
            or record.get('collection_path', '') != state.get('collection_path', '')
            or record.get('status') != 'collected' or not isinstance(record.get('tests'), list)):
        return None
    return record


def _outcome(events):
    missing = {'status': 'not_run', 'exception': None, 'phases': [], 'duration_seconds': 0}
    if not isinstance(events, list) or not events:
        return missing
    if len(events) > 3 or any(not isinstance(event, dict) for event in events):
        return {**missing, 'status': 'inconclusive'}
    phases = [event.get('phase') for event in events]
    valid = (phases in (['setup', 'call', 'teardown'], ['setup', 'teardown'])
             and all(event.get('outcome') in ('passed', 'failed', 'skipped') for event in events))
    status = 'inconclusive'
    if valid and not any(event.get('xfail') for event in events):
        if any(event['outcome'] == 'skipped' for event in events):
            status = 'skipped'
        elif phases == ['setup', 'call', 'teardown'] and events[0]['outcome'] == events[2]['outcome'] == 'passed':
            status = events[1]['outcome']
    exception = next((event.get('exception') for event in events if event.get('outcome') == 'failed'), None)
    duration = sum(event.get('duration_seconds', 0) for event in events
                   if type(event.get('duration_seconds')) in (int, float) and math.isfinite(event['duration_seconds']))
    return {'status': status, 'exception': exception, 'phases': events, 'duration_seconds': round(duration, 6)}


def _version(result, selected):
    raw = result.get('raw', {})
    events = raw.get('events', {})
    valid = (result.get('worker_record_complete') is True and isinstance(events, dict)
             and not (set(events) - set(selected))
             and isinstance(raw.get('started_tests'), list)
             and len(raw['started_tests']) == len(set(raw['started_tests']))
             and set(raw['started_tests']) <= set(selected)
             and isinstance(raw.get('selection_guard'), dict)
             and raw['selection_guard'].get('status') == 'allowed')
    outcomes = {identity: _outcome(events.get(identity)) if valid else
                ({**_outcome(None), 'status': 'inconclusive'} if result.get('status') != 'not_run' else _outcome(None))
                for identity in selected}
    status = result.get('status', 'not_run')
    if status == 'completed':
        if not valid or result.get('exit_code') not in (0, 1):
            status = 'worker_error'
        elif result.get('collection_errors'):
            status = 'collection_failed'
        elif result.get('exit_code') == 1 and all(item['status'] == 'passed' for item in outcomes.values()):
            status = 'worker_error'
        elif result.get('exit_code') == 0 and any(item['status'] == 'failed' for item in outcomes.values()):
            status = 'worker_error'
    if status in ('worker_error', 'timed_out'):
        outcomes = {identity: {**item, 'status': 'inconclusive'} for identity, item in outcomes.items()}
    observations = result.get('observations')
    if status in ('worker_error', 'timed_out', 'cancelled') and isinstance(observations, dict):
        for identity in selected:
            observed = _outcome(observations.get(identity))
            outcomes[identity] = {**observed, 'status': 'inconclusive',
                                 'observed_status': observed['status'],
                                 'reason': '本版执行未完整结束；保留已收到的阶段观察，不计为正式通过。'}
    return {key: value for key, value in result.items() if key not in ('raw', 'observations')} | {'status': status, 'tests': outcomes}


def _matrix(before, after, selected, cancelled=False):
    entries, counts = [], {}
    mapping = {('passed', 'passed'): 'regression_passed', ('passed', 'failed'): 'regression_detected',
               ('failed', 'failed'): 'baseline_failure', ('failed', 'passed'): 'improved'}
    for identity in selected:
        left = before.get('tests', {}).get(identity, _outcome(None))
        right = after.get('tests', {}).get(identity, _outcome(None))
        status = mapping.get((left['status'], right['status']), 'inconclusive')
        entries.append({'test_id': identity, 'status': status, 'before': left, 'after': right})
        counts[status] = counts.get(status, 0) + 1
    return _regression_status(before, after, counts, cancelled), entries, counts


def _regression_status(before, after, counts, cancelled=False):
    if cancelled:
        return 'cancelled'
    if counts.get('regression_detected'):
        return 'regression_detected'
    if (counts.get('baseline_failure') or counts.get('inconclusive')
            or before.get('status') != 'completed' or after.get('status') != 'completed'):
        return 'attention'
    return 'regression_passed'


def _copy_session(state, destination):
    source = Path(state['directory'])
    (destination / 'inputs').mkdir(parents=True)
    for label in ('before', 'after'):
        root = source / 'inputs' / label
        entries, digest = source_fingerprint(root)
        if (digest != state['source_sha256'][label]
                or snapshot(root, destination / 'inputs' / label, entries) != digest
                or source_mode_fingerprint(destination / 'inputs' / label) != state['source_mode_sha256'][label]):
            raise ValueError('项目证据复制期间源码或权限发生变化。')
    saved = {**state, 'directory': str(destination), 'runs': []}
    name = state.get('collection_record')
    if isinstance(name, str):
        original = source / name
        if _digest(original) != state.get('collection_sha256'):
            raise ValueError('测试发现记录发生变化。')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    save_json(destination / 'project.json', saved)
    _check_sources(destination, saved)


def _not_run(folder, label, selected):
    atomic_write(folder / (label + '.log'), b'')
    save_json(folder / (label + '.events.json'), {})
    return {'status': 'not_run', 'tests': {identity: _outcome(None) for identity in selected},
            'exit_code': None, 'worker_record_complete': False, 'timed_out': False,
            'collection_errors': [], 'duration_seconds': 0, 'log_excerpt': '',
            'log': label + '.log', 'events': label + '.events.json'}


def _manifest(folder):
    hashes, modes = {}, {}
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise ValueError('报告证据中不能包含符号链接。')
        if path.is_file() and path.relative_to(folder).as_posix() != 'report.json':
            name = path.relative_to(folder).as_posix()
            hashes[name] = _digest(path)
            modes[name] = stat.S_IMODE(path.stat().st_mode) & 0o777
    return {'sha256': hashes, 'file_modes': modes}


def run_project(directory, output_root, *, selected_ids=None, python_executable=None, timeout=120, confirmed=False, control=None):
    if confirmed is not True:
        raise ValueError('请审阅所选测试和固定输入，再明确开始项目回归。')
    _timeout(timeout)
    control = control or RunControl()
    control.check()
    state = read_project(directory)
    if state.get('runner') in ('vitest', 'go'):
        raise ValueError(f"请使用 project {state['runner']} 或 project replay。")
    # Empty selection is rejected before discovery could load project code.
    if selected_ids == [] or (selected_ids is None and requested_selection(state) == []):
        raise ValueError('当前范围已主动清空；正式运行须明确选择 1–2000 项，不会自动全选。')
    runtime = choose_runtime(python_executable or state['runtime']['executable'])
    execution_environment = resolve_variables(read_pytest_profile(Path(state['directory']) / 'inputs/after'), control.runtime_values)
    collection = _collection(state, runtime)
    if collection and (execution_environment[1]['local_names'] or collection.get('runtime_environment', variable_record(None)) != execution_environment[1]):
        collection = None  # Local values are intentionally not fingerprinted or saved. Recheck discovery.
    if collection is None:
        collection = collect_project(directory, python_executable=runtime['executable'], timeout=timeout, control=control.child(progress=control.progress), execution_environment=execution_environment)
        state = read_project(directory)
    if collection['status'] != 'collected':
        raise ValueError('没有可用的真实测试清单，请先完成测试发现。')
    if selected_ids is None:
        requested = requested_selection(state)
        if requested is None and (collection.get('partial') or collection.get('truncated')):
            raise ValueError('发现清单不完整，未自动运行。请处理收集问题后重试，或用 --test / --selection-file 明确本次范围。')
        if requested is not None:
            review = reconcile_selection(requested, collection['tests'],
                collection_complete=not (collection.get('partial') or collection.get('truncated')))
            if review['requires_confirmation']:
                raise ValueError(f'继承选择中有 {review["missing_count"]} 项未找到；未开始两版测试。'
                                 '发现可能不完整，请用 project selection-check 核对，并明确提供要运行的选择清单。')
            selected_ids = review['matched_tests']
    selected = list(dict.fromkeys(collection['tests'] if selected_ids is None else selected_ids))
    if (not 1 <= len(selected) <= MAX_SELECTED or any(not isinstance(item, str) for item in selected)
            or not set(selected) <= set(collection['tests'])):
        raise ValueError('请选择实际发现清单中的 1–2000 个精确测试目标。')
    for identity in selected:
        parse_test_target(identity)
    output = Path(output_root).expanduser().resolve()
    folder = Path(state['directory'])
    if output.is_relative_to(folder) or output.is_relative_to(Path(state['repository']).resolve()):
        raise ValueError('项目报告须保存在原仓库及项目会话之外。')
    with _lock(folder):
        latest = read_project(folder)
        if _identity(latest) != _identity(state) or _collection(latest, runtime) != collection:
            raise ValueError('项目输入或测试发现结果发生变化，请重新选择。')
        report_dir = output / _new_id('project-run')
        report_dir.mkdir(parents=True, exist_ok=False)
        save_json(report_dir / 'operation.json', {'status': 'preparing', 'project_id': state['project_id']})
        evidence = report_dir / 'evidence/project'
        _copy_session(state, evidence)
        control.check()
        save_json(report_dir / 'selected.json', {'selected_tests': selected, 'project_id': state['project_id']})
        shared = {name: _digest(evidence / 'inputs/after' / name)
                  for name in dict.fromkeys(parse_test_target(identity) for identity in selected)}
        before, after = _not_run(report_dir, 'before', selected), _not_run(report_dir, 'after', selected)
        cancelled = False
        try:
            control.notify('project_before', {'total': len(selected), 'selected_tests': selected, 'report_dir': str(report_dir)})
            before = _version(_launch(evidence / 'inputs/before', evidence / 'inputs/after', selected, report_dir,
                                     'before', runtime, timeout, control, execution_environment=execution_environment), selected)
            cancelled = before.get('cancelled', False)
            if not cancelled:
                control.notify('project_after', {'total': len(selected), 'report_dir': str(report_dir), 'before': before})
                after = _version(_launch(evidence / 'inputs/after', evidence / 'inputs/after', selected, report_dir,
                                        'after', runtime, timeout, control, execution_environment=execution_environment), selected)
                cancelled = after.get('cancelled', False)
            if not cancelled:
                control.commit()
        except RunCancelled:
            cancelled = True
        _check_sources(evidence, state)
        _check_sources(folder, state)
        status, entries, counts = _matrix(before, after, selected, cancelled)
        report = {'schema_version': 1, 'kind': 'project_regression', 'run_id': report_dir.name,
                  'reprolab_version': __version__,
                  'project_id': state['project_id'], 'project_directory': str(folder), 'title': state['title'],
                  'issue': state.get('issue', ''), 'repository': state['repository'],
                  'before_commit': state['before_commit'], 'after_commit': state['after_commit'],
                  'status': status, 'report_dir': str(report_dir), 'selected_tests': selected,
                  'counts': counts, 'entries': entries, 'before': before, 'after': after,
                  'source_sha256': state['source_sha256'], 'source_mode_sha256': state['source_mode_sha256'],
                  'shared_tests': shared, 'changes': state['changes'], 'changed_files': state['changed_files'],
                  'pytest_profile': read_pytest_profile(evidence / 'inputs/after'),
                  'runtime': runtime, 'environment': snapshot_environment(runtime['executable']),
                  'runtime_environment': execution_environment[1],
                  'refresh_origin': state.get('refresh_origin'),
                  'review_source': state.get('review_source'),
                  'collection': {**{key: collection.get(key) for key in ('count', 'partial', 'collection_errors', 'truncated')},
                                 'collection_path': collection.get('collection_path', '')},
                  'timeout': timeout, 'created_at': _now(), 'notice': NOTICE}
        execution = {'runtime_environment': execution_environment[1], 'selected_tests': selected, 'cancelled': cancelled, 'project_id': state['project_id'],
                     'before': {key: before.get(key) for key in ('exit_code', 'status', 'timed_out', 'cancelled', 'duration_seconds', 'process_id', 'progress_schema') if key in before},
                     'after': {key: after.get(key) for key in ('exit_code', 'status', 'timed_out', 'cancelled', 'duration_seconds', 'process_id', 'progress_schema') if key in after}}
        save_json(report_dir / 'execution.json', execution)
        save_json(report_dir / 'operation.json', {'status': status, 'run_id': report_dir.name, 'phase': 'saved'})
        from .project_report import write_project_report
        write_project_report(report)
        save_json(report_dir / 'catalog.json', {key: report[key] for key in
                  ('kind', 'title', 'status', 'created_at', 'project_id', 'run_id')})
        report['persistence'] = _manifest(report_dir)
        save_json(report_dir / 'report.json', report)
        # The published report stands on its own even if the history index fails.
        try:
            latest = _read(folder / 'project.json')
            latest['runs'] = [*latest.get('runs', []), {key: report[key] for key in ('run_id', 'status', 'report_dir', 'created_at')}][-200:]
            latest['status'] = status
            save_json(folder / 'project.json', latest)
        except (OSError, ValueError) as exc:
            report['history_warning'] = '报告已保存，但会话历史更新失败：' + str(exc)
        if not cancelled:
            control.notify('project_done', {'report_dir': str(report_dir), 'status': status})
        return report


def _saved_version(folder, label, meta, selected):
    raw = _read(folder / (label + '.events.json'))
    result = {**meta, 'raw': raw, 'worker_record_complete': (
        raw.get('kind') == 'project_worker' and raw.get('schema_version') == 1 and raw.get('mode') == 'run'
        and raw.get('requested_tests') == selected and raw.get('exit_code') == meta.get('exit_code')
        and isinstance(raw.get('events'), dict) and isinstance(raw.get('collected_tests'), list)),
        'collection_errors': raw.get('collection_errors', []), 'log_excerpt': '',
        'python_version': raw.get('python_version'), 'pytest_version': raw.get('pytest_version'),
        'pytest_profile': raw.get('pytest_profile'), 'runtime_environment': raw.get('runtime_environment')}
    with (folder / (label + '.log')).open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - 20000))
        result['log_excerpt'] = stream.read().decode('utf-8', 'replace')
    if meta.get('progress_schema') == 1:
        reader = ProgressReader(folder / (label + '.progress.jsonl'), selected)
        reader.poll(final=True)
        result['worker_record_complete'] = result['worker_record_complete'] and reader.matches(raw)
        result['progress'] = reader.summary()
        result['observations'] = reader.events if reader.valid else None
        result['python_version'] = raw.get('python_version') or (reader.header or {}).get('python_version')
        result['pytest_version'] = raw.get('pytest_version') or (reader.header or {}).get('pytest_version')
        result['pytest_profile'] = raw.get('pytest_profile', (reader.header or {}).get('pytest_profile'))
        result['runtime_environment'] = raw.get('runtime_environment', (reader.header or {}).get('runtime_environment'))
    return _version(result, selected)


def check_project_report(directory, *, control=None):
    folder = Path(directory).expanduser().resolve()
    if folder.is_file():
        folder = folder.parent
    issues = []
    try:
        report = _read(folder / 'report.json')
        if report.get('kind') != 'project_regression' or report.get('schema_version') != 1:
            raise ValueError('请选择项目回归报告目录。')
        hashes = report.get('persistence', {}).get('sha256')
        modes = report.get('persistence', {}).get('file_modes', {})
        required = {'report.html', 'execution.json', 'selected.json', 'before.log', 'after.log',
                    'before.events.json', 'after.events.json', 'evidence/project/project.json'}
        if not isinstance(hashes, dict) or not required <= set(hashes):
            raise ValueError('项目报告的必要证据清单不完整。')
        for name, digest in hashes.items():
            if control:
                control.check()
            path = folder / name
            if not path.resolve().is_relative_to(folder) or path.is_symlink() or not path.is_file() or _digest(path) != digest:
                raise ValueError('报告证据内容不一致：' + name)
            if name in modes and stat.S_IMODE(path.stat().st_mode) & 0o777 != modes[name]:
                raise ValueError('报告证据普通文件权限不一致：' + name)
        state = read_project(folder / 'evidence/project')
        if any(report.get(key) != value for key, value in _identity(state).items()):
            raise ValueError('报告与固定项目输入不一致。')
        if (report.get('review_source') != state.get('review_source')
                or report.get('refresh_origin') != state.get('refresh_origin')):
            raise ValueError('报告来源记录与保存的会话证据不一致。')
        for key in ('before_commit', 'after_commit'):
            if report.get(key) not in (None, '') and report[key] != state.get(key):
                raise ValueError('报告基线或版本记录与固定会话不一致。')
        execution = _read(folder / 'execution.json')
        for label in ('before', 'after'):
            if execution.get(label, {}).get('progress_schema') == 1 and label + '.progress.jsonl' not in hashes:
                raise ValueError('缺少逐项进度证据：' + label)
        selected = _read(folder / 'selected.json').get('selected_tests')
        if (selected != report.get('selected_tests') or execution.get('selected_tests') != selected
                or not isinstance(selected, list) or not 1 <= len(selected) <= MAX_SELECTED):
            raise ValueError('报告所选测试清单不一致。')
        if report.get('runner') == 'vitest':
            from .vitest_project import check_vitest_evidence
            check_vitest_evidence(folder, report, state, execution, selected)
            return {'status': 'consistent', 'report_dir': str(folder), 'issues': [],
                    'notice': '项目证据一致性与测试结果分别判断；校验值不是身份认证。'}
        if report.get('runner') == 'go':
            from .go_project import check_go_evidence
            check_go_evidence(folder, report, state, execution, selected)
            return {'status': 'consistent', 'report_dir': str(folder), 'issues': [],
                    'notice': '项目证据一致性与测试结果分别判断；校验值不是身份认证。'}
        scope = (report.get('collection') or {}).get('collection_path', '')
        if scope != state.get('collection_path', ''):
            raise ValueError('报告发现范围与固定会话不一致。')
        if scope:
            collection = _collection(state, report.get('runtime'))
            if (collection is None or not set(selected) <= set(collection['tests'])
                    or any(not PurePosixPath(parse_test_target(identity)).is_relative_to(scope) for identity in selected)):
                raise ValueError('报告所选测试与保存的发现范围不一致。')
        shared = report.get('shared_tests')
        # Missing optional caches in old records remain unknown in comparisons.
        # All mandatory frozen source and execution evidence is still checked.
        if shared is not None:
            if not isinstance(shared, dict) or set(shared) != {parse_test_target(identity) for identity in selected}:
                raise ValueError('共享测试清单不完整。')
            if any(_digest(folder / 'evidence/project/inputs/after' / name) != digest for name, digest in shared.items()):
                raise ValueError('共享测试内容不一致。')
        if 'pytest_profile' in report and report['pytest_profile'] != read_pytest_profile(folder / 'evidence/project/inputs/after'):
            raise ValueError('报告的 pytest 运行配置与固定输入不一致。')
        variables = variable_record(read_pytest_profile(folder / 'evidence/project/inputs/after'))
        if 'runtime_environment' in report or variables['values'] or variables['local_names']:
            if report.get('runtime_environment') != variables or execution.get('runtime_environment') != variables:
                raise ValueError('运行变量记录与固定输入或执行记录不一致。')
        before = _saved_version(folder, 'before', execution['before'], selected)
        after = _saved_version(folder, 'after', execution['after'], selected)
        fields = ('status', 'exit_code', 'timed_out', 'tests', 'duration_seconds',
                  'worker_record_complete', 'python_version', 'pytest_version', 'log_excerpt')
        for label, actual in (('before', before), ('after', after)):
            cached = report.get(label, {})
            if execution[label].get('progress_schema') == 1 and cached.get('progress') != actual.get('progress'):
                raise ValueError('报告进度摘要与原始事件不一致：' + label)
            if 'pytest_profile' in cached and cached['pytest_profile'] != actual.get('pytest_profile'):
                raise ValueError('报告的 pytest 运行配置与执行记录不一致：' + label)
            if 'runtime_environment' in report:
                if cached.get('runtime_environment') != actual.get('runtime_environment'):
                    raise ValueError('运行变量与工作进程记录不一致：' + label)
                if actual.get('worker_record_complete') and actual.get('runtime_environment') != variables:
                    raise ValueError('工作进程未使用声明的运行变量：' + label)
            if any(cached.get(key) != actual.get(key) for key in fields):
                raise ValueError('报告版本摘要与原始证据不一致：' + label)
        status, entries, counts = _matrix(before, after, selected, execution.get('cancelled') is True)
        if (status != report.get('status') or counts != report.get('counts')
                or entries != report.get('entries')):
            raise ValueError('报告结论或逐项结果与原始执行证据不一致。')
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        issues.append(str(exc))
    return {'status': 'inconsistent' if issues else 'consistent', 'report_dir': str(folder),
            'issues': issues, 'notice': '项目证据一致性与测试结果分别判断；校验值不是身份认证。'}


def recorded_project_timeout(report):
    try:
        return _timeout(report.get('timeout'))
    except ValueError:
        return None


def project_replay_settings(report, *, python_executable=None, timeout=None):
    source_timeout = recorded_project_timeout(report)
    return {'source_runtime': {'executable': (report.get('runtime') or {}).get('executable')},
            'source_timeout': source_timeout,
            'runtime': {'executable': str(python_executable or sys.executable)},
            'timeout': _timeout(timeout if timeout is not None else source_timeout or 120)}


def replay_project_report(directory, output_root, *, python_executable=None, timeout=None, confirmed=False,
                          control=None, node=None, npm=None, offline=None, npm_cache=None, go=None):
    checked = check_project_report(directory)
    if checked['status'] != 'consistent':
        raise ValueError('项目报告证据不一致：' + '; '.join(checked['issues']))
    folder = Path(checked['report_dir'])
    report = _read(folder / 'report.json')
    if Path(output_root).expanduser().resolve().is_relative_to(folder):
        raise ValueError('重放输出须保存在来源报告之外。')
    if report.get('runner') == 'vitest':
        from .vitest_project import replay_vitest_project
        if python_executable is not None or go is not None:
            raise ValueError('Vitest 重放使用 --node 和 --npm 选择运行环境。')
        return replay_vitest_project(folder, report, output_root, node=node, npm=npm, timeout=timeout,
                                     offline=offline, npm_cache=npm_cache, confirmed=confirmed, control=control)
    if report.get('runner') == 'go':
        from .go_project import replay_go_project
        if any(value is not None for value in (python_executable, node, npm, offline, npm_cache)):
            raise ValueError('Go 重放使用 --go 选择运行环境。')
        return replay_go_project(folder, report, output_root, go=go, timeout=timeout,
                                 confirmed=confirmed, control=control)
    if any(value is not None for value in (node, npm, offline, npm_cache, go)):
        raise ValueError('此报告使用 pytest，请通过 --python 选择运行环境。')
    replay_profile = read_pytest_profile(folder / 'evidence/project/inputs/after')
    variables = variable_record(replay_profile)
    settings = project_replay_settings(report, python_executable=python_executable, timeout=timeout)
    preview = {**settings, 'runtime_environment': variables, 'required_local_variables': variables['local_names'],
               'status': 'ready', 'kind': 'project_replay_preview', 'title': report['title'],
               'report_dir': str(folder), 'selected_tests': report['selected_tests'], 'count': len(report['selected_tests']),
               'collection_path': (report.get('collection') or {}).get('collection_path', ''),
               'notice': '已校验证据；明确重放后将使用所选环境重新运行，不覆盖来源报告。'}
    if confirmed is not True:
        return preview
    resolve_variables(replay_profile, getattr(control, 'runtime_values', None))
    source = read_project(folder / 'evidence/project')
    clone = Path(output_root).expanduser().resolve() / 'replay-projects' / _new_id('project-replay')
    clone.mkdir(parents=True, exist_ok=False)
    _copy_session(source, clone)
    return run_project(clone, output_root, selected_ids=report['selected_tests'],
                       python_executable=settings['runtime']['executable'], timeout=settings['timeout'],
                       confirmed=True, control=control)


def prepare_project_ai(directory, issue, output_root, *, python_executable=None):
    """Prepare the existing AI workflow from this session's exact frozen inputs."""
    from .repair_suite import _context, _suite_prompt, _checked_state
    if not isinstance(issue, str) or not issue.strip() or len(issue) > 16000:
        raise ValueError('AI 计划需要 1–16000 字符的问题与期望描述。')
    state = read_project(directory)
    output = Path(output_root).expanduser().resolve()
    if (output.is_relative_to(Path(state['directory']))
            or output.is_relative_to(Path(state['repository']).resolve())):
        raise ValueError('AI 计划须保存在原仓库及项目会话之外。')
    runtime = choose_runtime(python_executable or state['runtime']['executable'])
    output.mkdir(parents=True, exist_ok=True)
    identity = _new_id('suite')
    final = output / identity
    with tempfile.TemporaryDirectory(prefix='.project-ai-', dir=output) as temp:
        folder = Path(temp) / identity
        (folder / 'inputs').mkdir(parents=True)
        for label in ('before', 'after'):
            source = Path(state['directory']) / 'inputs' / label
            entries, digest = source_fingerprint(source)
            if (snapshot(source, folder / 'inputs' / label, entries) != state['source_sha256'][label]
                    or source_mode_fingerprint(folder / 'inputs' / label) != state['source_mode_sha256'][label]):
                raise ValueError('AI 计划复制期间固定输入发生变化。')
        context = _context(folder / 'inputs/before', folder / 'inputs/after', issue)
        context.update(repository=Path(state['repository']).name, before_commit=state['before_commit'], after_commit=state['after_commit'])
        save_json(folder / 'context.json', context)
        atomic_write(folder / 'prompt.txt', _suite_prompt(context))
        prepared = {'schema_version': 1, 'kind': 'ai_suite', 'status': 'prepared', 'suite_id': identity,
                    'directory': str(final), 'repository': state['repository'], 'issue': issue, 'runtime': runtime,
                    'before_commit': state['before_commit'], 'after_commit': state['after_commit'],
                    'source_sha256': state['source_sha256'], 'source_mode_sha256': state['source_mode_sha256'],
                    'context_sha256': _digest(folder / 'context.json'), 'prompt_sha256': _digest(folder / 'prompt.txt'),
                    'project_origin': {'project_id': state['project_id']},
                    'notice': '已从项目工作台的固定输入准备 AI 计划，审阅发送内容后再生成检查。'}
        save_json(folder / 'suite.json', prepared)
        _check_sources(Path(state['directory']), state)
        _checked_state(folder)
        folder.rename(final)
    return _checked_state(final)[1]
