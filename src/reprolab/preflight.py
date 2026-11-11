"""Read-only input checks. Never import or collect the project under test."""
import ast
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

from .case import load_case, parse_test_target, validate_draft_review
from .environment import snapshot_environment, compare_environments
from .runtime import child_environment

NOTICE = '预检只读取本地输入和可信解释器元数据，不执行测试；不能保证动态收集恰好一个测试或项目依赖可用。ready 不代表修复已验证。'


_SYNTAX_PROBE = """import ast,json,pathlib,sys
try:
    source=pathlib.Path(sys.argv[1]).read_bytes()
    if len(source)>4*1024*1024: raise ValueError('file too large')
    ast.parse(source,filename='<regression>')
except SyntaxError as exc:
    print(json.dumps({'ok':False,'line':exc.lineno}))
except (OSError,ValueError,UnicodeError):
    print(json.dumps({'ok':False}))
else:
    print(json.dumps({'ok':True}))
"""


def _test_syntax(path, executable=None):
    if path.is_symlink() or not path.is_file() or path.suffix != '.py':
        raise ValueError('请选择存在的普通 Python 测试文件，不支持符号链接。')
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('独立测试文件过大，未进行静态检查。')
    if executable is None:
        # The unavailable-environment check already blocks execution. Preserve
        # path guards without making a syntax claim using a different Python.
        return path.resolve()
    if executable == os.path.abspath(sys.executable):
        # An own frozen executable is an app dispatcher, not Python -c.
        try:
            ast.parse(path.read_bytes(), filename='<regression>')
        except SyntaxError as exc:
            raise ValueError(f'测试文件在所选 Python 中存在语法错误（行 {exc.lineno}）。') from None
    else:
        try:
            with tempfile.TemporaryFile() as output:
                process = subprocess.run([executable, '-I', '-X', 'utf8', '-c', _SYNTAX_PROBE, str(path)],
                                         stdout=output, stderr=subprocess.DEVNULL,
                                         timeout=10, env=child_environment(), cwd=tempfile.gettempdir())
                if process.returncode or output.tell() > 4096:
                    raise ValueError('所选 Python 的静态语法探测失败。')
                output.seek(0)
                response = json.loads(output.read(4096).decode('utf-8'))
        except (OSError, ValueError, subprocess.SubprocessError):
            # Never retain interpreter startup output or source/error text.
            raise ValueError('所选 Python 的静态语法探测不可用。') from None
        if not isinstance(response, dict) or response.get('ok') is not True:
            raise ValueError('测试文件未通过所选 Python 的静态语法检查。')
    return path.resolve()


def _output_check(output, sources, protected):
    output = Path(output).expanduser().resolve()
    if any(output.is_relative_to(source) or source.is_relative_to(output) for source in sources):
        raise ValueError('输出目录不能与源码或 Git 仓库目录重叠。')
    if any(path.is_relative_to(output) for path in protected):
        raise ValueError('输出目录不能包含原始案例或测试文件。')
    ancestor = output
    while not ancestor.exists():
        if ancestor.is_symlink():
            raise ValueError('输出路径包含无效符号链接。')
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise ValueError('输出位置的最近现有目录不可写或不是目录。')
    return '输出位置未与输入重叠，最近现有父目录具有写入权限；未创建目录。'


def _git_tree(root, sha, target=None):
    from .git_source import _git
    from .runner import EXCLUDED, MAX_BYTES, MAX_ENTRIES
    count, total, found = 0, 0, False
    for record in _git(root, 'ls-tree', '-r', '-l', '-z', '--full-tree', sha).split(b'\0'):
        if not record:
            continue
        metadata, raw_name = record.split(b'\t', 1)
        mode, kind, oid, raw_size = metadata.split()
        name = raw_name.decode('utf-8')
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in ('.', '..') for part in path.parts):
            raise ValueError('Git 包含不支持的文件路径。')
        if any(part in EXCLUDED or part.startswith('.env') for part in path.parts):
            continue
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('Git 源码包含符号链接、子模块或其他非普通文件。')
        count += 1
        total += int(raw_size)
        if count > MAX_ENTRIES or total > MAX_BYTES:
            raise ValueError('Git 源码超过 5000 个文件或 100 MiB 的限制。')
        if name == target:
            found = True
    if target is not None and not found:
        raise ValueError('修复后版本不存在选中的普通 Python 测试文件。')


def check_case(path, output_root=None, *, timeout=15, python_executable=None,
               previous_environment=None, control=None):
    """Return structured checks, converting user input errors into attention.

    Cancellation intentionally propagates. Runtime readiness here means supported
    Python/pytest metadata; the execution path still validates the real runtime.
    """
    checks = []
    result = {'status': 'attention', 'checks': checks, 'runtime': None,
              'environment': None, 'environment_comparison': None,
              'tests_executed': False, 'notice': NOTICE}

    def checkpoint():
        if control is not None:
            control.check()

    def perform(identifier, operation, message, suggestion):
        checkpoint()
        try:
            value = operation()
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, SyntaxError, RuntimeError) as exc:
            checks.append({'id': identifier, 'status': 'error', 'message': str(exc)[:2000], 'suggestion': suggestion})
            return None
        checks.append({'id': identifier, 'status': 'ok', 'message': message, 'suggestion': ''})
        checkpoint()
        return value

    checkpoint()
    def valid_timeout():
        if type(timeout) not in (int, float) or not .1 <= timeout <= 300 or not math.isfinite(timeout):
            raise ValueError('timeout 必须是 0.1–300 秒之间的有限数字。')
    perform('timeout', valid_timeout, '超时时间有效。', '请选择 0.1–300 秒之间的有限数字。')

    def read_config():
        config = Path(path).expanduser()
        if config.is_symlink():
            raise ValueError('不自动运行符号链接指向的案例文件。')
        config = config.resolve()
        if config.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('案例配置过大。')
        data = json.loads(config.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('案例必须是 JSON 对象。')
        if data.get('source') not in (None, 'snapshot', 'git'):
            raise ValueError('不支持的案例 source 类型。')
        return config, data
    loaded = perform('case', read_config, '案例 JSON 可读取。', '检查案例文件是否为有效的本地案例 JSON。')
    sources, protected, pending_tests, data = [], [], [], {}
    if loaded is not None:
        config, data = loaded
        protected.append(config)
        if data.get('source') != 'git':
            case = perform('case_contract', lambda: load_case(config), '案例路径和预期故障声明有效。', '修正案例路径、类型和 expected_failure 字段。')
            if case is not None:
                from .runner import source_entries
                from .pytest_profile import read_pytest_profile
                from .runtime_environment import resolve_variables
                if case.test_target:
                    perform('runtime_variables', lambda: resolve_variables(read_pytest_profile(case.after), getattr(control, 'runtime_values', None)),
                            '自定义运行变量声明有效，必要本机值已提供；未运行项目代码。', '检查运行变量声明，并提供缺失的本机值。')
                sources = [case.before, case.after]
                protected.append(case.test)
                for label, source in (('before', case.before), ('after', case.after)):
                    perform('source_' + label, lambda source=source: source_entries(source),
                            '源码清单符合本地文件限制。', '移除源码中的符号链接或特殊文件，并检查大小限制。')
                pending_tests.append(case.test)
        else:
            def git_contract():
                if any(not isinstance(data.get(k), str) or not data[k] for k in ('repository', 'before_ref', 'after_ref')):
                    raise ValueError('Git 案例缺少有效的仓库或版本字段。')
                if not isinstance(data.get('title'), str) or not data['title'].strip():
                    raise ValueError('title 必须是非空字符串。')
                expected = data.get('expected_failure')
                if (not isinstance(expected, dict) or not isinstance(expected.get('type'), str)
                        or not re.fullmatch(r'[A-Za-z_]\w*', expected['type'])
                        or not isinstance(expected.get('message_contains'), str) or not expected['message_contains'].strip()):
                    raise ValueError('expected_failure 必须包含有效异常类名和非空消息。')
                test, target = data.get('test'), data.get('test_target')
                if bool(test) == bool(target) or any(value is not None and (not isinstance(value, str) or not value) for value in (test, target)):
                    raise ValueError('Git 案例必须选择独立 test 或项目 test_target 其中一种。')
                validate_draft_review(data.get('draft_review', {}))
                return parse_test_target(target) if target else ''
            target = perform('case_contract', git_contract, 'Git 案例和预期故障声明有效。', '检查 Git 版本、测试目标和 expected_failure 字段。')
            if target is not None:
                from .recipes import repository_root
                from .git_source import resolve_commit
                root = perform('repository', lambda: repository_root(config.parent / data['repository']), '本地 Git 仓库可读取。', '选择有效的本地 Git 仓库。')
                if root is not None:
                    sources = [root]
                    for label in ('before', 'after'):
                        def revision(label=label):
                            sha = resolve_commit(root, data[label + '_ref'])
                            _git_tree(root, sha, target if label == 'after' and target else None)
                            return sha
                        perform('git_' + label, revision, 'Git 提交及普通文件清单可读取，未导出或切换版本。', '检查版本是否存在，目标是否为普通文件，以及源码文件限制。')
                if not target:
                    test_path = config.parent / data['test']
                    pending_tests.append(test_path)

    # An explicit choice always overrides the saved environment, including its base.
    def selected_executable():
        selected = python_executable if python_executable is not None else data.get('python')
        if selected is None:
            return None
        if not isinstance(selected, (str, os.PathLike)) or not str(selected):
            raise ValueError('python 必须是解释器路径。')
        candidate = Path(selected).expanduser()
        base = Path.cwd() if python_executable is not None or loaded is None else loaded[0].parent
        return os.path.abspath(candidate if candidate.is_absolute() else base / candidate)
    selected = perform('python_path', selected_executable, '已选择可信本地 Python 路径。', '选择已存在的可信 Python 解释器。')
    # Do not silently substitute the own environment for an invalid selection.
    path_invalid = any(item['id'] == 'python_path' and item['status'] == 'error' for item in checks)
    checkpoint()
    environment = snapshot_environment('' if path_invalid else selected)
    checkpoint()
    result['environment'] = environment
    comparison = compare_environments(previous_environment, environment)
    result['environment_comparison'] = comparison
    if environment['status'] == 'recorded':
        checks.append({'id': 'environment', 'status': 'ok', 'message': environment['notice'], 'suggestion': ''})
        def runtime_metadata():
            version = tuple(int(part) for part in environment['python_version'].split('.')[:2])
            pytest_version = environment['distributions'].get('pytest')
            if pytest_version is None:
                raise ValueError('所选 Python 环境缺少 pytest 发行包元数据；请在该环境安装 pytest>=8,<10。')
            if version < (3, 11) or not 8 <= int(pytest_version.split('.')[0]) < 10:
                raise ValueError('需要 Python >=3.11 和 pytest >=8,<10。')
            return {'executable': environment['executable'], 'python_version': environment['python_version'], 'pytest_version': pytest_version}
        result['runtime'] = perform('runtime', runtime_metadata, 'Python/pytest 版本元数据符合要求；未执行 pytest 导入或项目依赖检查。', '请选择带有 Python >=3.11、pytest >=8,<10 的可信环境。')
    else:
        checks.append({'id': 'environment', 'status': 'error', 'message': environment['notice'], 'suggestion': '检查所选解释器是否存在、可执行且可读取发行包元数据。'})
    syntax_executable = environment['executable'] if environment['status'] == 'recorded' else None
    for test_path in pending_tests:
        message = ('测试文件在所选 Python 中静态语法可读取；尚未动态收集。' if syntax_executable
                   else '测试路径可读取；环境不可用，尚未进行语法检查。')
        checked_test = perform('test', lambda test_path=test_path: _test_syntax(test_path, syntax_executable),
                               message, '检查测试路径、普通文件类型及所选 Python 支持的语法。')
        if checked_test is not None:
            protected.append(checked_test)
    if output_root is not None and loaded is not None:
        perform('output', lambda: _output_check(output_root, sources, protected),
                '输出位置与输入不重叠，最近现有目录可写；未创建目录。', '选择源码、案例和测试文件之外的可写报告目录。')
    if comparison['status'] == 'changed':
        checks.append({'id': 'environment_comparison', 'status': 'warning', 'message': comparison['notice'], 'suggestion': '确认这是本次希望使用的环境；变化本身不阻止重跑。'})
    checkpoint()
    result['status'] = 'attention' if any(item['status'] == 'error' for item in checks) else 'ready'
    return result
