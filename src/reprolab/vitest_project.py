"""Compare selected Vitest files and discover them from a frozen candidate."""

from collections import Counter
from datetime import datetime, timezone
from glob import iglob
import json
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
from .recipes import repository_root
from .repair_suite import _new_id
from .replay_toolchain import query_tool_version
from .runner import RunCancelled, RunControl, source_fingerprint, source_mode_fingerprint
from .workspace import WORKTREE, copy_workspace

NOTICE = ('按文件选择测试，按匹配的用例比较结果。两版使用对比版本的同一份测试文件，'
          '各自按锁文件安装依赖并保留项目配置。测试范围无法核对时，文件列为受阻。')
_REPORT_VERSION = 2
_FILE_STATUS_ORDER = ('regression_detected', 'inconclusive', 'baseline_failure', 'improved', 'regression_passed')
_SUFFIXES = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.mts', '.cjs', '.cts'}
_MAX_WORKER_BYTES = 8 * 1024 * 1024
_MAX_FILES = 2000
_DISCOVERY_FILES = {'discovery.json', 'discovery.log'}


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

    if not isinstance(files, list) or not 1 <= len(files) <= _MAX_FILES:
        raise ValueError(f'请选择 1–{_MAX_FILES} 个 Vitest 测试文件。')
    normalized = []
    for value in files:
        name = normalize_collection_path(value)
        path = PurePosixPath(name)
        if (not name or path.suffix not in _SUFFIXES
                or any(part in EXCLUDED or part.startswith('.env') for part in path.parts)):
            raise ValueError(f'请选择项目内的 JavaScript 或 TypeScript 测试文件：{value}')
        normalized.append(name)
    return list(dict.fromkeys(normalized))


def normalize_vitest_project_dir(value):
    from .project_collection import normalize_collection_path
    from .runner import EXCLUDED

    message = '请选择仓库内的 npm 项目目录，例如 . 或 frontend。'
    if isinstance(value, str) and value.strip().replace('\\', '/') in ('.', './'):
        return '.'
    try:
        directory = normalize_collection_path(value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if not directory or any(part in EXCLUDED or part.startswith('.env') for part in PurePosixPath(directory).parts):
        raise ValueError(message)
    return directory


def _require_project_selection(selected, project_dir):
    for name in selected:
        if not PurePosixPath(name).is_relative_to(project_dir):
            raise ValueError(f'所选测试文件不在 npm 项目目录 {project_dir} 中：{name}')


def _registry_package(item):
    return (isinstance(item, dict) and not item.get('link') and isinstance(item.get('integrity'), str)
            and str(item.get('resolved', '')).startswith(('https://', 'http://')))


def _workspace_links(root, declaration, packages):
    from .project_collection import normalize_collection_path

    locked_root = packages.get('')
    patterns = declaration.get('packages') if isinstance(declaration, dict) else declaration
    if not isinstance(locked_root, dict) or locked_root.get('workspaces') != declaration:
        raise ValueError('workspace 根目录的 package.json 与 package-lock.json 声明不同，请更新锁文件。')
    if not isinstance(patterns, list) or not patterns or not all(isinstance(value, str) for value in patterns):
        raise ValueError('workspace 需要以相对路径或 glob 声明成员目录。')
    declared = set()
    for pattern in patterns:
        path = pattern.lstrip('!')
        if (not path or path.startswith('/') or '..' in PurePosixPath(path).parts
                or any(character in path for character in '\\:{}()')
                or any(ord(character) < 32 for character in path)):
            raise ValueError('workspace 成员声明须位于 npm 根目录内，支持相对路径、通配符和排除规则。')
        matches = {Path(match).as_posix() for match in iglob(path, root_dir=root, recursive=True)
                   if (root / match / 'package.json').is_file()}
        if (len(pattern) - len(path)) % 2:
            declared.difference_update(matches)
        else:
            declared.update(matches)

    links = {}
    for name, item in packages.items():
        if not isinstance(item, dict) or item.get('link') is not True:
            continue
        target = item.get('resolved')
        if (not isinstance(target, str) or target not in packages
                or normalize_vitest_project_dir(target) != target or target == '.'
                or not name.startswith('node_modules/') or normalize_collection_path(name) != name):
            raise ValueError(f'锁文件含无法核对的 workspace 链接：{name}')
        manifest = _read_json(root / target / 'package.json', 1024 * 1024)
        if not manifest.get('name') or name != f'node_modules/{manifest["name"]}':
            raise ValueError(f'workspace 包名与锁文件链接不一致：{target}')
        links[name] = target
    if not links:
        raise ValueError('workspace 锁文件缺少成员链接，请更新 package-lock.json。')

    members = set(links.values())
    if members != declared:
        differences = sorted(members ^ declared)
        raise ValueError(f'workspace 成员与根锁文件不一致，请更新锁文件：{", ".join(differences[:8])}')
    for name, item in packages.items():
        if not name or name in links:
            continue
        if name in members:
            if not isinstance(item, dict) or item.get('link'):
                raise ValueError(f'锁文件中的 workspace 成员记录无效：{name}')
            continue
        prefix, marker, _ = ('/' + name).partition('/node_modules/')
        if (not marker or (prefix and prefix[1:] not in members)
                or normalize_collection_path(name) != name or not _registry_package(item)):
            raise ValueError(f'锁文件含未登记的本地依赖或非 registry 来源：{name}')
    return links


def _locked_project(snapshot, project_dir):
    project = snapshot / project_dir
    path = PurePosixPath(project_dir)
    root = next((snapshot / parent for parent in (path, *path.parents)
                 if (snapshot / parent / 'package-lock.json').is_file()), None)
    if root is None:
        raise ValueError('缺少 package-lock.json。')
    package = _read_json(root / 'package.json', 1024 * 1024)
    lock = _read_json(root / 'package-lock.json', 16 * 1024 * 1024)
    packages = lock.get('packages')
    if lock.get('lockfileVersion') not in (2, 3) or not isinstance(packages, dict):
        raise ValueError('Vitest 需要 v2/v3 package-lock.json。')

    member = project.relative_to(root).as_posix()
    links = {}
    if declaration := package.get('workspaces'):
        if (root / 'npm-shrinkwrap.json').exists():
            raise ValueError('workspace 安装使用 package-lock.json，请移除优先级更高的 npm-shrinkwrap.json。')
        links = _workspace_links(root, declaration, packages)
    elif any(name and (not name.startswith('node_modules/') or not _registry_package(item))
             for name, item in packages.items()):
        raise ValueError('Vitest 需要从 npm registry 锁定的依赖。当前锁文件含本地或其他来源。')
    if member != '.' and member not in links.values():
        raise ValueError(f'所选目录未登记为 npm workspace 成员：{member}。请核对根目录锁文件。')

    path = PurePosixPath(member)
    search = (path, *path.parents)
    vitest_path = next(((parent / 'node_modules/vitest').as_posix() for parent in search
                        if (parent / 'node_modules/vitest').as_posix() in packages), '')
    version = packages.get(vitest_path, {}).get('version') if vitest_path else None
    if not isinstance(version, str) or not re.fullmatch(r'4\.\d+\.\d+', version):
        raise ValueError('请在 npm 项目可用的 package-lock.json 中锁定 Vitest 4。')
    return {'root': root.relative_to(snapshot).as_posix(), 'links': links, 'vitest': vitest_path, 'version': version}


def _locked_projects(snapshots, project_dir):
    projects = {}
    for side, label in (('before', '基线'), ('after', '候选')):
        try:
            projects[side] = _locked_project(snapshots / side, project_dir)
        except (OSError, ValueError) as exc:
            reason = (f'缺少 {Path(exc.filename).name}。' if isinstance(exc, FileNotFoundError) and exc.filename
                      else str(exc))
            raise ValueError(f'{label}的 npm 项目目录 {project_dir}：{reason}') from exc
    scopes = [{'root': project['root'], 'workspaces': bool(project['links'])} for project in projects.values()]
    if scopes[0] != scopes[1]:
        raise ValueError('基线与候选的 npm 安装根目录或 workspace 模式不同。请选择使用相同布局的版本。')
    return projects, scopes[0]


def _recorded_install(record):
    return record.get('vitest_install', {'root': record.get('vitest_project_dir', '.'), 'workspaces': False})


def _runtime(node, npm, *, cwd=None, control=None):
    tools = {}
    for key, executable in (('node', node), ('npm', npm)):
        path = shutil.which(executable or key)
        if path is None:
            raise ValueError(f'找不到 {key}，请安装它或通过 --{key} 指定路径。')
        tools[key] = os.path.abspath(path)
    env = _runtime_environment(tools)
    for key in ('node', 'npm'):
        tools[key + '_version'] = query_tool_version(
            key, [tools[key], '--version'], env=env, cwd=cwd, control=control)
    return tools


def _runtime_environment(tools):
    env = os.environ.copy()
    for key in ('NODE_OPTIONS', 'NODE_PATH'):
        env.pop(key, None)
    env['PATH'] = os.pathsep.join((str(Path(tools['node']).parent), env.get('PATH', '')))
    return env


def _invoke_vitest(log_path, snapshots, label, selected, tools, timeout, offline, npm_cache, control,
                   *, project_dir, locked):
    start = time.monotonic()
    raw = {}
    phase = 'dependencies'
    env = _runtime_environment(tools)
    with tempfile.TemporaryDirectory(prefix='reprolab-vitest-') as temp, log_path.open('wb') as log:
        work = Path(temp).resolve()
        copied = work / 'project'
        shutil.copytree(snapshots / label, copied)
        for name in selected or ():
            destination = copied / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshots / 'after' / name, destination)
        project = copied / project_dir
        install_root = copied / locked['root']
        install = [tools['npm'], 'ci', '--prefix', str(install_root), '--no-global',
                   '--ignore-scripts', '--no-audit', '--no-fund', '--include=dev', '--no-progress']
        install.extend(('--workspaces', '--include-workspace-root') if locked['links'] else ('--workspaces=false',))
        if offline:
            install.append('--offline')
        if npm_cache:
            install.extend(('--cache', str(Path(npm_cache).expanduser().resolve())))
        deadline = start + timeout
        log.write(b'=== npm ci ===\n')
        log.flush()
        status, code = run_logged_command(install, install_root, env, log, deadline, control)
        if status == 'completed':
            if code != 0:
                status = 'dependencies_failed'
            else:
                for name, target in locked['links'].items():
                    link = install_root / name
                    if not link.is_dir() or link.resolve() != install_root / target:
                        log.write(f'安装后的 workspace 链接与锁文件不一致：{name}\n'.encode('utf-8'))
                        status = 'dependencies_failed'
                        break
        if status == 'completed':
            phase = 'vitest'
            result_path = work / 'vitest.json'
            log.write(b'\n=== Vitest ===\n')
            log.flush()
            worker = Path(__file__).with_name('_vitest_worker.mjs')
            options = work / 'selection.json'
            save_json(options, selected)
            expected = install_root / locked['vitest'] / 'package.json'
            command = [tools['node'], str(worker), str(result_path), str(options), str(copied), str(expected)]
            status, code = run_logged_command(command, project, env, log, deadline, control)
            if status == 'completed':
                try:
                    raw = _read_json(result_path, _MAX_WORKER_BYTES)
                except (OSError, ValueError, RecursionError) as exc:
                    log.write(('Vitest 记录无法读取：' + str(exc)).encode('utf-8'))
    execution = {'status': status, 'phase': phase, 'exit_code': code,
                 'duration_seconds': round(time.monotonic() - start, 3)}
    return execution, raw


def _version_run(report_dir, label, evidence, selected, tools, expected_version, timeout, offline, npm_cache,
                 control, *, project_dir, locked):
    execution, raw = _invoke_vitest(report_dir / (label + '.log'), evidence / 'inputs', label, selected,
                                    tools, timeout, offline, npm_cache, control, project_dir=project_dir, locked=locked)
    save_json(report_dir / (label + '.events.json'), raw)
    return execution, _derive_version(report_dir, label, raw, execution, selected, expected_version, _REPORT_VERSION)


def _discovered_files(snapshot, project_dir, expected_version, execution, raw):
    if (not isinstance(execution, dict) or execution.get('status') != 'completed'
            or execution.get('phase') != 'vitest' or execution.get('exit_code') != 0
            or not isinstance(raw, dict) or raw.get('schema_version') != _REPORT_VERSION
            or raw.get('vitest_version') != expected_version or raw.get('errors') != []
            or not isinstance(raw.get('files'), list)):
        raise ValueError('未取得完整的 Vitest 测试文件清单。')
    paths = raw['files']
    if not paths:
        raise ValueError(f'候选的 npm 项目目录 {project_dir} 没有匹配 Vitest 配置的测试文件。')
    if len(paths) > _MAX_FILES:
        raise ValueError(f'发现的 Vitest 测试文件超过 {_MAX_FILES} 项，请通过 --file 明确选择。')
    selected = _test_files(paths)
    if len(selected) != len(paths):
        raise ValueError('同一文件由多个 Vitest 测试配置匹配，请核对 projects 与 include。')
    if selected != paths:
        raise ValueError('Vitest 发现结果含非规范的测试文件路径。')
    _require_project_selection(selected, project_dir)
    for name in selected:
        path = snapshot / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'发现的测试文件未保存在候选快照中：{name}')
    return sorted(selected)


def _discover_vitest_files(directory, snapshots, tools, timeout, offline, npm_cache, control, *, project_dir, locked):
    log = directory / 'discovery.log'
    execution, raw = _invoke_vitest(log, snapshots, 'after', None, tools, timeout, offline, npm_cache, control,
                                    project_dir=project_dir, locked=locked)
    if execution['status'] == 'timed_out':
        raise ValueError(f'Vitest 测试发现超时（{timeout:g} 秒），最后阶段：{execution["phase"]}。')
    try:
        files = _discovered_files(snapshots / 'after', project_dir, locked['version'], execution, raw)
    except ValueError as exc:
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 1600))
            detail = stream.read().decode('utf-8', errors='replace').strip()
        raise ValueError(f'{exc}\n{detail}'.rstrip()) from exc
    return files, {'execution': execution, 'worker': raw}


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
                counts = [case.get(name) for name in ('retry_count', 'repeat_count')]
                if (any(count is not None and (type(count) is not int or count < 0) for count in counts)
                        or type(case.get('flaky')) is not bool):
                    return False
    return True


def _case_observation(case):
    status = case['status']
    retries = case.get('retry_count')
    retry_passed = status == 'passed' and (case['flaky'] or bool(retries) or bool(case['errors']))
    return {'name': case['name'], 'status': status, 'expected_failure': case['expected_failure'],
            'retry_count': retries, 'retry_passed': retry_passed,
            **({'repeat_count': case['repeat_count']} if 'repeat_count' in case else {}),
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


def _publish(stage, final, selected, metadata, tools, timeout, offline, npm_cache, control, *, project_dir):
    from .project_report import write_project_report
    from .project_sessions import _check_sources, _digest, _identity, _manifest

    evidence = stage / 'evidence/project'
    snapshots = evidence / 'inputs'
    locked, install = _locked_projects(snapshots, project_dir)
    discovered = selected is None
    if discovered:
        selected, discovery = _discover_vitest_files(stage, snapshots, tools, timeout, offline, npm_cache, control,
                                                      project_dir=project_dir, locked=locked['after'])
        save_json(stage / 'discovery.json', discovery)
    _require_project_selection(selected, project_dir)
    versions = {side: project['version'] for side, project in locked.items()}
    shared = {}
    for name in selected:
        path = evidence / 'inputs/after' / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'对比版本快照中没有所选测试文件：{name}')
        shared[name] = _digest(path)
    state = {**metadata, 'kind': 'project_session', 'schema_version': 1, 'runner': 'vitest',
             'directory': str(final / 'evidence/project'), 'runtime': tools, 'vitest_project_dir': project_dir,
             'vitest_install': install}
    state['archive_preflight'] = source_zip_preflight(snapshots, runner='vitest', control=control)
    save_json(evidence / 'project.json', state)
    _check_sources(evidence, state)
    runs, execution = {}, {'runner': 'vitest', 'project_id': state['project_id'], 'selected_tests': selected,
                           'runtime': tools, 'offline': offline, 'timeout': timeout,
                           'vitest_report_version': _REPORT_VERSION, 'vitest_project_dir': project_dir,
                           'vitest_install': install}
    for label in ('before', 'after'):
        execution[label], runs[label] = _version_run(stage, label, evidence, selected, tools,
                                                    versions[label], timeout, offline, npm_cache, control,
                                                    project_dir=project_dir, locked=locked[label])
    _check_sources(evidence, state)
    status, entries, counts = _compare(runs['before'], runs['after'], selected)
    report = {**_identity(state), **runs, 'schema_version': 1, 'kind': 'project_regression', 'review_summary_version': 3,
              'runner': 'vitest', 'vitest_report_version': _REPORT_VERSION, 'vitest_project_dir': project_dir,
              'vitest_install': install,
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
    if state.get('review_source') is not None:
        report['review_source'] = state['review_source']
    if discovered:
        report['vitest_discovery'] = execution['vitest_discovery'] = True
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


def _freeze_vitest_sources(root, before, after, snapshots, control):
    snapshots.mkdir(parents=True)
    control.check()
    export_revision(root, before, snapshots / 'before')
    control.check()
    if after == WORKTREE:
        copy_workspace(root, snapshots / 'after', control=control)
    else:
        export_revision(root, after, snapshots / 'after')


def list_vitest_files(repository, before, after, *, project_dir='.', node=None, npm=None,
                      timeout=120, offline=False, npm_cache=None, control=None):
    from .project_sessions import _timeout

    control = control or RunControl()
    project_dir, timeout = normalize_vitest_project_dir(project_dir), _timeout(timeout)
    root = repository_root(repository)
    tools = _runtime(node, npm)
    before = resolve_commit(root, before)
    after = after if after == WORKTREE else resolve_commit(root, after)
    with tempfile.TemporaryDirectory(prefix='reprolab-vitest-discovery-') as temporary:
        directory = Path(temporary)
        snapshots = directory / 'inputs'
        _freeze_vitest_sources(root, before, after, snapshots, control)
        locked, install = _locked_projects(snapshots, project_dir)
        files, _ = _discover_vitest_files(directory, snapshots, tools, timeout, offline, npm_cache, control,
                                          project_dir=project_dir, locked=locked['after'])
    return {'kind': 'vitest_file_discovery', 'schema_version': 1, 'status': 'collected',
            'vitest_project_dir': project_dir, 'vitest_install': install,
            'before_commit': before, 'after_commit': after, 'files': files}


def run_vitest_project(repository, before, after, files, output, *, project_dir='.', node=None, npm=None,
                       timeout=120, offline=False, npm_cache=None, control=None, acceptance_plan=None):
    from .project_sessions import _timeout

    control = control or RunControl()
    project_dir = normalize_vitest_project_dir(project_dir)
    selecting_from_plan = acceptance_plan is not None and files is None
    if acceptance_plan is not None:
        from .acceptance_plan import check_plan_scope, execution_selection, require_plan_runner

        require_plan_runner(acceptance_plan, 'vitest')
        if selecting_from_plan:
            files = execution_selection(acceptance_plan, 'vitest')
    selected = _test_files(files) if files is not None else None
    if selected is not None:
        if selecting_from_plan and selected != files:
            raise ValueError('验收计划须使用报告中的规范 Vitest 文件路径。')
        _require_project_selection(selected, project_dir)
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
        _freeze_vitest_sources(root, before, after, snapshots, control)
        metadata = {'project_id': _new_id('project'), 'title': root.name + ' · Vitest 项目回归',
                    'repository': str(root), 'before_commit': before, 'after_commit': after,
                    'excluded_tracked_changes': excluded_tracked_changes(root, before, None if after == WORKTREE else after),
                    'source_sha256': {side: source_fingerprint(snapshots / side)[1] for side in ('before', 'after')},
                    'source_mode_sha256': {side: source_mode_fingerprint(snapshots / side) for side in ('before', 'after')}}
        if acceptance_plan is not None:
            available = {name for name in selected if (snapshots / 'after' / name).is_file()}
            check_plan_scope(acceptance_plan, metadata, selected, available=available, vitest_project_dir=project_dir)
        return _publish(stage, final, selected, metadata, tools, timeout, offline, npm_cache, control,
                        project_dir=project_dir)


def check_vitest_evidence(folder, report, state, execution, selected):
    from .project_sessions import _digest, _read, _timeout

    if report.get('review_source') is not None:
        from .project_history import validate_report_reference

        reference = validate_report_reference(report['review_source'])
        if reference.get('selection_mode') != 'all' or reference.get('selected_tests') != selected:
            raise ValueError('Vitest 复查所选文件与来源引用不一致。')
    project_dir = normalize_vitest_project_dir(report.get('vitest_project_dir', '.'))
    if any(record.get('vitest_project_dir', '.') != project_dir for record in (state, execution, report)):
        raise ValueError('Vitest 项目目录与固定会话或执行记录不一致。')
    if (_test_files(selected) != selected or state.get('runner') != 'vitest' or execution.get('runner') != 'vitest'
            or report.get('runtime') != execution.get('runtime') or state['runtime'] != execution.get('runtime')
            or report.get('offline') != execution.get('offline') or _timeout(report.get('timeout')) != execution.get('timeout')):
        raise ValueError('Vitest 运行条件或测试文件选择与证据不一致。')
    record_version = report.get('vitest_report_version', 1)
    execution_version = execution.get('vitest_report_version', 1)
    if (type(record_version) is not int or record_version not in (1, _REPORT_VERSION)
            or type(execution_version) is not int or execution_version != record_version):
        raise ValueError('Vitest 报告格式与执行记录不一致。')
    _require_project_selection(selected, project_dir)
    snapshots = folder / 'evidence/project/inputs'
    locked, install = _locked_projects(snapshots, project_dir)
    if any(_recorded_install(record) != install for record in (state, report, execution)):
        raise ValueError('Vitest 的 npm 安装范围与锁文件或执行记录不一致。')
    versions = {side: project['version'] for side, project in locked.items()}
    if report.get('vitest_versions') != versions or report.get('shared_tests') != {
            name: _digest(snapshots / 'after' / name) for name in selected}:
        raise ValueError('Vitest 锁定版本或共享测试文件与证据不一致。')
    discovery = report.get('vitest_discovery', False)
    discovery_files = _DISCOVERY_FILES.intersection(report['persistence']['sha256'])
    if (type(discovery) is not bool or execution.get('vitest_discovery', False) is not discovery
            or discovery_files != (_DISCOVERY_FILES if discovery else set())):
        raise ValueError('Vitest 测试发现记录与证据清单不一致。')
    if discovery:
        saved = _read(folder / 'discovery.json')
        if _discovered_files(snapshots / 'after', project_dir, versions['after'],
                             saved.get('execution'), saved.get('worker')) != selected:
            raise ValueError('Vitest 发现清单与所选文件不一致。')
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
    project_dir = report.get('vitest_project_dir', '.')
    offline = report.get('offline', False) if offline is None else offline
    if not confirmed:
        return {'kind': 'project_replay_preview', 'status': 'ready', 'runner': 'vitest',
                'report_dir': str(folder), 'selected_tests': selected, 'count': len(selected),
                'timeout': timeout, 'offline': offline, 'source_runtime': report['runtime'],
                'vitest_project_dir': project_dir, 'vitest_install': _recorded_install(report)}
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
        return _publish(stage, final, selected, state, tools, timeout, offline, npm_cache, control,
                        project_dir=project_dir)


def recheck_vitest_project(directory, output, *, expected_report, repository=None, node=None, npm=None,
                           offline=None, npm_cache=None, timeout=None, confirmed=False, control=None):
    """Run the original Vitest scope with the saved baseline and a new worktree."""
    from .project_history import _checked_report, _match_reference
    from .project_refresh import _stage_worktree_recheck
    from .project_sessions import _timeout

    control = control or RunControl()
    control.check()
    source = _checked_report(directory, control=control)
    _match_reference(source, expected_report)
    if source.get('runner') != 'vitest':
        raise ValueError('请选择 Vitest 项目报告作为复查来源。')
    selected = source['selected_tests']
    project_dir = source.get('vitest_project_dir', '.')
    timeout = _timeout(source['timeout'] if timeout is None else timeout)
    offline = source.get('offline', False) if offline is None else offline
    if confirmed is not True:
        return {'kind': 'vitest_workspace_recheck_preview', 'status': 'ready', 'runner': 'vitest',
                'report_dir': source['report_dir'], 'selected_tests': selected, 'count': len(selected),
                'source_runtime': source['runtime'], 'timeout': timeout, 'offline': offline,
                'repository': source['repository'], 'vitest_project_dir': project_dir,
                'vitest_install': _recorded_install(source)}

    if npm_cache and Path(npm_cache).expanduser().resolve().is_relative_to(Path(source['report_dir'])):
        raise ValueError('npm 缓存须位于来源报告目录之外。')
    with _stage_worktree_recheck(source, output, repository, control=control) as (stage, final, metadata):
        tools = _runtime(node, npm)
        report = _publish(stage, final, selected, metadata, tools, timeout, offline, npm_cache, control,
                          project_dir=project_dir)
    return {'report': report, 'comparison': compare_vitest_recheck(source, report)}


def _recheck_conditions(source, current):
    from .project_history import _baseline

    changes = []
    if _baseline(source, current) is not True:
        changes.append('固定基线不同或无法核对')
    if source['selected_tests'] != current['selected_tests']:
        changes.append('所选测试文件范围不同')
    for key, fields, label in (('runtime', ('node', 'node_version', 'npm', 'npm_version'), '记录的 Node/npm 工具链'),
                               ('environment', ('platform', 'machine'), '平台或架构'),
                               ('vitest_versions', ('after',), '候选锁定的 Vitest 版本')):
        old, new = source.get(key), current.get(key)
        if not all(isinstance(record, dict) and all(record.get(field) for field in fields) for record in (old, new)):
            changes.append(f'无法核对{label}')
        elif any(old[field] != new[field] for field in fields):
            changes.append(f'{label}已变化')
    for key, label in (('offline', '离线安装模式'), ('timeout', '每版限时'),
                       ('reprolab_version', 'ReproLab 版本')):
        if source.get(key) is None or source.get(key) != current.get(key):
            changes.append(f'{label}不同或无法核对')
    project_dir = source.get('vitest_project_dir', '.')
    if project_dir != current.get('vitest_project_dir', '.'):
        changes.append('npm 项目目录不同')
        return changes
    install = _recorded_install(source)
    if install != _recorded_install(current):
        changes.append('npm 安装范围不同')
        return changes
    old_files, new_files = (report['persistence']['sha256'] for report in (source, current))
    snapshot = PurePosixPath('evidence/project/inputs/after')
    install_root = snapshot / install['root']
    project = snapshot / project_dir
    manifests = dict.fromkeys((install_root / 'package-lock.json', install_root / 'package.json', project / 'package.json'))
    for manifest in manifests:
        filename = manifest.relative_to(snapshot).as_posix()
        old, new = old_files.get(manifest.as_posix()), new_files.get(manifest.as_posix())
        if not old or not new:
            changes.append(f'无法核对候选的 {filename}')
        elif old != new:
            changes.append(f'候选的 {filename} 已变化')
    configs = (name for name in old_files.keys() | new_files.keys()
               if name.startswith(project.as_posix() + '/')
               and re.fullmatch(r'(vitest|vite)\.(config|workspace)\.(js|ts|mjs|mts|cjs|cts)',
                                PurePosixPath(name).name))
    if any(old_files.get(name) != new_files.get(name) for name in configs):
        changes.append('候选的 Vitest 配置文件已变化')
    return changes


def _recheck_candidate_file(report, path, label):
    run = report['after']
    file = run['tests'].get(path, {})
    if report.get('vitest_report_version', 1) < 2:
        return file, None, f'{label}报告缺少可核对的逐用例证据'
    if run['status'] != 'completed' or run['worker_record_complete'] is not True:
        return file, None, f'{label}候选执行不完整'
    if not file.get('comparable'):
        reason = file.get('reason') or '缺少可核对的用例清单'
        return file, None, f'{label}候选 · {reason}'
    return file, {case['name']: case for case in file['cases']}, None


def _recheck_case_outcome(cases, name):
    if cases is None:
        return 'inconclusive'
    case = cases.get(name)
    if case is None:
        return 'missing'
    if case['expected_failure']:
        return 'expected_failure'
    if case['retry_passed']:
        return 'retry_passed'
    return case['status'] if case['status'] in ('passed', 'failed', 'skipped') else 'inconclusive'


def compare_vitest_recheck(source, current):
    """Match actual candidate cases by file and full name from checked reports."""
    changes = _recheck_conditions(source, current)
    verdicts = {('failed', 'passed'): 'resolved', ('failed', 'failed'): 'still_failed',
                ('passed', 'failed'): 'new_failure', ('passed', 'passed'): 'still_passed'}
    files, totals = [], Counter()
    for path in source['selected_tests']:
        old_file, old, old_reason = _recheck_candidate_file(source, path, '上一轮')
        new_file, new, new_reason = _recheck_candidate_file(current, path, '本轮')
        reasons = [reason for reason in (old_reason, new_reason) if reason]
        if source['shared_tests'].get(path) != current['shared_tests'].get(path):
            reasons.append('共享测试文件内容已变化')
        if old is not None and new is not None:
            if old_file['project'] != new_file['project']:
                reasons.append('两轮候选所属的 Vitest 项目不同')
            if old.keys() != new.keys():
                reasons.append('两轮候选的实际用例清单不同')
        cases = []
        for name in dict.fromkeys([*(old or {}), *(new or {})]):
            previous, latest = _recheck_case_outcome(old, name), _recheck_case_outcome(new, name)
            verdict = verdicts.get((previous, latest))
            status = 'not_reviewed' if verdict is None else 'conditions_changed' if changes or reasons else verdict
            cases.append({'test_id': name, 'status': status, 'previous': previous, 'current': latest})
        counts = Counter(case['status'] for case in cases)
        totals.update(counts)
        files.append({'test_id': path, 'previous': old_file.get('status', 'missing'),
                      'current': new_file.get('status', 'missing'), 'case_entries': cases,
                      'counts': dict(counts), 'changes': reasons})
    return {'changes': changes, 'entries': files, 'counts': dict(totals)}
