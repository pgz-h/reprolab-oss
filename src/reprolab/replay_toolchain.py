"""Inspect the chosen local environment before a project replay."""
import subprocess
import tempfile
import time

from .process_tree import spawn, terminate_tree


_VERSION_TIMEOUT = 10


def _controlled_query(command, env, cwd, control):
    control.check()
    with spawn(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
               stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        deadline = time.monotonic() + _VERSION_TIMEOUT
        try:
            while True:
                control.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, _VERSION_TIMEOUT)
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                control.check()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            terminate_tree(process)


def query_tool_version(tool, command, *, env, cwd=None, control=None):
    try:
        if control is None:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                                    timeout=_VERSION_TIMEOUT, check=False)
        else:
            result = _controlled_query(command, env, cwd, control)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f'{tool} 版本查询超时，请核对本机工具路径后重试。') from exc
    except OSError as exc:
        raise ValueError(f'无法启动 {tool}，请检查本机工具路径和执行权限。\n{exc}') from exc
    version = result.stdout.decode('utf-8', errors='replace').strip()
    if result.returncode or not version:
        details = result.stderr.decode('utf-8', errors='replace').strip()[:500]
        message = f'无法获取 {tool} 版本（退出码 {result.returncode}），请核对本机工具路径后重试。'
        raise ValueError(message + ('\n' + details if details else ''))
    return version


def probe_replay_tools(runner, *, go=None, node=None, npm=None, control=None):
    """Resolve the local tools and query their versions outside the evidence."""
    with tempfile.TemporaryDirectory(prefix='reprolab-replay-tools-') as cwd:
        if runner == 'go':
            from .go_project import _runtime

            return _runtime(go, cwd=cwd, control=control)
        if runner == 'vitest':
            from .vitest_project import _runtime

            return _runtime(node, npm, cwd=cwd, control=control)
    raise ValueError('此报告不支持本机工具检查。')


def inspect_pytest_replay_environment(source, report, executable, *, control=None):
    from .environment import compare_environments
    from .project_environment import inspect_project
    from .runtime import choose_runtime

    if control:
        control.check()
    try:
        runtime = choose_runtime(executable)
    except ValueError as exc:
        return {'status': 'blocked', 'runtime': {'executable': executable}, 'issues': [str(exc)],
                'dependencies': [], 'plugins': [], 'comparison': compare_environments(None, None)}
    if control:
        control.check()
    inspected = inspect_project(source, runtime['executable'])
    if control:
        control.check()
    comparison = compare_environments(report.get('environment'), inspected['environment'])
    plugins = inspected['pytest_plugins']['declared']
    dependencies = [item for item in inspected['dependencies']
                    if item['status'] not in ('satisfied', 'not-applicable')]
    status = inspected['reuse']['status']
    if status == 'ready' and (comparison['status'] == 'unknown'
            or any(comparison[key] for key in ('added', 'removed', 'changed', 'python_changed', 'platform_changed'))
            or any(item['status'] not in ('installed', 'local') for item in plugins)):
        status = 'review'
    return {'status': status, 'runtime': runtime, 'issues': inspected['issues'],
            'dependencies': dependencies, 'plugins': plugins, 'comparison': comparison}
