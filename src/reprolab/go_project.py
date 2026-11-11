"""Compare selected Go tests in an explicitly selected package of one Git module."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tempfile
import time

from . import __version__
from .changes import describe_changes
from .git_source import export_revision, resolve_commit
from .persistence import save_json
from .process_tree import run_logged_command
from .project_collection import normalize_collection_path
from .recipes import repository_root
from .repair_suite import _new_id
from .runner import EXCLUDED, RunControl, source_fingerprint, source_mode_fingerprint
from .runtime import BASE_ENVIRONMENT_KEYS
from .workspace import WORKTREE, copy_workspace

NOTICE = ('两版在独立副本中使用候选版本同一包的测试文件，并保留各自的实现和模块依赖。'
          '仅比较所选的顶层 Go 测试；原始事件包含子测试。')
_EVENT_BYTES = 8 * 1024 * 1024
_OUTCOMES = {'pass': 'passed', 'fail': 'failed', 'skip': 'skipped'}


def _package(value):
    path = normalize_collection_path(value) if value not in ('.', './') else '.'
    if not path or any(part in EXCLUDED or part.startswith('.') for part in PurePosixPath(path).parts):
        raise ValueError('请选择仓库内单个 Go 包目录，例如 . 或 internal/cart。')
    return path


def _tests(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 2000
            or any(not isinstance(name, str) or not 4 < len(name) <= 200
                   or not name.startswith('Test') or not name.isidentifier() or name[4].islower() for name in values)):
        raise ValueError('请用 --test 指定顶层 Go 测试函数，例如 TestCheckout，可重复。')
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
        raise ValueError('候选版本的包目录中没有 *_test.go 文件。')
    return files


def _environment():
    keys = (*BASE_ENVIRONMENT_KEYS, 'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'GOCACHE', 'GOMODCACHE')
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(GOENV='off', GOWORK='off', GOTOOLCHAIN='local', GOPROXY='off', CGO_ENABLED='0')
    return env


def _runtime(go):
    executable = shutil.which(os.path.expanduser(go) if go else 'go')
    if executable is None:
        raise ValueError('找不到 Go，请安装工具链或通过 --go 指定路径。')
    executable = os.path.abspath(executable)
    result = subprocess.run([executable, 'version'], capture_output=True, encoding='utf-8',
                            errors='replace', timeout=10, env=_environment())
    if result.returncode or not result.stdout.startswith('go version '):
        raise ValueError('无法获取 Go 工具链版本。')
    return {'go': executable, 'go_version': result.stdout.strip()}


def _events(log):
    if log.stat().st_size > _EVENT_BYTES:
        return {'events': [], 'complete_json': False, 'reason': 'Go 事件超过 8 MiB。'}
    events, complete = [], True
    for line in log.read_bytes().splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get('Action'), str):
                raise ValueError('missing Go event action')
            events.append(event)
        except (ValueError, RecursionError):
            complete = False
    return {'events': events, 'complete_json': complete}


def _version(folder, label, execution, raw, selected):
    started, tests, package_ends, output = set(), {}, [], {}
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
            output[parent] = (output.get(parent, '') + event['Output'])[-4000:]
        if name != parent:
            continue
        if action == 'run':
            valid = valid and name not in started
            started.add(name)
        if action in _OUTCOMES:
            valid = valid and name in started and name not in tests
            elapsed = event.get('Elapsed', 0)
            tests[name] = {'status': _OUTCOMES[action], 'exception': None, 'phases': [],
                           'duration_seconds': elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) else 0}
    failed = any(result['status'] == 'failed' for result in tests.values())
    valid = (valid and len(packages) == 1 and started == tests.keys()
             and package_ends == (['fail'] if failed else ['pass']) and execution['exit_code'] == int(failed))
    for name, result in tests.items():
        if result['status'] == 'failed':
            result['exception'] = {'type': 'Go test', 'message': output.get(name, '').strip()}
        if not valid:
            result['observed_status'] = result['status']
            result['status'] = 'inconclusive'
    log = folder / (label + '.log')
    with log.open('rb') as stream:
        stream.seek(max(0, log.stat().st_size - 4000))
        excerpt = stream.read(4000).decode('utf-8', errors='replace')
    return {**execution, 'status': 'completed' if valid else (
            'worker_error' if execution['status'] == 'completed' else execution['status']),
            'worker_record_complete': valid, 'tests': tests,
            'timed_out': execution['status'] == 'timed_out',
            'reason': '' if valid else raw.get('reason', 'Go 未产生完整且可核对的执行记录。'),
            'log': log.name, 'events': label + '.events.json', 'log_excerpt': excerpt}


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
    raw = _events(log)
    save_json(stage / (label + '.events.json'), raw)
    return execution, _version(stage, label, execution, raw, selected)


def _publish(stage, final, state, package, selected, runtime, timeout, control):
    from .project_report import write_project_report
    from .project_sessions import _check_sources, _digest, _identity, _manifest, _matrix

    evidence = stage / 'evidence/project'
    snapshots = evidence / 'inputs'
    shared = {name: _digest(snapshots / 'after' / name) for name in _test_files(snapshots, package)}
    state = {**state, 'schema_version': 1, 'kind': 'project_session', 'runner': 'go',
             'directory': str(final / 'evidence/project'), 'runtime': runtime, 'go_package': package}
    _check_sources(evidence, state)
    save_json(evidence / 'project.json', state)
    execution = {'runner': 'go', 'project_id': state['project_id'], 'selected_tests': selected,
                 'go_package': package, 'runtime': runtime, 'timeout': timeout}
    runs = {}
    for label in ('before', 'after'):
        execution[label], runs[label] = _run_version(
            stage, label, snapshots, package, shared, selected, runtime, timeout, control)
    _check_sources(evidence, state)
    status, entries, counts = _matrix(runs['before'], runs['after'], selected)
    report = {**_identity(state), **runs, 'schema_version': 1, 'kind': 'project_regression',
              'reprolab_version': __version__, 'run_id': final.name, 'report_dir': str(stage),
              'project_directory': state['directory'], 'created_at': datetime.now(timezone.utc).isoformat(),
              'title': state['title'], 'repository': state['repository'], 'before_commit': state['before_commit'],
              'after_commit': state['after_commit'], 'go_package': package, 'runtime': runtime, 'timeout': timeout,
              'selected_tests': selected, 'shared_tests': shared, 'status': status, 'entries': entries, 'counts': counts,
              'environment': {'platform': platform.system(), 'machine': platform.machine()},
              'changes': describe_changes(snapshots / 'before', snapshots / 'after'), 'notice': NOTICE}
    save_json(stage / 'selected.json', {'selected_tests': selected, 'project_id': state['project_id']})
    save_json(stage / 'execution.json', execution)
    write_project_report(report)
    report.update(report_dir=str(final), persistence=_manifest(stage))
    save_json(stage / 'report.json', report)
    control.commit()
    stage.rename(final)
    return report


def run_go_project(repository, before, after, output, *, package, selected, go=None, timeout=120, control=None):
    from .project_sessions import _timeout

    control = control or RunControl()
    package, selected, timeout = _package(package), _tests(selected), _timeout(timeout)
    root = repository_root(repository)
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(root):
        raise ValueError('请将报告保存在被测仓库外。')
    runtime = _runtime(go)
    before = resolve_commit(root, before)
    after = after if after == WORKTREE else resolve_commit(root, after)
    output.mkdir(parents=True, exist_ok=True)
    final = output / _new_id('go-run')
    with tempfile.TemporaryDirectory(prefix='.go-run-', dir=output) as directory:
        stage = Path(directory) / final.name
        snapshots = stage / 'evidence/project/inputs'
        snapshots.mkdir(parents=True)
        export_revision(root, before, snapshots / 'before')
        if after == WORKTREE:
            copy_workspace(root, snapshots / 'after', control=control)
        else:
            export_revision(root, after, snapshots / 'after')
        state = {'project_id': _new_id('project'), 'title': root.name + ' · Go 项目回归',
                 'repository': str(root), 'before_commit': before, 'after_commit': after,
                 'source_sha256': {side: source_fingerprint(snapshots / side)[1] for side in ('before', 'after')},
                 'source_mode_sha256': {side: source_mode_fingerprint(snapshots / side) for side in ('before', 'after')}}
        return _publish(stage, final, state, package, selected, runtime, timeout, control)


def check_go_evidence(folder, report, state, execution, selected):
    from .project_sessions import _digest, _matrix, _read, _timeout

    package = _package(report['go_package'])
    if (_tests(selected) != selected or state.get('runner') != 'go' or execution.get('runner') != 'go'
            or not all(record.get('go_package') == package for record in (state, report, execution))
            or report['runtime'] != state['runtime'] or report['runtime'] != execution['runtime']
            or _timeout(report['timeout']) != execution['timeout']):
        raise ValueError('Go 运行条件与证据不一致。')
    snapshots = folder / 'evidence/project/inputs'
    if report['shared_tests'] != {name: _digest(snapshots / 'after' / name) for name in _test_files(snapshots, package)}:
        raise ValueError('Go 共享测试文件与证据不一致。')
    runs = {}
    for label in ('before', 'after'):
        raw = _events(folder / (label + '.log'))
        if raw != _read(folder / (label + '.events.json')):
            raise ValueError('Go 事件与原始日志不一致。')
        runs[label] = _version(folder, label, execution[label], raw, selected)
        if report[label] != runs[label]:
            raise ValueError('Go 运行摘要与原始记录不一致。')
    if _matrix(runs['before'], runs['after'], selected) != (report['status'], report['entries'], report['counts']):
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
