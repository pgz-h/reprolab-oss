"""Compare selected Go tests in an explicitly selected package of one Git module."""
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import tempfile
import time

from . import __version__
from .changes import describe_changes
from .git_source import excluded_tracked_changes, export_revision, resolve_commit
from .persistence import save_json
from .process_tree import run_logged_command
from .project_archive import source_zip_preflight
from .project_collection import normalize_collection_path
from .recipes import repository_root
from .repair_suite import _new_id
from .replay_toolchain import query_tool_version
from .runner import EXCLUDED, RunControl, source_fingerprint, source_mode_fingerprint
from .runtime import BASE_ENVIRONMENT_KEYS
from .workspace import WORKTREE, copy_workspace

NOTICE = ('两版在独立副本中使用候选版本同一包的测试文件，并保留各自的实现和模块依赖。'
          '直接选择顶层测试，并用原始事件按完整名称核对其子测试。')
_EVENT_BYTES = 8 * 1024 * 1024
_MAX_TESTS = 2000
_OUTCOMES = {'pass': 'passed', 'fail': 'failed', 'skip': 'skipped'}


def _package(value):
    path = normalize_collection_path(value) if value not in ('.', './') else '.'
    if not path or any(part in EXCLUDED or part.startswith('.') for part in PurePosixPath(path).parts):
        raise ValueError('请选择仓库内单个 Go 包目录，例如 . 或 internal/cart。')
    return path


def _tests(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= _MAX_TESTS
            or any(not isinstance(name, str) or not 4 <= len(name) <= 200
                   or not name.startswith('Test') or not name.isidentifier()
                   or name[4:5].islower() for name in values)):
        raise ValueError(f'请选择 1–{_MAX_TESTS} 个顶层 Go 测试函数，例如 TestCheckout。')
    return list(dict.fromkeys(values))


def _test_files(snapshots, package):
    for side in ('before', 'after'):
        root = snapshots / side
        if not (root / 'go.mod').is_file():
            raise ValueError('Go 预览要求两版 Git 快照在根目录都有 go.mod。')
        if any(re.match(r'^\s*replace(?:\s|\()', line.split('//', 1)[0])
               for line in (root / 'go.mod').read_text(encoding='utf-8').splitlines()):
            raise ValueError('Go 预览要求 go.mod 不含 replace。')
        directory = root / package
        while directory != root:
            if (directory / 'go.mod').exists():
                raise ValueError('Go 预览只运行仓库根模块中的包。')
            directory = directory.parent
    files = sorted((snapshots / 'after' / package).glob('*_test.go'))
    files = [file.relative_to(snapshots / 'after').as_posix() for file in files if file.is_file()]
    if not files:
        raise ValueError(f'候选版本的包目录 {package} 中没有 *_test.go 文件。'
                         '请恢复该包的测试文件，或选择包含这些文件的 Git 工作区后重试。')
    return files


def _environment():
    keys = (*BASE_ENVIRONMENT_KEYS, 'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'GOCACHE', 'GOMODCACHE')
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(GOENV='off', GOWORK='off', GOTOOLCHAIN='local', GOPROXY='off', CGO_ENABLED='0')
    return env


def _runtime(go, *, cwd=None, control=None):
    executable = shutil.which(os.path.expanduser(go) if go else 'go')
    if executable is None:
        raise ValueError('找不到 Go，请安装工具链或通过 --go 指定路径。')
    executable = os.path.abspath(executable)
    version = query_tool_version('Go', [executable, 'version'], env=_environment(), cwd=cwd, control=control)
    if not version.startswith('go version '):
        raise ValueError('无法获取 Go 工具链版本，请选择可用的 Go 可执行文件。')
    return {'go': executable, 'go_version': version}


def _event(line):
    event = json.loads(line)
    if not isinstance(event, dict) or not isinstance(event.get('Action'), str):
        raise ValueError('missing Go event action')
    return event


def _compact_events(log):
    """Preserve lifecycle events even when Go emits large amounts of output."""
    events, tails = [], {}
    structural_bytes = omitted = 0
    complete = True
    with log.open('rb') as stream:
        while line := stream.readline(_EVENT_BYTES + 1):
            if len(line) > _EVENT_BYTES:
                return {'events': [], 'complete_json': False, 'reason': 'Go 单条事件超过 8 MiB。'}
            try:
                event = _event(line)
                if event['Action'] in ('output', 'build-output') and isinstance(event.get('Output'), str):
                    key = tuple(event.get(field) for field in ('Action', 'Package', 'Test', 'ImportPath'))
                    previous = tails.get(key)
                    if previous is None and len(tails) >= _MAX_TESTS * 4:
                        return {'events': [], 'complete_json': False, 'reason': 'Go 日志的输出归属超过记录上限。'}
                    chars = (previous['Output'] if previous else '') + event['Output']
                    event['Output'] = chars[-4000:]
                    tails[key] = event
                    omitted += len(chars) - len(event['Output'])
                else:
                    structural_bytes += len(line)
                    if structural_bytes > _EVENT_BYTES:
                        return {'events': [], 'complete_json': False, 'reason': 'Go 结构化执行事件超过 8 MiB。'}
                    events.append(event)
            except (ValueError, RecursionError, TypeError):
                complete = False
    events.extend(tails.values())
    return {'events': events, 'complete_json': complete, 'omitted_output_chars': omitted}


def _events(log, *, compact=False):
    if log.stat().st_size > _EVENT_BYTES:
        if compact:
            return _compact_events(log)
        return {'events': [], 'complete_json': False, 'reason': 'Go 事件超过 8 MiB。'}
    events, complete = [], True
    for line in log.read_bytes().splitlines():
        try:
            events.append(_event(line))
        except (ValueError, RecursionError):
            complete = False
    return {'events': events, 'complete_json': complete}


def _log_excerpt(log):
    with log.open('rb') as stream:
        stream.seek(max(0, log.stat().st_size - 4000))
        return stream.read(4000).decode('utf-8', errors='replace')


def _discover_go_tests(snapshots, package, runtime, timeout, control):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='reprolab-go-list-') as directory:
        scratch = Path(directory)
        project = scratch / 'project'
        shutil.copytree(snapshots / 'after', project)
        log = scratch / 'discovery.log'
        command = [runtime['go'], 'test', '-json', '-count=1', '-mod=readonly',
                   '-timeout=0', '-list', '^Test', '.']
        with log.open('wb') as stream:
            status, code = run_logged_command(
                command, project / package, _environment(), stream, started + timeout, control)
        if status == 'timed_out':
            raise ValueError(f'Go 测试发现超时（{timeout:g} 秒）。请调整限时后重试。')
        raw = _events(log)
        events = [event for event in raw['events'] if isinstance(event.get('Package'), str)]
        lifecycle = [event['Action'] for event in events if event['Action'] in ('start', 'pass', 'fail', 'skip')]
        if (status != 'completed' or code != 0 or not raw['complete_json']
                or len({event['Package'] for event in events}) != 1
                or lifecycle not in (['start', 'pass'], ['start', 'skip'])
                or any('Test' in event for event in events)
                or any(event['Action'] == 'build-fail' for event in raw['events'])):
            detail = ''.join(event['Output'] for event in raw['events']
                             if isinstance(event.get('Output'), str))[-4000:].strip()
            detail = raw.get('reason') or detail or _log_excerpt(log).strip()
            raise ValueError(f'无法取得包 {package} 的完整 Go 测试清单。\n{detail}'.rstrip())
        names = []
        for event in events:
            if event['Action'] == 'output' and isinstance(event.get('Output'), str):
                name = event['Output'].rstrip('\r\n')
                if re.fullmatch(r'Test\S*', name):
                    names.append(name)
    if lifecycle[-1] == 'skip' or not names:
        raise ValueError(f'候选包 {package} 在本机 Go 构建条件下没有可发现的顶层测试。')
    if len(names) > _MAX_TESTS:
        raise ValueError(f'候选包 {package} 的顶层测试超过 {_MAX_TESTS} 项，请手动指定测试范围。')
    return _tests(names)


def _version(folder, label, execution, raw, selected, *, include_subtests=False):
    started, tests, package_ends, output = set(), {}, [], {}
    subtests = {}
    valid = raw['complete_json'] and execution['status'] == 'completed'
    packages = set()
    for event in raw['events']:
        action, name = event['Action'], event.get('Test')
        if isinstance(event.get('Package'), str):
            packages.add(event['Package'])
        if name is None:
            if action in _OUTCOMES:
                package_ends.append(action)
            if action in ('build-fail', 'skip'):
                valid = False
            continue
        if not isinstance(name, str):
            valid = False
            continue
        parent = name.split('/', 1)[0]
        if parent not in selected:
            valid = False
            continue
        if action == 'output' and isinstance(event.get('Output'), str):
            for target in (parent, name) if include_subtests and name != parent else (parent,):
                output[target] = (output.get(target, '') + event['Output'])[-4000:]
        if name != parent and not include_subtests:
            continue
        results = tests if name == parent else subtests
        if action == 'run':
            valid = valid and name not in started
            started.add(name)
        if action in _OUTCOMES:
            valid = valid and name in started and name not in results
            elapsed = event.get('Elapsed', 0)
            results[name] = {'status': _OUTCOMES[action], 'exception': None, 'phases': [],
                           'duration_seconds': elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) else 0}
    failed = any(result['status'] == 'failed' for result in tests.values())
    valid = (valid and len(packages) == 1 and started == tests.keys() | subtests.keys()
             and package_ends == (['fail'] if failed else ['pass']) and execution['exit_code'] == int(failed))
    for name, result in (*tests.items(), *subtests.items()):
        if result['status'] == 'failed':
            result['exception'] = {'type': 'Go test', 'message': output.get(name, '').strip()}
        if not valid:
            result['observed_status'] = result['status']
            result['status'] = 'inconclusive'
    log = folder / (label + '.log')
    return {**execution, 'status': 'completed' if valid else (
            'worker_error' if execution['status'] == 'completed' else execution['status']),
            'worker_record_complete': valid, 'tests': tests,
            'timed_out': execution['status'] == 'timed_out',
            'reason': '' if valid else raw.get('reason', 'Go 未产生完整且可核对的执行记录。'),
            'log': log.name, 'events': label + '.events.json', 'log_excerpt': _log_excerpt(log),
            **({'omitted_output_chars': raw['omitted_output_chars']} if 'omitted_output_chars' in raw else {}),
            **({'subtests': subtests} if include_subtests else {})}


def _compare_with_subtests(before, after, selected):
    from .project_sessions import _matrix, _regression_status

    _, entries, _ = _matrix(before, after, selected)
    children = {}
    for run in (before, after):
        for name in run['subtests']:
            children.setdefault(name.split('/', 1)[0], {})[name] = None
    for entry in entries:
        names = children.get(entry['test_id'])
        if not names:
            continue
        _, cases, counts = _matrix({'tests': before['subtests']}, {'tests': after['subtests']}, list(names))
        entry['case_entries'] = cases
        if counts.get('regression_detected'):
            entry['status'] = 'regression_detected'
        elif counts.get('inconclusive') and entry['status'] != 'regression_detected':
            entry['status'] = 'inconclusive'
    counts = dict(Counter(entry['status'] for entry in entries))
    return _regression_status(before, after, counts), entries, counts


def _run_version(stage, label, snapshots, package, shared, selected, runtime, timeout, control):
    started = time.monotonic()
    log = stage / (label + '.log')
    with tempfile.TemporaryDirectory(prefix='reprolab-go-run-') as directory:
        project = Path(directory) / 'project'
        shutil.copytree(snapshots / label, project)
        package_dir = project / package
        package_dir.mkdir(parents=True, exist_ok=True)
        for existing in package_dir.glob('*_test.go'):
            if existing.is_file():
                existing.unlink()
        for file in shared:
            shutil.copy2(snapshots / 'after' / file, project / file)
        target = '^(' + '|'.join(selected) + ')$'
        command = [runtime['go'], 'test', '-json', '-count=1', '-mod=readonly', '-timeout=0', '-run', target, '.']
        with log.open('wb') as stream:
            status, code = run_logged_command(command, package_dir, _environment(), stream, started + timeout, control)
    execution = {'status': status, 'exit_code': code, 'duration_seconds': round(time.monotonic() - started, 3)}
    raw = _events(log, compact=True)
    save_json(stage / (label + '.events.json'), raw)
    return execution, _version(stage, label, execution, raw, selected, include_subtests=True)


def _publish(stage, final, state, package, selected, runtime, timeout, control):
    from .project_report import write_project_report
    from .project_sessions import _check_sources, _digest, _identity, _manifest

    evidence = stage / 'evidence/project'
    snapshots = evidence / 'inputs'
    shared = {name: _digest(snapshots / 'after' / name) for name in _test_files(snapshots, package)}
    state = {**state, 'schema_version': 1, 'kind': 'project_session', 'runner': 'go',
             'directory': str(final / 'evidence/project'), 'runtime': runtime, 'go_package': package}
    _check_sources(evidence, state)
    state['archive_preflight'] = source_zip_preflight(snapshots, runner='go', control=control)
    save_json(evidence / 'project.json', state)
    execution = {'runner': 'go', 'project_id': state['project_id'], 'selected_tests': selected,
                 'go_package': package, 'runtime': runtime, 'timeout': timeout, 'go_report_version': 3}
    runs = {}
    for label in ('before', 'after'):
        execution[label], runs[label] = _run_version(
            stage, label, snapshots, package, shared, selected, runtime, timeout, control)
    _check_sources(evidence, state)
    status, entries, counts = _compare_with_subtests(runs['before'], runs['after'], selected)
    report = {**_identity(state), **runs, 'schema_version': 1, 'kind': 'project_regression', 'review_summary_version': 3, 'go_report_version': 3,
              'reprolab_version': __version__, 'run_id': final.name, 'report_dir': str(stage),
              'project_directory': state['directory'], 'created_at': datetime.now(timezone.utc).isoformat(),
              'title': state['title'], 'repository': state['repository'], 'before_commit': state['before_commit'],
              'after_commit': state['after_commit'], 'go_package': package, 'runtime': runtime, 'timeout': timeout,
              'selected_tests': selected, 'shared_tests': shared, 'status': status, 'entries': entries, 'counts': counts,
              'environment': {'platform': platform.system(), 'machine': platform.machine()},
              'changes': describe_changes(snapshots / 'before', snapshots / 'after'), 'notice': NOTICE}
    if state.get('review_source') is not None:
        report['review_source'] = state['review_source']
    save_json(stage / 'selected.json', {'selected_tests': selected, 'project_id': state['project_id']})
    if control.network_isolation is not None:
        report['network_isolation'] = execution['network_isolation'] = control.network_isolation
    save_json(stage / 'execution.json', execution)
    write_project_report(report)
    report.update(report_dir=str(final), persistence=_manifest(stage))
    save_json(stage / 'report.json', report)
    control.commit()
    stage.rename(final)
    return report


def _freeze_go_sources(root, before, after, snapshots, control):
    before = resolve_commit(root, before)
    after = after if after == WORKTREE else resolve_commit(root, after)
    snapshots.mkdir(parents=True)
    export_revision(root, before, snapshots / 'before')
    control.check()
    if after == WORKTREE:
        copy_workspace(root, snapshots / 'after', control=control)
    else:
        export_revision(root, after, snapshots / 'after')
    return before, after


def list_go_tests(repository, before, after, *, package, go=None, timeout=120, control=None):
    from .project_sessions import _timeout

    control = control or RunControl()
    package, timeout = _package(package), _timeout(timeout)
    root = repository_root(repository)
    runtime = _runtime(go, control=control)
    with tempfile.TemporaryDirectory(prefix='reprolab-go-discovery-') as directory:
        snapshots = Path(directory) / 'inputs'
        before, after = _freeze_go_sources(root, before, after, snapshots, control)
        _test_files(snapshots, package)
        names = _discover_go_tests(snapshots, package, runtime, timeout, control)
    return {'kind': 'go_test_discovery', 'schema_version': 1, 'status': 'collected',
            'go_package': package, 'before_commit': before, 'after_commit': after,
            'runtime': runtime, 'tests': names}


def run_go_project(repository, before, after, output, *, package, selected, go=None, timeout=120,
                   control=None, acceptance_plan=None):
    from .project_sessions import _timeout

    control = control or RunControl()
    package, timeout = _package(package), _timeout(timeout)
    if acceptance_plan is not None:
        from .acceptance_plan import check_plan_scope, execution_selection, require_plan_runner

        require_plan_runner(acceptance_plan, 'go')
        if selected is None:
            selected = execution_selection(acceptance_plan, 'go')
    if selected is not None:
        selected = _tests(selected)
    root = repository_root(repository)
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(root):
        raise ValueError('请将报告保存在被测仓库外。')
    runtime = _runtime(go, control=control)
    output.mkdir(parents=True, exist_ok=True)
    final = output / _new_id('go-run')
    with tempfile.TemporaryDirectory(prefix='.go-run-', dir=output) as directory:
        stage = Path(directory) / final.name
        snapshots = stage / 'evidence/project/inputs'
        before, after = _freeze_go_sources(root, before, after, snapshots, control)
        state = {'project_id': _new_id('project'), 'title': root.name + ' · Go 项目回归',
                 'repository': str(root), 'before_commit': before, 'after_commit': after,
                 'excluded_tracked_changes': excluded_tracked_changes(root, before, None if after == WORKTREE else after),
                 'source_sha256': {side: source_fingerprint(snapshots / side)[1] for side in ('before', 'after')},
                 'source_mode_sha256': {side: source_mode_fingerprint(snapshots / side) for side in ('before', 'after')}}
        if acceptance_plan is not None:
            check_plan_scope(acceptance_plan, state, selected, go_package=package)
        if selected is None or acceptance_plan is not None:
            _test_files(snapshots, package)
            discovered = _discover_go_tests(snapshots, package, runtime, timeout, control)
            if acceptance_plan is None:
                selected = discovered
            else:
                check_plan_scope(acceptance_plan, state, selected, available=set(discovered), go_package=package)
        return _publish(stage, final, state, package, selected, runtime, timeout, control)


def check_go_evidence(folder, report, state, execution, selected):
    from .project_sessions import _digest, _matrix, _read, _timeout

    if report.get('review_source') is not None:
        from .project_history import validate_report_reference

        reference = validate_report_reference(report['review_source'])
        if reference.get('selection_mode') != 'all' or reference.get('selected_tests') != selected:
            raise ValueError('Go 复查所选测试与来源引用不一致。')
    package = _package(report['go_package'])
    if (_tests(selected) != selected or state.get('runner') != 'go' or execution.get('runner') != 'go'
            or not all(record.get('go_package') == package for record in (state, report, execution))
            or report['runtime'] != state['runtime'] or report['runtime'] != execution['runtime']
            or _timeout(report['timeout']) != execution['timeout']):
        raise ValueError('Go 运行条件与证据不一致。')
    snapshots = folder / 'evidence/project/inputs'
    if report['shared_tests'] != {name: _digest(snapshots / 'after' / name) for name in _test_files(snapshots, package)}:
        raise ValueError('Go 共享测试文件与证据不一致。')
    version = report.get('go_report_version', 1)
    if type(version) is not int or version not in (1, 2, 3) or execution.get('go_report_version', 1) != version:
        raise ValueError('Go 报告版本与执行证据不一致。')
    runs = {}
    for label in ('before', 'after'):
        raw = _events(folder / (label + '.log'), compact=version >= 3)
        if raw != _read(folder / (label + '.events.json')):
            raise ValueError('Go 事件与原始日志不一致。')
        runs[label] = _version(folder, label, execution[label], raw, selected, include_subtests=version >= 2)
        if report[label] != runs[label]:
            raise ValueError('Go 运行摘要与原始记录不一致。')
    compare = _compare_with_subtests if version >= 2 else _matrix
    if compare(runs['before'], runs['after'], selected) != (report['status'], report['entries'], report['counts']):
        raise ValueError('Go 回归结论与原始记录不一致。')


def replay_go_project(folder, report, output, *, go, timeout, confirmed, control):
    from .project_sessions import _copy_session, _timeout, read_project

    timeout = _timeout(report['timeout'] if timeout is None else timeout)
    selected = report['selected_tests']
    package = report['go_package']
    if not confirmed:
        return {'kind': 'project_replay_preview', 'status': 'ready', 'runner': 'go',
                'report_dir': str(folder), 'selected_tests': selected, 'count': len(selected),
                'go_package': package, 'source_runtime': report['runtime'], 'timeout': timeout}
    output = Path(output).expanduser().resolve()
    state = read_project(folder / 'evidence/project')
    if output.is_relative_to(Path(state['repository']).resolve()):
        raise ValueError('请将重放报告保存在原仓库外。')
    runtime = _runtime(go)
    output.mkdir(parents=True, exist_ok=True)
    final = output / _new_id('go-run')
    with tempfile.TemporaryDirectory(prefix='.go-replay-', dir=output) as directory:
        stage = Path(directory) / final.name
        _copy_session(state, stage / 'evidence/project')
        return _publish(stage, final, state, package, selected, runtime, timeout, control or RunControl())


def recheck_go_project(directory, output, *, expected_report, repository=None, go=None,
                       timeout=None, confirmed=False, control=None):
    """Compare a new worktree to the verified baseline of an exact Go report."""
    from .project_history import _checked_report, _match_reference
    from .project_refresh import _stage_worktree_recheck
    from .project_sessions import _timeout

    control = control or RunControl()
    control.check()
    source = _checked_report(directory, control=control)
    _match_reference(source, expected_report)
    if source.get('runner') != 'go':
        raise ValueError('请选择 Go 项目报告作为复查来源。')
    package, selected = source['go_package'], source['selected_tests']
    timeout = _timeout(source['timeout'] if timeout is None else timeout)
    if confirmed is not True:
        return {'kind': 'go_workspace_recheck_preview', 'status': 'ready', 'runner': 'go',
                'report_dir': source['report_dir'], 'selected_tests': selected, 'count': len(selected),
                'go_package': package, 'source_runtime': source['runtime'], 'timeout': timeout,
                'repository': source['repository']}

    with _stage_worktree_recheck(source, output, repository, control=control) as (stage, final, state):
        runtime = _runtime(go)
        report = _publish(stage, final, state, package, selected, runtime, timeout, control)
    return {'report': report, 'comparison': compare_go_recheck(source, report)}


def _candidate_outcome(report, name):
    run = report['after']
    if run['status'] != 'completed' or run['worker_record_complete'] is not True:
        return 'inconclusive'
    return run['tests'].get(name, {}).get('status', 'missing')


def compare_go_recheck(source, current):
    """Compare candidate observations from two verified Go reports."""
    from .project_history import _baseline

    changes = []
    if _baseline(source, current) is not True:
        changes.append('固定基线不同或无法核对')
    if (source['go_package'] != current['go_package']
            or source['selected_tests'] != current['selected_tests']):
        changes.append('包或所选范围不同')
    if source['shared_tests'] != current['shared_tests']:
        changes.append('共享测试文件内容或清单已变化')
    for key, fields, label in (('runtime', ('go', 'go_version'), '记录的 Go 工具链'),
                               ('environment', ('platform', 'machine'), '平台或架构')):
        old = tuple((source.get(key) or {}).get(field) for field in fields)
        new = tuple((current.get(key) or {}).get(field) for field in fields)
        if not all((*old, *new)):
            changes.append(f'无法核对{label}')
        elif old != new:
            changes.append(f'{label}已变化')
    if source['timeout'] != current['timeout']:
        changes.append('每版限时已变化')
    if not source.get('reprolab_version') or source.get('reprolab_version') != current.get('reprolab_version'):
        changes.append('ReproLab 版本不同或无法核对')

    verdicts = {('failed', 'passed'): 'resolved', ('failed', 'failed'): 'still_failed',
                ('passed', 'failed'): 'new_failure', ('passed', 'passed'): 'still_passed'}
    entries = []
    for name in source['selected_tests']:
        previous, latest = _candidate_outcome(source, name), _candidate_outcome(current, name)
        verdict = verdicts.get((previous, latest))
        status = 'not_reviewed' if verdict is None else 'conditions_changed' if changes else verdict
        entries.append({'test_id': name, 'status': status, 'previous': previous, 'current': latest})
    return {'changes': changes, 'entries': entries, 'counts': dict(Counter(item['status'] for item in entries))}
