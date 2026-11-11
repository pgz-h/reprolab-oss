"""Run an explicitly selected set of Vitest files in two frozen npm projects."""

from collections import Counter
from datetime import datetime, timezone
import json
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
from .recipes import repository_root
from .repair_suite import _new_id
from .runner import RunCancelled, RunControl, source_fingerprint, source_mode_fingerprint
from .workspace import WORKTREE, copy_workspace

NOTICE = ('按文件选择测试，按匹配的用例比较结果。两版使用对比版本的同一份测试文件，'
          '各自按锁文件安装依赖并保留项目配置。测试范围无法核对时，文件列为受阻。')
_REPORT_VERSION = 2
_FILE_STATUS_ORDER = ('regression_detected', 'inconclusive', 'baseline_failure', 'improved', 'regression_passed')
_SUFFIXES = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.mts', '.cjs', '.cts'}
_MAX_WORKER_BYTES = 8 * 1024 * 1024


def _read_json(path, limit):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{path.name} 超过读取上限。')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f'{path.name} 必须包含 JSON 对象。')
    return value


def _test_files(files):
    from .project_collection import normalize_collection_path
    from .runner import EXCLUDED

    if not isinstance(files, list) or not 1 <= len(files) <= 2000:
        raise ValueError('请通过 --file 选择 1–2000 个 Vitest 测试文件。')
    normalized = []
    for value in files:
        name = normalize_collection_path(value)
        path = PurePosixPath(name)
        if (not name or path.suffix not in _SUFFIXES
                or any(part in EXCLUDED or part.startswith('.env') for part in path.parts)):
            raise ValueError(f'请选择项目内的 JavaScript 或 TypeScript 测试文件：{value}')
        normalized.append(name)
    return list(dict.fromkeys(normalized))


def _vitest_version(root):
    package = _read_json(root / 'package.json', 1024 * 1024)
    lock = _read_json(root / 'package-lock.json', 16 * 1024 * 1024)
    packages = lock.get('packages')
    if package.get('workspaces') or lock.get('lockfileVersion') not in (2, 3) or not isinstance(packages, dict):
        raise ValueError('Vitest 预览需要独立 npm 项目和 v2/v3 package-lock.json。')
    for name, item in packages.items():
        if name and (not name.startswith('node_modules/') or not isinstance(item, dict)
                     or item.get('link') or not isinstance(item.get('integrity'), str)
                     or not str(item.get('resolved', '')).startswith(('https://', 'http://'))):
            raise ValueError('Vitest 预览需要从 npm registry 锁定的依赖。当前锁文件含本地或其他来源。')
    version = packages.get('node_modules/vitest', {}).get('version')
    if not isinstance(version, str) or not re.fullmatch(r'4\.\d+\.\d+', version):
        raise ValueError('请在根项目的 package-lock.json 中锁定 Vitest 4。')
    return version


def _runtime(node, npm):
    tools = {}
    for key, executable in (('node', node), ('npm', npm)):
        path = shutil.which(executable or key)
        if path is None:
            raise ValueError(f'找不到 {key}，请安装它或通过 --{key} 指定路径。')
        tools[key] = os.path.abspath(path)
    env = _runtime_environment(tools)
    for key in ('node', 'npm'):
        result = subprocess.run([tools[key], '--version'], capture_output=True, text=True,
                                timeout=10, check=False, env=env)
        if result.returncode or not result.stdout.strip():
            raise ValueError(f'无法获取 {key} 版本。')
        tools[key + '_version'] = result.stdout.strip()
    return tools


def _runtime_environment(tools):
    env = os.environ.copy()
    for key in ('NODE_OPTIONS', 'NODE_PATH'):
        env.pop(key, None)
    env['PATH'] = os.pathsep.join((str(Path(tools['node']).parent), env.get('PATH', '')))
    return env


def _version_run(report_dir, label, evidence, selected, tools, expected_version, timeout, offline, npm_cache, control):
    start = time.monotonic()
    log_path = report_dir / (label + '.log')
    raw_path = report_dir / (label + '.events.json')
    raw = {}
    phase = 'dependencies'
    env = _runtime_environment(tools)
    with tempfile.TemporaryDirectory(prefix='reprolab-vitest-') as temp, log_path.open('wb') as log:
        work = Path(temp).resolve()
        copied = work / 'project'
        shutil.copytree(evidence / 'inputs' / label, copied)
        for name in selected:
            destination = copied / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(evidence / 'inputs/after' / name, destination)
        install = [tools['npm'], 'ci', '--prefix', str(copied), '--no-global',
                   '--ignore-scripts', '--no-audit', '--no-fund', '--include=dev', '--no-progress']
        if offline:
            install.append('--offline')
        if npm_cache:
            install.extend(('--cache', str(Path(npm_cache).expanduser().resolve())))
        deadline = start + timeout
        log.write(b'=== npm ci ===\n')
        log.flush()
        status, code = run_logged_command(install, copied, env, log, deadline, control)
        if status == 'completed' and code != 0:
            status = 'dependencies_failed'
        if status == 'completed':
            phase = 'vitest'
            result_path = work / 'vitest.json'
            log.write(b'\n=== Vitest ===\n')
            log.flush()
            worker = Path(__file__).with_name('_vitest_worker.mjs')
            options = work / 'selection.json'
            save_json(options, selected)
            status, code = run_logged_command([tools['node'], str(worker), str(result_path), str(options)],
                                    copied, env, log, deadline, control)
            if status == 'completed':
                try:
                    raw = _read_json(result_path, _MAX_WORKER_BYTES)
                except (OSError, ValueError, RecursionError) as exc:
                    log.write(('Vitest 记录无法读取：' + str(exc)).encode('utf-8'))
    save_json(raw_path, raw)
    execution = {'status': status, 'phase': phase, 'exit_code': code,
                 'duration_seconds': round(time.monotonic() - start, 3)}
    return execution, _derive_version(report_dir, label, raw, execution, selected, expected_version, _REPORT_VERSION)


def _derive_version(folder, label, raw, execution, selected, expected_version, record_version=1):
    tests = {name: {'status': 'inconclusive', 'test_names': []} for name in selected}
    status = execution['status']
    errors = raw.get('errors', [])
    files = raw.get('files')
    complete = (status == 'completed' and execution['phase'] == 'vitest' and execution['exit_code'] == 0
                and raw.get('schema_version') == record_version and isinstance(errors, list)
                and _valid_files(files, record_version))
    if status == 'completed':
        if not complete:
            status = 'worker_error'
        elif errors:
            status = 'collection_failed'
        elif raw.get('vitest_version') != expected_version:
            status = 'worker_error'
            errors = [{'type': 'VitestVersion', 'message': '执行的 Vitest 与锁定版本不同。'}]
        elif sorted(item['path'] for item in files) != sorted(selected):
            status = 'collection_failed'
            errors = [{'type': 'VitestSelection', 'message': '返回的测试文件与所选范围不同。'}]
        else:
            for item in files:
                cases = item['tests']
                names = sorted(case['name'] for case in cases)
                reason = _blocked_file_reason(item['errors'], cases, names)
                outcome = 'inconclusive' if reason else 'failed' if any(
                    case['status'] == 'failed' for case in cases) else 'passed'
                errors_in_file = item['errors'] + [error for case in cases for error in case['errors']]
                tests[item['path']] = {'status': outcome, 'test_names': names, 'reason': reason,
                                       'exception': errors_in_file[0] if errors_in_file else None,
                                       'cases': [{'name': case['name'], 'status': case['status'],
                                                  'expected_failure': case['expected_failure']} for case in cases]}
                if record_version >= 2:
                    tests[item['path']].update(
                        project=item['project'],
                        comparable=bool(cases) and not item['errors'] and len(set(names)) == len(names),
                        cases=[_case_observation(case) for case in cases],
                    )
    log_name = label + '.log'
    with (folder / log_name).open('rb') as log:
        size = log.seek(0, os.SEEK_END)
        log.seek(max(0, size - 20000))
        excerpt = log.read().decode('utf-8', 'replace')
    return {**execution, 'status': status, 'tests': tests, 'worker_record_complete': complete,
            'timed_out': status == 'timed_out', 'vitest_version': raw.get('vitest_version'),
            'log_excerpt': excerpt, 'log': log_name, 'events': label + '.events.json',
            'collection_errors': errors}


def _valid_files(files, record_version):
    if not isinstance(files, list):
        return False
    for item in files:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not isinstance(item.get('errors'), list) or not isinstance(item.get('tests'), list)):
            return False
        if record_version >= 2 and not isinstance(item.get('project'), str):
            return False
        for case in item['tests']:
            if (not isinstance(case, dict) or not isinstance(case.get('name'), str)
                    or not isinstance(case.get('status'), str) or type(case.get('expected_failure')) is not bool
                    or not isinstance(case.get('errors'), list)):
                return False
            if record_version >= 2:
                retries = case.get('retry_count')
                if ((retries is not None and (type(retries) is not int or retries < 0))
                        or type(case.get('flaky')) is not bool):
                    return False
    return True


def _case_observation(case):
    status = case['status']
    retries = case.get('retry_count')
    retry_passed = status == 'passed' and (case['flaky'] or bool(retries) or bool(case['errors']))
    return {'name': case['name'], 'status': status, 'expected_failure': case['expected_failure'],
            'retry_count': retries, 'retry_passed': retry_passed,
            'exception': case['errors'][0] if case['errors'] else None}


def _blocked_file_reason(errors, cases, names):
    if errors:
        return '测试文件收集或套件级执行出错。'
    if not cases:
        return '没有可确认的逐测试执行记录。'
    if len(set(names)) != len(names):
        return '同一文件内有重名测试，无法核对两版范围。'
    if any(case['expected_failure'] for case in cases):
        return '文件包含预期失败标记。'
    if any(case['status'] not in ('passed', 'failed') for case in cases):
        return '文件中有跳过或未完成的测试。'
    return ''


def _compare_legacy(before, after, selected):
    # Replay verification for file-only reports uses the rules recorded at export.
    from .project_sessions import _matrix

    mismatched = set()
    for file in selected:
        before_names = before['tests'][file]['test_names']
        after_names = after['tests'][file]['test_names']
        if before_names and after_names and before_names != after_names:
            mismatched.add(file)
    status, entries, counts = _matrix(before, after, selected)
    for entry in entries:
        if entry['test_id'] in mismatched:
            previous = entry['status']
            if previous != 'inconclusive':
                counts[previous] -= 1
                counts['inconclusive'] = counts.get('inconclusive', 0) + 1
            entry['status'] = 'inconclusive'
            entry['reason'] = '两版实际收集的测试名称不同。'
    counts = {key: count for key, count in counts.items() if count}
    if mismatched:
        status = 'regression_detected' if counts.get('regression_detected') else 'attention'
    return status, entries, counts


def _case_result(case):
    if case['expected_failure'] or case['retry_passed'] or case['status'] not in ('passed', 'failed'):
        return {**case, 'status': 'inconclusive'}
    return case


def _compare(before, after, selected):
    from .project_sessions import _matrix, _regression_status

    entries = []
    for file in selected:
        old, new = before['tests'][file], after['tests'][file]
        entry = {'test_id': file, 'status': 'inconclusive', 'before': old, 'after': new, 'case_entries': []}
        entries.append(entry)
        if not old.get('comparable') or not new.get('comparable'):
            continue
        if old['project'] != new['project']:
            entry['reason'] = '两版测试所属的 Vitest 项目不同。'
            continue
        if old['test_names'] != new['test_names']:
            entry['reason'] = '两版实际收集的测试名称不同。'
            continue
        left = {case['name']: case for case in old['cases']}
        right = {case['name']: case for case in new['cases']}
        _, cases, counts = _matrix(
            {'status': 'completed', 'tests': {name: _case_result(case) for name, case in left.items()}},
            {'status': 'completed', 'tests': {name: _case_result(case) for name, case in right.items()}},
            list(left),
        )
        for case in cases:
            name = case['test_id']
            case.update(before=left[name], after=right[name])
        entry['case_entries'] = cases
        entry['status'] = next(status for status in _FILE_STATUS_ORDER if counts.get(status))
    counts = dict(Counter(entry['status'] for entry in entries))
    return _regression_status(before, after, counts), entries, counts


def _case_counts(entries):
    return dict(Counter(case['status'] for entry in entries for case in entry['case_entries']))


def _publish(stage, final, selected, metadata, tools, timeout, offline, npm_cache, control):
    from .project_report import write_project_report
    from .project_sessions import _check_sources, _digest, _identity, _manifest

    evidence = stage / 'evidence/project'
    versions = {side: _vitest_version(evidence / 'inputs' / side) for side in ('before', 'after')}
    shared = {}
    for name in selected:
        path = evidence / 'inputs/after' / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'对比版本快照中没有所选测试文件：{name}')
        shared[name] = _digest(path)
    state = {**metadata, 'kind': 'project_session', 'schema_version': 1, 'runner': 'vitest',
             'directory': str(final / 'evidence/project'), 'runtime': tools}
    save_json(evidence / 'project.json', state)
    _check_sources(evidence, state)
    runs, execution = {}, {'runner': 'vitest', 'project_id': state['project_id'], 'selected_tests': selected,
                           'runtime': tools, 'offline': offline, 'timeout': timeout,
                           'vitest_report_version': _REPORT_VERSION}
    for label in ('before', 'after'):
        execution[label], runs[label] = _version_run(stage, label, evidence, selected, tools,
                                                    versions[label], timeout, offline, npm_cache, control)
    _check_sources(evidence, state)
    status, entries, counts = _compare(runs['before'], runs['after'], selected)
    report = {**_identity(state), **runs, 'schema_version': 1, 'kind': 'project_regression',
              'runner': 'vitest', 'vitest_report_version': _REPORT_VERSION,
              'reprolab_version': __version__, 'run_id': final.name,
              'report_dir': str(stage), 'project_directory': state['directory'],
              'created_at': datetime.now(timezone.utc).isoformat(),
              'title': state['title'], 'repository': state['repository'],
              'before_commit': state['before_commit'], 'after_commit': state['after_commit'],
              'selected_tests': selected, 'shared_tests': shared, 'status': status, 'entries': entries,
              'counts': counts, 'case_counts': _case_counts(entries),
              'runtime': tools, 'timeout': timeout, 'offline': offline, 'vitest_versions': versions,
              'environment': {'platform': platform.system(), 'machine': platform.machine()},
              'changes': describe_changes(evidence / 'inputs/before', evidence / 'inputs/after'), 'notice': NOTICE}
    save_json(stage / 'selected.json', {'selected_tests': selected, 'project_id': state['project_id']})
    save_json(stage / 'execution.json', execution)
    write_project_report(report)
    report.update(report_dir=str(final), persistence=_manifest(stage))
    save_json(stage / 'report.json', report)
    control.commit()
    stage.rename(final)
    return report


def run_vitest_project(repository, before, after, files, output, *, node=None, npm=None,
                       timeout=120, offline=False, npm_cache=None, control=None):
    from .project_sessions import _timeout

    control = control or RunControl()
    selected = _test_files(files)
    _timeout(timeout)
    root = repository_root(repository)
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(root):
        raise ValueError('请将报告保存在被测仓库外。')
    tools = _runtime(node, npm)
    before = resolve_commit(root, before)
    after = after if after == WORKTREE else resolve_commit(root, after)
    output.mkdir(parents=True, exist_ok=True)
    final = output / _new_id('vitest-run')
    with tempfile.TemporaryDirectory(prefix='.vitest-run-', dir=output) as temp:
        stage = Path(temp) / final.name
        snapshots = stage / 'evidence/project/inputs'
        snapshots.mkdir(parents=True)
        export_revision(root, before, snapshots / 'before')
        if after == WORKTREE:
            copy_workspace(root, snapshots / 'after', control=control)
        else:
            export_revision(root, after, snapshots / 'after')
        metadata = {'project_id': _new_id('project'), 'title': root.name + ' · Vitest 项目回归',
                    'repository': str(root), 'before_commit': before, 'after_commit': after,
                    'source_sha256': {side: source_fingerprint(snapshots / side)[1] for side in ('before', 'after')},
                    'source_mode_sha256': {side: source_mode_fingerprint(snapshots / side) for side in ('before', 'after')}}
        return _publish(stage, final, selected, metadata, tools, timeout, offline, npm_cache, control)


def check_vitest_evidence(folder, report, state, execution, selected):
    from .project_sessions import _digest, _read, _timeout

    if (_test_files(selected) != selected or state.get('runner') != 'vitest' or execution.get('runner') != 'vitest'
            or report.get('runtime') != execution.get('runtime') or state['runtime'] != execution.get('runtime')
            or report.get('offline') != execution.get('offline') or _timeout(report.get('timeout')) != execution.get('timeout')):
        raise ValueError('Vitest 运行条件或测试文件选择与证据不一致。')
    record_version = report.get('vitest_report_version', 1)
    execution_version = execution.get('vitest_report_version', 1)
    if (type(record_version) is not int or record_version not in (1, _REPORT_VERSION)
            or type(execution_version) is not int or execution_version != record_version):
        raise ValueError('Vitest 报告格式与执行记录不一致。')
    snapshots = folder / 'evidence/project/inputs'
    versions = {side: _vitest_version(snapshots / side) for side in ('before', 'after')}
    if report.get('vitest_versions') != versions or report.get('shared_tests') != {
            name: _digest(snapshots / 'after' / name) for name in selected}:
        raise ValueError('Vitest 锁定版本或共享测试文件与证据不一致。')
    runs = {}
    for label in ('before', 'after'):
        raw = _read(folder / (label + '.events.json'))
        runs[label] = _derive_version(folder, label, raw, execution[label], selected, versions[label], record_version)
        if report.get(label) != runs[label]:
            raise ValueError('Vitest 运行结果与原始记录不一致。')
    compare = _compare_legacy if record_version == 1 else _compare
    status, entries, counts = compare(runs['before'], runs['after'], selected)
    if (status, entries, counts) != (report.get('status'), report.get('entries'), report.get('counts')):
        raise ValueError('Vitest 回归判断与原始记录不一致。')
    if record_version >= 2 and report.get('case_counts') != _case_counts(entries):
        raise ValueError('Vitest 用例统计与原始记录不一致。')


def replay_vitest_project(folder, report, output, *, node, npm, timeout, offline, npm_cache, confirmed, control):
    from .project_sessions import _copy_session, _timeout, read_project, recorded_project_timeout

    timeout = _timeout(timeout if timeout is not None else recorded_project_timeout(report) or 120)
    selected = report['selected_tests']
    offline = report.get('offline', False) if offline is None else offline
    if not confirmed:
        return {'kind': 'project_replay_preview', 'status': 'ready', 'runner': 'vitest',
                'report_dir': str(folder), 'selected_tests': selected, 'count': len(selected),
                'timeout': timeout, 'offline': offline, 'source_runtime': report['runtime']}
    control = control or RunControl()
    state = read_project(folder / 'evidence/project')
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(folder) or output.is_relative_to(Path(state['repository']).resolve()):
        raise ValueError('请将重放报告保存在来源报告和原仓库外。')
    tools = _runtime(node, npm)
    output.mkdir(parents=True, exist_ok=True)
    final = output / _new_id('vitest-run')
    with tempfile.TemporaryDirectory(prefix='.vitest-replay-', dir=output) as temp:
        stage = Path(temp) / final.name
        _copy_session(state, stage / 'evidence/project')
        return _publish(stage, final, selected, state, tools, timeout, offline, npm_cache, control)
