from .process_tree import spawn, terminate_tree
from .platform_support import exclusive_lock
"""Frozen, persistent project regression sessions with one pytest process per version."""
import ast
import errno
from contextlib import contextmanager, nullcontext
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
from ._clock_control import validate_clock
from ._fixture_bindings import checked_fixture_bindings
from ._input_samples import ERRORS as INPUT_ERRORS, read_sample_observations
from .case import parse_test_target, valid_source_mode_hashes
from .changes import describe_changes
from .environment import snapshot_environment
from .git_source import excluded_tracked_changes, export_revision, resolve_commit
from .persistence import save_json, atomic_write
from .project_selections import reconcile_selection, requested_selection
from .project_archive import pack_project_report, source_zip_preflight, unpack_project_report
from .recipes import repository_root
from .repair_suite import _new_id
from .runner import source_entries, source_fingerprint, source_mode_fingerprint, snapshot, RunControl, RunCancelled
from .runtime import snapshot_process_environment, choose_runtime
from .pytest_profile import read_pytest_profile, profile_args
from .project_progress import ProgressReader
from .project_fixture import changed_conftests, check_fixture_record, fixture_record
from .project_input_control import CONTROL_FILE, control_metadata, select_failure_inputs
from .project_pr_context import validate_pr_context
from .project_snapshot_scope import normalize_source_scope
from .project_test_source import validate_test_source_control
from .project_order import order_digest, validate_order_control
from .project_version_order import PAIR_STATE_ENV, validate_version_order
from .project_collection import collection_diagnosis, normalize_collection_path, require_collection_target
from .project_recommendations import recommend_project_tests
from .runtime_environment import resolve_variables, variable_record
from .workspace import WORKTREE, copy_workspace

MAX_SELECTED = 2000
MAX_RECORD_BYTES = 128 * 1024 * 1024
NOTICE = ('项目回归结果覆盖本次所选测试。两版在独立副本中使用修复后版本的同一份目标测试文件，'
          '各版本的项目配置和 fixture 分别保留。执行前后各一个 pytest 进程；原始源码证据保持完整。')


def _read(path, *, expected_sha256=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError(f'记录不存在、过大或不是普通文件：{path.name}')
    try:
        if expected_sha256 is None:
            data = json.loads(path.read_bytes())
        else:
            with path.open('rb') as stream:
                payload = stream.read(MAX_RECORD_BYTES + 1)
            if len(payload) > MAX_RECORD_BYTES or hashlib.sha256(payload).hexdigest() != expected_sha256:
                raise ValueError('证据指纹不一致。')
            data = json.loads(payload)
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
    for key in ('runner', 'excluded_tracked_changes', 'archive_preflight', 'snapshot_scope', 'pr_context'):
        if key in state:
            identity[key] = state[key]
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
    excluded = state.get('excluded_tracked_changes')
    if excluded is not None and (type(excluded) is not int or excluded < 0):
        raise ValueError('项目会话的快照排除记录无效。')
    if state.get('snapshot_scope') != normalize_source_scope(state.get('snapshot_scope')):
        raise ValueError('会话源码路径范围无效。')
    validate_pr_context(state.get('pr_context'), state['before_commit'], state['after_commit'])
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


def prepare_project(repository, before_ref, after_ref, output_root, *, python_executable=None, title=None,
                    issue='', collection_path='', snapshot_scope=None, pr_context=None):
    root = repository_root(repository)
    before = resolve_commit(root, before_ref)
    after = WORKTREE if after_ref == WORKTREE else resolve_commit(root, after_ref)
    return _prepare_project(root, before, after, output_root,
                            python_executable=python_executable, title=title, issue=issue,
                            collection_path=collection_path, snapshot_scope=snapshot_scope, pr_context=pr_context)


def _prepare_project(root, before, after, output_root, *, python_executable, title, issue,
                     source=None, refresh_metadata=None, control=None, collection_path='', snapshot_scope=None, pr_context=None):
    collection_path = normalize_collection_path(collection_path)
    scope = normalize_source_scope(source.get('snapshot_scope') if source is not None else snapshot_scope)
    validate_pr_context(pr_context, before, after)
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
            export_revision(root, before, old, snapshot_scope=scope)
            before_hash = source_fingerprint(old)[1]
            before_mode = source_mode_fingerprint(old)
        else:
            baseline = Path(source['directory']) / 'inputs/before'
            before_hash = source['source_sha256']['before']
            before_mode = source['source_mode_sha256']['before']
            if snapshot(baseline, old, source_entries(baseline), control=control) != before_hash:
                raise ValueError('复制期间来源基线发生变化，请重新打开来源。')
        if after == WORKTREE:
            after_hash = copy_workspace(root, new, control=control, snapshot_scope=scope)['sha256']
        else:
            export_revision(root, after, new, snapshot_scope=scope)
            after_hash = source_fingerprint(new)[1]
        changed, files, tests = _inventory(old, new)
        excluded = excluded_tracked_changes(root, before, None if after == WORKTREE else after,
                                            saved_baseline=source is not None, snapshot_scope=scope)
        state = {'schema_version': 1, 'kind': 'project_session', 'project_id': identity,
                 'excluded_tracked_changes': excluded,
                 **({'snapshot_scope': scope} if scope else {}),
                 **({'pr_context': pr_context} if pr_context else {}),
                 'archive_preflight': source_zip_preflight(folder / 'inputs', control=control),
                 'title': title.strip() if isinstance(title, str) and title.strip() else root.name + ' 项目回归',
                 'issue': issue, 'directory': str(final), 'repository': str(root),
                 'before_commit': before, 'after_commit': after, 'runtime': runtime, 'created_at': _now(),
                 'source_sha256': {'before': before_hash, 'after': after_hash},
                 'source_mode_sha256': {'before': before_mode, 'after': source_mode_fingerprint(new)},
                 'changed_files': changed, 'changes': describe_changes(old, new), 'test_files': files,
                 'static_tests': tests, 'recommendations': recommend_project_tests(changed, tests, old, new, control=control),
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


def scan_project_runs(directory, report_roots, *, rebuild=False, control=None):
    """Recover pytest history from checked standalone reports; session runs are a cache."""
    from .project_history import _checked_report

    state = read_project(directory)
    folder = Path(state['directory'])
    roots = [Path(path).expanduser().resolve() for path in report_roots]
    if any(not root.is_dir() for root in roots):
        raise ValueError('请选择有效报告根目录。')
    paths = {path.parent for root in roots for path in root.rglob('report.json')
             if not path.is_symlink() and path.resolve().is_relative_to(root)}
    cached = [Path(item['report_dir']).expanduser().resolve() for item in state.get('runs', [])
              if isinstance(item, dict) and isinstance(item.get('report_dir'), str)]
    missing_cached = sum(not (path / 'report.json').is_file() for path in cached)
    paths.update(path for path in cached if (path / 'report.json').is_file())
    runs, issues = {}, []
    for path in sorted(paths):
        if control:
            control.check()
        try:
            hint = _read(path / 'report.json')
            if hint.get('project_id') != state['project_id']:
                continue
            report = _checked_report(path, control=control)
            if _identity(report) != _identity(state):
                raise ValueError('报告与会话固定输入不一致。')
            old = runs.get(report['run_id'])
            if old and old['report_sha256'] != report['_report_sha256']:
                raise ValueError('同一运行 ID 的报告指纹不一致。')
            row = {key: report[key] for key in ('run_id', 'status', 'report_dir', 'created_at')}
            runs[report['run_id']] = {**row, 'report_sha256': report['_report_sha256']}
        except (OSError, ValueError, KeyError) as exc:
            issues.append({'report_dir': str(path), 'issue': str(exc)})
    rows = sorted(runs.values(), key=lambda row: (row['created_at'], row['run_id']))
    if rebuild and not issues:
        with _lock(folder):
            latest = read_project(folder)
            if _identity(latest) != _identity(state) or latest.get('runs') != state.get('runs'):
                raise ValueError('扫描期间会话历史发生变化，请重新扫描。')
            latest['runs'] = [{key: value for key, value in row.items() if key != 'report_sha256'}
                              for row in rows[-200:]]
            if rows:
                latest['status'] = rows[-1]['status']
            if control:
                control.commit()
            save_json(folder / 'project.json', latest)
    return {'kind': 'project_run_history', 'schema_version': 1, 'project_id': state['project_id'],
            'runs': rows, 'issues': issues, 'missing_cached_reports': missing_cached, 'index_rebuilt': rebuild and not issues}


def _command(runtime, source, destination, options):
    arguments = [str(source), str(destination), str(options)]
    if getattr(sys, 'frozen', False) and runtime['executable'] == sys.executable:
        return [runtime['executable'], '--reprolab-project-worker', *arguments]
    return [runtime['executable'], '-I', '-X', 'utf8', str(Path(__file__).with_name('_project_worker.py')), *arguments]


def _watch_project_modules(snapshots, profile):
    """Watch configured names plus regular modules in the common root and src layouts."""
    names = set(profile.get('snapshot_modules', ()))
    for side in ('before', 'after'):
        tree = snapshots / side
        for root in (tree, tree / 'src'):
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if path.suffix == '.py' and path.is_file() and path.stem.isidentifier() and path.stem != '__init__':
                    names.add(path.stem)
                elif path.name.isidentifier() and path.is_dir() and (path / '__init__.py').is_file():
                    names.add(path.name)
    return sorted(names)


def _pytest_execution_context(raw, reader, complete):
    header = reader.header if reader and reader.valid else None
    rules = (header or {}).get('pytest_rules') or (raw.get('pytest_rules') if complete else None)
    if rules is None:
        return {}
    result = {'pytest_rules': rules}
    if header and reader.mode == 'run':
        result['execution_order'] = reader.started
    if complete and raw.get('mode') == 'run':
        result['module_origins'] = raw['module_origins']
        if 'runtime_context' in raw:
            result['runtime_context'] = raw['runtime_context']
        if 'worker_resources' in raw:
            result['worker_resources'] = raw['worker_resources']
    return result


def _launch(source, shared_after, selected, folder, label, runtime, timeout, control, *, collecting=False,
            collection_path='', execution_environment=None, watched_modules=(), fixed_order=None, pair_state=None, baseline_fixtures=None, input_values=None, shared_tests_from=None,
            record_fixture_bindings=False):
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
                shutil.copy2((shared_tests_from or shared_after) / name, target)
        fixture_paths = []
        if baseline_fixtures is not None:
            baseline, names = baseline_fixtures
            for name in names:
                target = copied / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                shutil.copy2(baseline / name, target)
            fixture_paths = names
        options = work / 'options.json'
        profile = read_pytest_profile(shared_after)
        child_env, variables = execution_environment or resolve_variables(profile, control.runtime_values)
        child_env = snapshot_process_environment(child_env, copied, runtime['executable'])
        if pair_state is not None:
            child_env[PAIR_STATE_ENV] = pair_state
        save_json(options, {'collect_only': collecting, 'selected_tests': selected,
                            'collection_path': collection_path,
                            'record_fixture_bindings': record_fixture_bindings,
                            'pytest_profile': profile, 'pytest_args': profile_args(profile), 'runtime_environment': variables,
                            'progress_path': str(progress_path.resolve()), 'watched_modules': list(watched_modules),
                            'clock_control': control.clock_control, 'fixed_order': fixed_order, 'fixture_paths': fixture_paths, 'input_values': input_values})
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
             and raw.get('clock_control') == control.clock_control
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
            'runtime_environment': raw.get('runtime_environment', (reader.header or {}).get('runtime_environment')),
            **_pytest_execution_context(raw, reader, valid)}


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
                         collecting=True, collection_path=scope, execution_environment=execution_environment,
                         record_fixture_bindings=True)
        if result['cancelled']:
            raise RunCancelled('已取消项目测试发现。')
        raw = result['raw']
        tests, valid, scope_mismatch = _discovery_tests(result, snapshot, scope)
        bindings = checked_fixture_bindings(raw.get('fixture_bindings'), tests, snapshot) if valid else None
        status = 'collected' if valid and tests else 'collection_failed'
        record = {'schema_version': 1, 'kind': 'project_collection', **_identity(state),
                  'status': status, 'tests': tests if valid else [], 'count': raw.get('collected', 0),
                  'collection_path': scope, 'scope_mismatch': scope_mismatch,
                  'truncated': bool(raw.get('collection_truncated')), 'runtime': runtime,
                  'pytest_profile': read_pytest_profile(folder / 'inputs/after'),
                  'runtime_environment': execution_environment[1],
                  'progress': result.get('progress'),
                  **({'pytest_rules': result['pytest_rules']} if 'pytest_rules' in result else {}),
                  'worker_status': result['status'], 'exit_code': result['exit_code'],
                  'timed_out': result['timed_out'], 'worker_record_complete': result['worker_record_complete'],
                  'log_excerpt': result['log_excerpt'], 'collection_errors': result['collection_errors'],
                  'partial': bool(result['collection_errors']), 'directory': str(folder),
                  'evidence_dir': str(collection_dir), 'created_at': _now(),
                  **({'fixture_bindings': bindings} if bindings is not None else {}),
                  'recommendations': recommend_project_tests(state['changed_files'], tests if valid else [],
                                                              folder / 'inputs/before', snapshot, control=control,
                                                              fixture_bindings=bindings),
                  'notice': '实际发现会加载可信项目、测试与 conftest，不运行测试主体。' if status == 'collected'
                            else '测试发现未完成，请核对所选环境、项目配置和日志。'}
        if control.clock_control is not None:
            record['clock_control'] = control.clock_control
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
        if valid:
            state['recommendations'] = record['recommendations']
        save_json(folder / 'project.json', state)
        return record


def _discovery_tests(result, snapshot, scope):
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
    return tests, valid, scope_mismatch


def _baseline_discovery(folder, snapshot, scope, runtime, timeout, control):
    try:
        require_collection_target(snapshot, scope)
        profile = read_pytest_profile(snapshot)
        environment = resolve_variables(profile, control.runtime_values)
    except ValueError as exc:
        return {'status': 'unavailable', 'reason': str(exc)}
    result = _launch(snapshot, snapshot, [], folder, 'baseline-discovery', runtime, timeout, control,
                     collecting=True, collection_path=scope, execution_environment=environment)
    return {key: result[key] for key in ('status', 'exit_code', 'worker_record_complete', 'collection_errors')}


def _discovery_audit(folder, baseline, collection, selected):
    snapshots = folder / 'evidence/project/inputs'
    scope = collection.get('collection_path', '')
    if baseline['status'] == 'unavailable':
        raw = {}
    else:
        raw = _read(folder / 'baseline-discovery.events.json')
    tests, valid, _ = _discovery_tests({**baseline, 'raw': raw, 'worker_record_complete':
                                      baseline.get('worker_record_complete', False)}, snapshots / 'before', scope)
    before_complete = valid and not raw.get('collection_truncated') and not baseline.get('collection_errors')
    after_complete = not (collection.get('partial') or collection.get('truncated'))
    before, after = (set(tests) if valid else set()), set(collection['tests'])
    missing, added = sorted(before - after), sorted(after - before)
    by_suffix, by_file, missing_files = {}, {}, {}
    if before_complete and after_complete:
        for identity in added:
            file, suffix = identity.split('::', 1)
            by_suffix.setdefault(suffix, []).append(identity)
            by_file.setdefault(file, []).append(identity)
        for identity in missing:
            file = parse_test_target(identity)
            missing_files[file] = missing_files.get(file, 0) + 1
    baseline_only = []
    if before_complete and after_complete:
        for identity in missing:
            file = parse_test_target(identity)
            possible = by_suffix.get(identity.split('::', 1)[1], [])
            if not possible and missing_files[file] == 1 and len(by_file.get(file, ())) == 1:
                possible = by_file[file]
            reason = ('possible_rename' if possible else 'source_file_absent'
                      if not (snapshots / 'after' / file).is_file() else 'not_discovered')
            baseline_only.append({'test_id': identity, 'reason': reason, 'possible_matches': possible[:10]})
    return {'status': 'complete' if before_complete and after_complete else 'incomplete',
            'collection_path': scope, 'before_complete': before_complete, 'after_complete': after_complete,
            'before_count': len(before), 'after_count': len(after), 'baseline_status': baseline['status'],
            'baseline_only': baseline_only, 'candidate_only_count': len(added) if before_complete and after_complete else None,
            'unselected_common_count': len((before & after) - set(selected)) if before_complete and after_complete else None}


def _collection(state, runtime):
    name = state.get('collection_record')
    if not isinstance(name, str):
        return None
    path = Path(state['directory']) / name
    if not path.resolve().is_relative_to(Path(state['directory'])):
        raise ValueError('测试发现记录路径不正确。')
    record = _read(path)
    if _digest(path) != state.get('collection_sha256') or not _matches_collection(record, state, runtime):
        return None
    return record


def _matches_collection(record, state, runtime):
    return (record.get('runtime') == runtime and all(record.get(key) == value for key, value in _identity(state).items())
            and record.get('collection_path', '') == state.get('collection_path', '')
            and record.get('status') == 'collected' and isinstance(record.get('tests'), list))


def saved_report_collection(report):
    """Read the discovery record actually bound to this saved report, if available."""
    prefix = 'evidence/project/'
    try:
        root = Path(report['report_dir']).resolve()
        hashes = report['persistence']['sha256']
        state_name = prefix + 'project.json'
        state_path = root / state_name
        state_digest = hashes[state_name]
        if not isinstance(state_digest, str) or not state_path.resolve().is_relative_to(root):
            return None
        state = _read(state_path, expected_sha256=state_digest)
        name = state['collection_record']
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or name != relative.as_posix()
                or any(char in name for char in (':', '\\'))):
            return None
        path = root / prefix / name
        digest = hashes[prefix + name]
        if (not isinstance(digest, str) or digest != state.get('collection_sha256')
                or not path.resolve().is_relative_to(root / prefix)
                or any(report.get(key) != value for key, value in _identity(state).items())):
            return None
        record = _read(path, expected_sha256=digest)
        if (not _matches_collection(record, state, report['runtime'])
                or record.get('clock_control') != report.get('clock_control')
                or ('runtime_environment' in record
                    and record['runtime_environment'] != report.get('runtime_environment'))):
            return None
        tests = record['tests']
        if any(not isinstance(test, str) for test in tests) or not set(report['selected_tests']) <= set(tests):
            return None
        return record
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return None


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


def _version(result, selected, expected_inputs=None, *, include_resources=False):
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
    if status == 'completed':
        parameters = raw.get('parameter_digests', {})
        if not isinstance(parameters, dict) or not parameters.keys() <= set(selected):
            raise ValueError('参数化输入记录与所选测试不一致。')
        for identity, fingerprints in parameters.items():
            if (not isinstance(fingerprints, dict) or not 1 <= len(fingerprints) <= 64
                    or any(value is not None and (not isinstance(value, str) or len(value) != 64
                                                   or any(char not in '0123456789abcdef' for char in value))
                           for value in fingerprints.values())):
                raise ValueError('参数化输入指纹无效。')
            outcomes[identity]['parameter_digests'] = fingerprints
        digests, errors = read_sample_observations(raw, selected)
        expected = expected_inputs or {}
        for identity in dict.fromkeys([*digests, *errors, *expected]):
            outcome = outcomes[identity]
            actual = digests.get(identity)
            error = errors.get(identity)
            if identity in expected and actual != expected[identity] and not error:
                error = 'replay_mismatch'
            if actual:
                outcome['input_digests'] = actual
            if error:
                outcome.update(status='inconclusive', observed_status=outcome['status'],
                               input_error=error, reason=INPUT_ERRORS[error])
    if include_resources:
        resource_errors = []
        for identity, outcome in outcomes.items():
            for phase in outcome.get('phases', ()):
                if phase['outcome'] != 'failed':
                    continue
                error = phase.get('exception') or {}
                if error.get('type') == 'MemoryError' or error.get('errno') in (errno.ENOMEM, errno.ENOSPC, getattr(errno, 'EDQUOT', -1)):
                    item = {'test_id': identity, 'phase': phase['phase'], 'type': error['type']}
                    if 'errno' in error:
                        item['errno'] = error['errno']
                    resource_errors.append(item)
        if resource_errors:
            result['resource_errors'] = resource_errors
        code = result.get('exit_code')
        if type(code) is int and code < 0 and status not in ('cancelled', 'timed_out'):
            result['termination_signal'] = -code
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


def run_project(directory, output_root, *, selected_ids=None, python_executable=None, timeout=120,
                confirmed=False, control=None, acceptance_plan=None, order_control=None, version_order=None, audit_discovery=False, fixture_control=None, input_control=None, test_source_control=None):
    if confirmed is not True:
        raise ValueError('请审阅所选测试和固定输入，再明确开始项目回归。')
    _timeout(timeout)
    control = control or RunControl()
    control.check()
    state = read_project(directory)
    if state.get('runner') in ('vitest', 'go'):
        raise ValueError(f"请使用 project {state['runner']} 或 project replay。")
    if acceptance_plan is not None:
        from .acceptance_plan import (PlanSelectionBlocked, plan_matches_baseline, pytest_plan_targets,
                                      require_plan_runner, review_pytest_plan)

        require_plan_runner(acceptance_plan, 'pytest')
        if selected_ids is None:
            selected_ids = pytest_plan_targets(acceptance_plan)
        if not plan_matches_baseline(acceptance_plan, state):
            raise PlanSelectionBlocked(review_pytest_plan(acceptance_plan, state, None, selected=selected_ids))
    # Empty selection is rejected before discovery could load project code.
    if selected_ids == [] or (selected_ids is None and requested_selection(state) == []):
        raise ValueError('当前范围已主动清空；正式运行须明确选择 1–2000 项，不会自动全选。')
    runtime = choose_runtime(python_executable or state['runtime']['executable'])
    execution_environment = resolve_variables(read_pytest_profile(Path(state['directory']) / 'inputs/after'), control.runtime_values)
    collection = _collection(state, runtime)
    if collection and collection.get('clock_control') != control.clock_control:
        collection = None
    if collection and (execution_environment[1]['local_names'] or collection.get('runtime_environment', variable_record(None)) != execution_environment[1]):
        collection = None  # Local values are intentionally not fingerprinted or saved. Recheck discovery.
    if collection is None:
        collection = collect_project(directory, python_executable=runtime['executable'], timeout=timeout, control=control.child(progress=control.progress), execution_environment=execution_environment)
        state = read_project(directory)
    if acceptance_plan is not None:
        review = review_pytest_plan(acceptance_plan, state, collection, selected=selected_ids)
        if not review['ready']:
            raise PlanSelectionBlocked(review)
    if collection['status'] != 'collected':
        diagnosis = collection_diagnosis(collection)
        log = Path(collection['evidence_dir']) / 'collection.log'
        raise ValueError(f'没有可用的真实测试清单。{diagnosis["summary"]} 收集日志：{log}')
    audit_discovery = audit_discovery or (selected_ids is None and requested_selection(state) is None)
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
    validate_order_control(order_control, selected)
    fixed_order = order_control['test_ids'] if order_control is not None else None
    output = Path(output_root).expanduser().resolve()
    folder = Path(state['directory'])
    if output.is_relative_to(folder) or output.is_relative_to(Path(state['repository']).resolve()):
        raise ValueError('项目报告须保存在原仓库及项目会话之外。')
    if acceptance_plan is not None and acceptance_plan.protected_inputs is not None:
        if input_control is not None:
            raise ValueError('计划固定输入与其他业务输入对照不能同时使用。')
        input_control = acceptance_plan.fixed_input_record(selected)
    input_metadata = control_metadata(input_control, selected) if input_control is not None else None
    expected_inputs = {row['test_id']: row['input_digests'] for row in input_metadata['targets']} if input_metadata else None
    validate_test_source_control(test_source_control)
    test_source = 'before' if test_source_control is not None else 'after'
    if any(not (folder / 'inputs' / test_source / parse_test_target(identity)).is_file() for identity in selected):
        raise ValueError('所选文件在测试来源快照中不完整。')
    sequence = validate_version_order(version_order)
    state_directory = tempfile.TemporaryDirectory(prefix='reprolab-pair-state-') if version_order else nullcontext()
    with state_directory as pair_state, _lock(folder):
        latest = read_project(folder)
        if _identity(latest) != _identity(state) or _collection(latest, runtime) != collection:
            raise ValueError('项目输入或测试发现结果发生变化，请重新选择。')
        report_dir = output / _new_id('project-run')
        report_dir.mkdir(parents=True, exist_ok=False)
        save_json(report_dir / 'operation.json', {'status': 'preparing', 'project_id': state['project_id']})
        evidence = report_dir / 'evidence/project'
        _copy_session(state, evidence)
        if fixture_control is not None:
            check_fixture_record(evidence / 'inputs', fixture_control, selected, _digest)
        fixture_changes = changed_conftests(evidence / 'inputs', selected, _digest)
        control.check()
        save_json(report_dir / 'selected.json', {'selected_tests': selected, 'project_id': state['project_id']})
        if input_control is not None:
            save_json(report_dir / CONTROL_FILE, input_control)
        shared = {name: _digest(evidence / 'inputs' / test_source / name)
                  for name in dict.fromkeys(parse_test_target(identity) for identity in selected)}
        profile = read_pytest_profile(evidence / 'inputs/after')
        watched_modules = _watch_project_modules(evidence / 'inputs', profile)
        versions = {side: _not_run(report_dir, side, selected) for side in ('before', 'after')}
        started_versions = []
        baseline_discovery = None
        cancelled = discovery_cancelled = False
        try:
            for side in sequence:
                payload = {'total': len(selected), 'report_dir': str(report_dir)}
                if side == 'before':
                    payload['selected_tests'] = selected
                else:
                    payload['before'] = versions['before']
                control.notify('project_' + side, payload)
                versions[side] = _version(_launch(
                    evidence / 'inputs' / side, evidence / 'inputs/after', selected, report_dir,
                    side, runtime, timeout, control, execution_environment=execution_environment,
                    watched_modules=watched_modules, fixed_order=fixed_order, pair_state=pair_state,
                    baseline_fixtures=(evidence / 'inputs/before', [item['path'] for item in fixture_control['files']])
                    if side == 'after' and fixture_control is not None else None,
                    input_values=input_control['values'] if input_control is not None else None,
                    shared_tests_from=evidence / 'inputs' / test_source,
                ), selected, expected_inputs, include_resources=True)
                started_versions.append(side)
                if versions[side].get('cancelled', False):
                    cancelled = True
                    break
            if not cancelled and audit_discovery:
                # Collect after the comparison so this additional process cannot prewarm the test pair.
                try:
                    baseline_discovery = _baseline_discovery(report_dir, evidence / 'inputs/before',
                                                             collection.get('collection_path', ''), runtime, timeout, control)
                    control.check()
                except (RunCancelled, KeyboardInterrupt):
                    discovery_cancelled = True
                    baseline_discovery = {'status': 'unavailable', 'reason': '基线发现已取消。'}
            if not cancelled and not discovery_cancelled:
                control.commit()
        except RunCancelled:
            cancelled = True
        before, after = versions['before'], versions['after']
        _check_sources(evidence, state)
        _check_sources(folder, state)
        status, entries, counts = _matrix(before, after, selected, cancelled)
        report = {**_identity(state), 'schema_version': 1, 'kind': 'project_regression', 'review_summary_version': 3, 'run_id': report_dir.name,
                  'reprolab_version': __version__,
                  'project_directory': str(folder), 'title': state['title'],
                  'issue': state.get('issue', ''), 'repository': state['repository'],
                  'before_commit': state['before_commit'], 'after_commit': state['after_commit'],
                  'status': status, 'report_dir': str(report_dir), 'selected_tests': selected,
                  'counts': counts, 'entries': entries, 'before': before, 'after': after,
                  'shared_tests': shared, 'changes': state['changes'], 'changed_files': state['changed_files'],
                  'pytest_profile': profile,
                  'runtime': runtime, 'environment': snapshot_environment(runtime['executable']),
                  'runtime_environment': execution_environment[1],
                  'refresh_origin': state.get('refresh_origin'),
                  'review_source': state.get('review_source'),
                  'collection': {**{key: collection.get(key) for key in ('count', 'partial', 'collection_errors', 'truncated')},
                                 'collection_path': collection.get('collection_path', '')},
                  'timeout': timeout, 'created_at': _now(), 'notice': NOTICE}
        execution = {'resource_diagnostics': 1, 'runtime_environment': execution_environment[1], 'selected_tests': selected, 'cancelled': cancelled, 'project_id': state['project_id'],
                     'before': {key: before.get(key) for key in ('exit_code', 'status', 'timed_out', 'cancelled', 'duration_seconds', 'process_id', 'progress_schema') if key in before},
                     'after': {key: after.get(key) for key in ('exit_code', 'status', 'timed_out', 'cancelled', 'duration_seconds', 'process_id', 'progress_schema') if key in after}}
        if order_control is not None:
            report['order_control'] = execution['order_control'] = order_control
        report['fixture_changes'] = execution['fixture_changes'] = fixture_changes
        if fixture_control is not None:
            report['fixture_control'] = execution['fixture_control'] = fixture_control
        if test_source_control is not None:
            report['test_source_control'] = execution['test_source_control'] = test_source_control
        if input_metadata is not None:
            report['input_control'] = execution['input_control'] = input_metadata
        if baseline_discovery is not None:
            report['discovery_audit'] = _discovery_audit(report_dir, baseline_discovery, collection, selected)
            execution['baseline_discovery'] = baseline_discovery
        if version_order is not None:
            record = {**version_order, 'started_versions': started_versions}
            report['version_order'] = execution['version_order'] = record
        if control.clock_control is not None:
            report['clock_control'] = execution['clock_control'] = control.clock_control
        if control.network_isolation is not None:
            report['network_isolation'] = execution['network_isolation'] = control.network_isolation
        from .project_comparison import comparison_scope, comparison_view

        _, phase_decisions = comparison_view(report, comparison_scope(report))
        if phase_decisions:
            report['phase_decisions'] = phase_decisions
        save_json(report_dir / 'execution.json', execution)
        save_json(report_dir / 'operation.json', {'status': status, 'run_id': report_dir.name, 'phase': 'saved'})
        from .project_report import write_project_report
        write_project_report(report, collection=collection)
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
        if not cancelled and not discovery_cancelled:
            control.notify('project_done', {'report_dir': str(report_dir), 'status': status})
        return report


def _saved_version(folder, label, meta, selected, expected_inputs=None, *, include_resources=False):
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
    reader = None
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
    result.update(_pytest_execution_context(raw, reader, result['worker_record_complete']))
    return _version(result, selected, expected_inputs, include_resources=include_resources)


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
        if any(report.get(key) != state.get(key) for key in ('excluded_tracked_changes', 'archive_preflight')):
            raise ValueError('报告的快照排除或 ZIP 路径预检记录与固定会话不一致。')
        if 'archive_preflight' in state and state['archive_preflight'] != source_zip_preflight(
                folder / 'evidence/project/inputs', runner=report.get('runner', 'pytest'), control=control):
            raise ValueError('ZIP 路径预检记录与固定源码不一致。')
        if (report.get('review_source') != state.get('review_source')
                or report.get('refresh_origin') != state.get('refresh_origin')):
            raise ValueError('报告来源记录与保存的会话证据不一致。')
        for key in ('before_commit', 'after_commit'):
            if report.get(key) not in (None, '') and report[key] != state.get(key):
                raise ValueError('报告基线或版本记录与固定会话不一致。')
        execution = _read(folder / 'execution.json')
        from .project_network import validate_network_record

        validate_network_record(report, execution)
        clock = report.get('clock_control')
        validate_clock(clock)
        if execution.get('clock_control') != clock:
            raise ValueError('固定时钟与执行记录不一致。')
        if clock is not None:
            for side in ('before', 'after'):
                raw = _read(folder / f'{side}.events.json')
                if raw and raw.get('clock_control') != clock:
                    raise ValueError('工作进程未使用声明的固定时钟。')
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
        snapshots = folder / 'evidence/project/inputs'
        if 'fixture_changes' in execution or 'fixture_changes' in report:
            changed = changed_conftests(snapshots, selected, _digest)
            if report.get('fixture_changes') != changed or execution.get('fixture_changes') != changed:
                raise ValueError('夹具变更记录与固定源码不一致。')
        fixture = report.get('fixture_control')
        if fixture != execution.get('fixture_control'):
            raise ValueError('夹具对照记录与执行证据不一致。')
        if fixture is not None:
            check_fixture_record(snapshots, fixture, selected, _digest)
            raw = _read(folder / 'after.events.json')
            if raw and raw.get('fixture_sha256') != {item['path']: item['before_sha256'] for item in fixture['files']}:
                raise ValueError('候选工作进程未记录所选基线夹具指纹。')
        version_order = report.get('version_order')
        if version_order != execution.get('version_order'):
            raise ValueError('版本先后对照记录与执行证据不一致。')
        if version_order is not None:
            validate_version_order(version_order, execution=execution)
        order = report.get('order_control')
        validate_order_control(order, selected)
        if order != execution.get('order_control'):
            raise ValueError('顺序对照记录与执行证据不一致。')
        fixed_order = order['test_ids'] if order is not None else None
        expected_order_digest = order_digest(fixed_order) if fixed_order is not None else None
        scope = (report.get('collection') or {}).get('collection_path', '')
        if scope != state.get('collection_path', ''):
            raise ValueError('报告发现范围与固定会话不一致。')
        if scope:
            collection = _collection(state, report.get('runtime'))
            if (collection is None or not set(selected) <= set(collection['tests'])
                    or any(not PurePosixPath(parse_test_target(identity)).is_relative_to(scope) for identity in selected)):
                raise ValueError('报告所选测试与保存的发现范围不一致。')
        if 'baseline_discovery' in execution or 'discovery_audit' in report:
            baseline_discovery = execution['baseline_discovery']
            if baseline_discovery['status'] != 'unavailable' and not {
                'baseline-discovery.events.json', 'baseline-discovery.log', 'baseline-discovery.progress.jsonl',
            } <= hashes.keys():
                raise ValueError('缺少基线测试发现证据。')
            collection = _collection(state, report.get('runtime'))
            if (collection is None or state['collection_record'] not in {
                    name.removeprefix('evidence/project/') for name in hashes if name.startswith('evidence/project/')}
                    or not set(selected) <= set(collection['tests'])
                    or report.get('discovery_audit') != _discovery_audit(folder, baseline_discovery, collection, selected)):
                raise ValueError('两版发现范围核对与保存的发现证据不一致。')
        source_control = report.get('test_source_control')
        validate_test_source_control(source_control)
        if source_control != execution.get('test_source_control'):
            raise ValueError('测试来源对照与执行证据不一致。')
        test_source = 'before' if source_control is not None else 'after'
        shared = report.get('shared_tests')
        # Missing optional caches in old records remain unknown in comparisons.
        # All mandatory frozen source and execution evidence is still checked.
        if shared is not None:
            if not isinstance(shared, dict) or set(shared) != {parse_test_target(identity) for identity in selected}:
                raise ValueError('共享测试清单不完整。')
            if any(_digest(folder / 'evidence/project/inputs' / test_source / name) != digest for name, digest in shared.items()):
                raise ValueError('共享测试内容不一致。')
        if 'pytest_profile' in report and report['pytest_profile'] != read_pytest_profile(folder / 'evidence/project/inputs/after'):
            raise ValueError('报告的 pytest 运行配置与固定输入不一致。')
        variables = variable_record(read_pytest_profile(folder / 'evidence/project/inputs/after'))
        if 'runtime_environment' in report or variables['values'] or variables['local_names']:
            if report.get('runtime_environment') != variables or execution.get('runtime_environment') != variables:
                raise ValueError('运行变量记录与固定输入或执行记录不一致。')
        expected_inputs = None
        if 'input_control' in report or 'input_control' in execution or CONTROL_FILE in hashes:
            if CONTROL_FILE not in hashes:
                raise ValueError('缺少固定业务输入文件。')
            metadata = control_metadata(_read(folder / CONTROL_FILE), selected)
            if metadata != report.get('input_control') or metadata != execution.get('input_control'):
                raise ValueError('业务输入对照与执行证据不一致。')
            expected_inputs = {row['test_id']: row['input_digests'] for row in metadata['targets']}
        resources = execution.get('resource_diagnostics') == 1
        before = _saved_version(folder, 'before', execution['before'], selected, expected_inputs,
                                include_resources=resources)
        after = _saved_version(folder, 'after', execution['after'], selected, expected_inputs,
                               include_resources=resources)
        fields = ('status', 'exit_code', 'timed_out', 'tests', 'duration_seconds',
                  'worker_record_complete', 'python_version', 'pytest_version', 'log_excerpt',
                  'pytest_rules', 'execution_order', 'module_origins', 'runtime_context', 'worker_resources',
                  'resource_errors', 'termination_signal')
        for label, actual in (('before', before), ('after', after)):
            rules = actual.get('pytest_rules')
            if rules and rules.get('order_sha256') != expected_order_digest:
                raise ValueError('pytest 执行顺序设置与证据不一致：' + label)
            if fixed_order is not None and actual['status'] == 'completed' and actual.get('execution_order') != fixed_order:
                raise ValueError('pytest 实际顺序与对照设置不一致：' + label)
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
        if 'phase_decisions' in report:
            from .project_comparison import comparison_scope, comparison_view

            _, decisions = comparison_view(report, comparison_scope(report))
            if report['phase_decisions'] != decisions:
                raise ValueError('pytest 阶段比较与保存的执行证据不一致。')
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
    source_runtime = report.get('runtime') or {}
    return {'source_runtime': {key: source_runtime.get(key) for key in ('executable', 'python_version', 'pytest_version')},
            'source_timeout': source_timeout,
            'runtime': {'executable': str(python_executable or sys.executable)},
            'timeout': _timeout(timeout if timeout is not None else source_timeout or 120)}


def replay_project_report(directory, output_root, *, python_executable=None, timeout=None, confirmed=False,
                          control=None, node=None, npm=None, offline=None, npm_cache=None, go=None,
                          expected_report=None, check_environment=False, verify_result=False, order_mode=None, version_order=None, fixture_files=None, input_tests=None, baseline_tests=False):
    if verify_result and not confirmed:
        raise ValueError('重放后核对需要明确启动重放。')
    if confirmed and check_environment:
        raise ValueError('请分别检查本机环境和启动重放。')
    if control:
        control.check()
    if expected_report is not None or check_environment or verify_result or order_mode is not None or fixture_files is not None or input_tests is not None or baseline_tests:
        from .project_history import _checked_report, _match_reference

        report = _checked_report(directory, control=control)
        if expected_report is not None:
            _match_reference(report, expected_report)
        folder = Path(report['report_dir'])
    else:
        checked = check_project_report(directory, control=control) if control else check_project_report(directory)
        if checked['status'] != 'consistent':
            raise ValueError('项目报告证据不一致：' + '; '.join(checked['issues']))
        folder = Path(checked['report_dir'])
        report = _read(folder / 'report.json')
    if control:
        control.check()
    if Path(output_root).expanduser().resolve().is_relative_to(folder):
        raise ValueError('重放输出须保存在来源报告之外。')
    def finish(result):
        if not verify_result:
            return result
        from .replay_verification import verify_new_replay

        verification = verify_new_replay(report, result, control=None if result['status'] == 'cancelled' else control)
        return {**result, 'replay_verification': verification}

    control = control or RunControl()
    clock = report.get('clock_control')
    if control.clock_control is not None and report.get('runner', 'pytest') != 'pytest':
        raise ValueError('固定 Python 时钟目前用于 pytest。')
    if clock is not None and control.clock_control not in (None, clock):
        raise ValueError('重放需要使用来源报告的固定时钟。')
    if clock is not None and confirmed:
        control.clock_control = clock
    isolation = getattr(control, 'network_isolation', None)
    if confirmed and report.get('network_isolation') and isolation is None:
        raise ValueError('此报告记录网络隔离，请使用 project replay --network-isolated 重放。')
    if fixture_files is not None and (report.get('runner') in ('go', 'vitest') or report.get('fixture_control')):
        raise ValueError('夹具对照需要一份原始 pytest 报告。')
    test_source_control = report.get('test_source_control')
    if baseline_tests:
        if report.get('runner', 'pytest') != 'pytest' or test_source_control is not None:
            raise ValueError('请选择使用候选测试文件的原始 pytest 比较。')
        snapshot = folder / 'evidence/project/inputs/before'
        if any(not (snapshot / parse_test_target(identity)).is_file() for identity in report['selected_tests']):
            raise ValueError('基线缺少所选测试文件，无法建立完整测试来源对照。')
        test_source_control = {'source_report_sha256': report['_report_sha256'], 'tests_from': 'before'}
    input_control = None
    if input_tests is not None:
        input_control = select_failure_inputs(report, input_tests)
    elif report.get('input_control'):
        input_control = _read(folder / CONTROL_FILE)
    if order_mode is not None and report.get('runner') in ('go', 'vitest'):
        raise ValueError('顺序对照适用于 pytest 项目报告。')
    if check_environment and report.get('runner') in ('go', 'vitest'):
        raise ValueError('此选项用于检查 pytest 报告的 Python 环境。')
    if report.get('runner') == 'vitest':
        from .vitest_project import replay_vitest_project
        if python_executable is not None or go is not None:
            raise ValueError('Vitest 重放使用 --node 和 --npm 选择运行环境。')
        if confirmed and npm_cache and Path(npm_cache).expanduser().resolve().is_relative_to(folder):
            raise ValueError('npm 缓存须位于来源报告目录之外。')
        return finish(replay_vitest_project(folder, report, output_root, node=node, npm=npm, timeout=timeout,
                                            offline=True if isolation else offline, npm_cache=npm_cache, confirmed=confirmed, control=control))
    if report.get('runner') == 'go':
        from .go_project import replay_go_project
        if any(value is not None for value in (python_executable, node, npm, offline, npm_cache)):
            raise ValueError('Go 重放使用 --go 选择运行环境。')
        return finish(replay_go_project(folder, report, output_root, go=go, timeout=timeout,
                                        confirmed=confirmed, control=control))
    if any(value is not None for value in (node, npm, offline, npm_cache, go)):
        raise ValueError('此报告使用 pytest，请通过 --python 选择运行环境。')
    if report.get('version_order') is not None:
        if order_mode is not None:
            raise ValueError('请从原始报告启动固定测试顺序对照。')
        if confirmed:
            raise ValueError('请用 project version-order 从原始报告重放完整的版本先后对照。')
    fixture = report.get('fixture_control')
    if fixture_files is not None:
        fixture = fixture_record(folder / 'evidence/project/inputs', fixture_files, report['_report_sha256'],
                                 report['selected_tests'], _digest)
    order = report.get('order_control')
    if order_mode is not None:
        if order_mode not in ('baseline', 'requested'):
            raise ValueError('请选择基线实际顺序或原请求顺序。')
        tests = report['selected_tests']
        if order_mode == 'baseline':
            if report['before']['status'] != 'completed':
                raise ValueError('基线执行未完成，无法采用它的实际顺序。')
            tests = report['before'].get('execution_order')
            if tests is None:
                tests = _read(folder / 'before.events.json').get('started_tests')
        order = {'mode': order_mode, 'source_report_sha256': report['_report_sha256'], 'test_ids': tests}
        validate_order_control(order, report['selected_tests'])
    replay_profile = read_pytest_profile(folder / 'evidence/project/inputs/after')
    variables = variable_record(replay_profile)
    settings = project_replay_settings(report, python_executable=python_executable, timeout=timeout)
    preview = {**settings, 'runtime_environment': variables, 'required_local_variables': variables['local_names'],
               'status': 'ready', 'kind': 'project_replay_preview', 'title': report['title'],
               'report_dir': str(folder), 'selected_tests': report['selected_tests'], 'count': len(report['selected_tests']),
               'collection_path': (report.get('collection') or {}).get('collection_path', ''),
               'notice': '已校验证据；明确重放后将使用所选环境重新运行，不覆盖来源报告。'}
    if report.get('version_order') is not None:
        preview['version_order'] = report['version_order']
        preview['notice'] = '请用 project version-order 从原始报告重放完整的版本先后对照。'
    if clock is not None or control.clock_control is not None:
        preview['clock_control'] = clock or control.clock_control
    if test_source_control is not None:
        preview['test_source_control'] = test_source_control
    if input_control is not None:
        preview['input_control'] = control_metadata(input_control, report['selected_tests'])
    if fixture is not None:
        preview['fixture_control'] = fixture
    if order is not None:
        preview['order_control'] = order
    if check_environment:
        from .replay_toolchain import inspect_pytest_replay_environment

        preview['environment_check'] = inspect_pytest_replay_environment(
            folder / 'evidence/project/inputs/after', report, settings['runtime']['executable'], control=control)
        preview['status'] = preview['environment_check']['status']
    if confirmed is not True:
        return preview
    resolve_variables(replay_profile, getattr(control, 'runtime_values', None))
    source = read_project(folder / 'evidence/project')
    clone = Path(output_root).expanduser().resolve() / 'replay-projects' / _new_id('project-replay')
    clone.mkdir(parents=True, exist_ok=False)
    _copy_session(source, clone)
    return finish(run_project(clone, output_root, selected_ids=report['selected_tests'],
                              python_executable=settings['runtime']['executable'], timeout=settings['timeout'],
                              confirmed=True, control=control, order_control=order, version_order=version_order,
                              audit_discovery='discovery_audit' in report, fixture_control=fixture, input_control=input_control,
                              test_source_control=test_source_control))


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
