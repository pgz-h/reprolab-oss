"""Explicit pytest collection in a disposable copy of trusted local code."""
from .process_tree import spawn, terminate_tree
import ast
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from .case import parse_test_target
from .git_source import export_revision, resolve_commit
from .recipes import repository_root
from .runtime import choose_runtime, worker_command
from .pytest_profile import read_pytest_profile, profile_args
from .runtime_environment import resolve_variables
from .workspace import WORKTREE, copy_workspace, workspace_manifest

NOTICE = '实际收集会导入测试、conftest及项目钩子；仅用于可信代码。不启动测试主体或fixture准备，不代表验收通过。'


def list_workspace_tests(repository):
    root = repository_root(repository)
    entries, _ = workspace_manifest(root)
    tests, notes = [], []
    for relative in entries:
        if relative.suffix != '.py' or not (relative.name.startswith('test_') or relative.name.endswith('_test.py')):
            continue
        try:
            tree = ast.parse((root / relative).read_bytes(), filename=str(relative))
        except (SyntaxError, ValueError):
            notes.append(f'{relative}: 无法静态解析，可使用所选环境实际收集。')
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
                tests.append(f'{relative.as_posix()}::{node.name}')
            elif isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
                tests.extend(f'{relative.as_posix()}::{node.name}::{method.name}' for method in node.body
                             if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith('test'))
    return {'tests': tests, 'notes': notes, 'notice': '静态读取当前工作区；保存案例时重新冻结当时的内容。'}


def collect_project_tests(repository, ref='HEAD', *, target=None, python_executable=None, timeout=15):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 300:
        raise ValueError('收集时限必须为0.1–300秒。')
    if target:
        parse_test_target(target)
    root = repository_root(repository)
    runtime = choose_runtime(python_executable)
    revision = WORKTREE if ref == WORKTREE else resolve_commit(root, ref)
    with tempfile.TemporaryDirectory(prefix='reprolab-collect-') as temp:
        folder = Path(temp)
        source = folder / 'source'
        if revision == WORKTREE:
            frozen = copy_workspace(root, source)
        else:
            export_revision(root, revision, source)
            frozen = None
        raw_path, log_path = folder / 'collection.json', folder / 'collection.log'
        profile = read_pytest_profile(source)
        child_env, variables = resolve_variables(profile)
        command = worker_command(runtime['executable'], [str(source), str(folder / 'unused.py'), str(raw_path),
                                 json.dumps({'collect_only': True, 'test_target': target,
                                             'pytest_profile': profile, 'pytest_args': profile_args(profile), 'runtime_environment': variables})])
        timed_out = False
        with log_path.open('wb') as log:
            process = spawn(command, cwd=source, stdout=log, stderr=subprocess.STDOUT,
                                       env=child_env, start_new_session=(os.name == 'posix'))
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                code = terminate_tree(process)
        with log_path.open('rb') as log:
            size = log.seek(0, 2)
            log.seek(max(0, size - 12000))
            excerpt = log.read().decode('utf-8', 'replace')
        try:
            raw = json.loads(raw_path.read_text(encoding='utf-8')) if raw_path.stat().st_size <= 8 * 1024 * 1024 else {}
        except (OSError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    tests = raw.get('collected_tests')
    valid = (raw.get('runtime_environment') == variables and raw.get('collection_only') is True and raw.get('exit_code') == code
             and raw.get('events') == [] and isinstance(tests, list)
             and all(isinstance(test, str) and test for test in tests)
             and type(raw.get('collected')) is int and raw['collected'] >= len(tests))
    success = valid and not timed_out and code in (0, 5) and not raw.get('collection_errors')
    status = ('collected' if tests else 'empty') if success else 'collection_failed'
    return {'status': status, 'tests': tests if success else [], 'count': raw.get('collected') if valid else None,
            'truncated': bool(raw.get('collection_truncated')), 'tests_executed': False,
            'runtime': runtime, 'revision': revision, 'workspace': frozen, 'pytest_profile': profile, 'runtime_environment': variables,
            'collection_errors': raw.get('collection_errors', []), 'log_excerpt': excerpt,
            'timed_out': timed_out, 'notice': NOTICE,
            'explanation': ('收集完成，请选择一个具体测试。' if status == 'collected' else
                            '没有收集到测试，请检查目标和项目配置。' if status == 'empty' else
                            '收集超时，已停止工作进程。' if timed_out else '收集失败，请检查环境及下方日志；没有生成验收结论。')}
