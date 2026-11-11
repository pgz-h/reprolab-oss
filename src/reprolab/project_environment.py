"""Static project requirements and explicitly previewed external environments.

No project imports, setup.py execution, credential configuration, or guessed
import-to-package mapping. Only named wheel distributions from public PyPI can
be installed into a newly created directory outside the inspected project.
"""
from .process_tree import spawn, terminate_tree
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .environment import snapshot_environment
from .runner import RunCancelled, RunControl
from .runtime import child_environment
from .pytest_profile import read_pytest_profile
from .execution_settings import inspect_execution_settings
from .dependency_groups import resolve_dependency_groups
from .plugin_inventory import inspect_pytest_plugins

INDEX_URL = 'https://pypi.org/simple'
NOTICE = ('静态读取项目基础依赖、所选测试／开发依赖组与 requirements.txt；'
          '核对发行包版本，不导入项目，不证明测试或全部传递依赖可用。')
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_FILES = 32
MAX_REQUIREMENTS = 2000
_MARKER_KEYS = ('implementation_name', 'implementation_version', 'os_name',
                'platform_machine', 'platform_release', 'platform_system',
                'platform_version', 'python_full_version',
                'platform_python_implementation', 'python_version', 'sys_platform')
_MARKER_PROBE = '''import json,os,platform,sys
v=sys.implementation.version
iv='.'.join(str(x) for x in v[:3])
if v.releaselevel!='final': iv+=v.releaselevel[0]+str(v.serial)
print(json.dumps(dict(implementation_name=sys.implementation.name,
implementation_version=iv,os_name=os.name,platform_machine=platform.machine(),
platform_release=platform.release(),platform_system=platform.system(),
platform_version=platform.version(),python_full_version=platform.python_version(),
platform_python_implementation=platform.python_implementation(),
python_version='.'.join(str(x) for x in sys.version_info[:2]),sys_platform=sys.platform)))
'''


def _local_marker_environment():
    version = sys.implementation.version
    implementation_version = '.'.join(str(x) for x in version[:3])
    if version.releaselevel != 'final':
        implementation_version += version.releaselevel[0] + str(version.serial)
    return dict(implementation_name=sys.implementation.name,
                implementation_version=implementation_version, os_name=os.name,
                platform_machine=platform.machine(), platform_release=platform.release(),
                platform_system=platform.system(), platform_version=platform.version(),
                python_full_version=platform.python_version(),
                platform_python_implementation=platform.python_implementation(),
                python_version='.'.join(str(x) for x in sys.version_info[:2]),
                sys_platform=sys.platform)


def _marker_environment(executable):
    """Use the selected runtime's complete marker context, never host defaults."""
    try:
        if executable == os.path.abspath(sys.executable):
            return _local_marker_environment()
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([executable, '-I', '-X', 'utf8', '-c', _MARKER_PROBE],
                                    stdout=output, stderr=subprocess.DEVNULL,
                                    cwd=tempfile.gettempdir(), env=child_environment(), timeout=10)
            if result.returncode or output.tell() > 32768:
                return None
            output.seek(0)
            info = json.loads(output.read(32768))
        if (not isinstance(info, dict) or any(not isinstance(info.get(k), str)
                or len(info[k]) > 1024 for k in _MARKER_KEYS)):
            return None
        return {k: info[k] for k in _MARKER_KEYS}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _project_root(project):
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('项目目录不存在。')
    return root


def _declarations(root, selected_groups=None):
    """Read only bounded static manifests; diagnostics never include raw lines."""
    requirements, issues, files, visited = [], [], {}, set()
    total = 0
    python_specifier = '>=3.11'
    groups = resolve_dependency_groups({}, selected_groups)

    def review(reason='包含不支持的依赖声明，需人工检查。'):
        if reason not in issues:
            issues.append(reason)

    def read(path):
        nonlocal total
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or path.is_symlink():
                review('依赖文件必须是项目内的普通文件，需人工检查。')
                return None
            relative = path.relative_to(root).as_posix()
            if relative in visited:
                review('依赖包含循环或重复引用，需人工检查。')
                return None
            if len(visited) >= MAX_FILES:
                review('依赖文件数量超过限制，需人工检查。')
                return None
            visited.add(relative)
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                review('依赖文件缺失或超过大小限制，需人工检查。')
                return None
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                review('依赖文件超过大小限制，需人工检查。')
                return None
            files[relative] = hashlib.sha256(raw).hexdigest()
            return raw.decode('utf-8-sig')
        except (OSError, ValueError, UnicodeError):
            review('依赖文件无法读取，需人工检查。')
            return None

    def add(line):
        if len(requirements) >= MAX_REQUIREMENTS:
            review('依赖条目超过限制，需人工检查。')
            return
        # Reject before parsing: exceptions or package URLs must not reach UI/logs.
        if (not isinstance(line, str) or len(line) > 2048 or any(ord(c) < 32 for c in line)
                or line.startswith(('-', '.', '/')) or '@' in line or '://' in line):
            review()
            return
        try:
            item = Requirement(line)
            if item.url:
                review()
                return
            requirements.append(item)
        except InvalidRequirement:
            review()

    def requirements_file(path):
        content = read(path)
        if content is None:
            return
        for raw in content.splitlines():
            line = re.split(r'\s+#', raw, maxsplit=1)[0].strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'(?:-r\s*|--requirement(?:\s+|=))(.+)', line)
            if match:
                include = match.group(1).strip()
                # No quoting, options, URLs, variables, or absolute includes.
                if (not include or any(c in include for c in ':@\\\"\'#$')
                        or any(c.isspace() for c in include) or Path(include).is_absolute()):
                    review()
                    continue
                target = Path(os.path.abspath(path.parent / include))
                if not target.is_relative_to(root):
                    review('依赖引用超出项目目录，需人工检查。')
                    continue
                requirements_file(target)
            else:
                add(line)

    # Missing top-level files participate in the fingerprint, so adding one
    # invalidates a preview as surely as editing an already-present file.
    for name in ('pyproject.toml', 'requirements.txt'):
        files[name] = None
    pyproject = root / 'pyproject.toml'
    if pyproject.exists() or pyproject.is_symlink():
        content = read(pyproject)
        if content is not None:
            try:
                parsed = tomllib.loads(content)
                groups = resolve_dependency_groups(parsed, selected_groups)
                for value in groups['requirements']:
                    add(value)
                project = parsed.get('project', {})
                if not isinstance(project, dict):
                    raise ValueError()
                values = project.get('dependencies', [])
                if not isinstance(values, list):
                    raise ValueError()
                for value in values:
                    add(value)
                dynamic = project.get('dynamic', [])
                if not isinstance(dynamic, list) or any(x in dynamic for x in ('dependencies', 'requires-python')):
                    review('项目依赖由动态配置提供，需人工检查。')
                if 'requires-python' in project:
                    python_specifier = str(SpecifierSet(python_specifier + ',' + str(SpecifierSet(project['requires-python']))))
                if not project and not groups['available'] and not (root / 'requirements.txt').is_file():
                    review('项目没有受支持的静态依赖声明，需人工检查。')
            except (ValueError, TypeError, InvalidSpecifier):
                review('pyproject.toml 的依赖声明无效，需人工检查。')
    requirements_path = root / 'requirements.txt'
    if requirements_path.exists() or requirements_path.is_symlink():
        requirements_file(requirements_path)
    if not any(value is not None for value in files.values()):
        if any((root / name).exists() for name in ('setup.py', 'setup.cfg')):
            review('项目只提供未解析的构建依赖声明，需人工检查。')
    for issue in groups['issues']:
        review(issue)
    canonical = sorted({str(item): item for item in requirements}.values(), key=str)
    fingerprint = hashlib.sha256(json.dumps({'files': files, 'python': python_specifier,
        'requirements': [str(item) for item in canonical], 'groups': groups['selected'], 'issues': issues},
        sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return {'requirements': canonical, 'issues': issues, 'files_sha256': files,
            'python_specifier': python_specifier, 'manifest_fingerprint': fingerprint,
            'available_groups': groups['available'], 'selected_groups': groups['selected']}


def _applies(requirement, markers):
    if requirement.marker is None:
        return True
    if markers is None or re.search(r'\b(extra|extras|dependency_groups)\b', str(requirement.marker)):
        return None
    try:
        return requirement.marker.evaluate(environment=markers)
    except (ValueError, KeyError):
        return None


def _version_status(installed, specifier):
    if installed is None:
        return 'missing'
    try:
        return 'satisfied' if Version(installed) in SpecifierSet(specifier) else 'incompatible'
    except (InvalidVersion, InvalidSpecifier, TypeError):
        return 'unknown'


def candidate_pythons(project):
    """List conventional environment paths without starting any interpreter."""
    root = _project_root(project)
    return [str(root / directory / suffix)
            for directory in ('.venv', 'venv', 'env')
            for suffix in ('bin/python', 'Scripts/python.exe')
            if (root / directory / suffix).is_file()]


def inspect_project(project, python_executable=None, *, selected_groups=None):
    """Read declarations and explicitly selected runtime metadata only."""
    root = _project_root(project)
    declarations = _declarations(root, selected_groups)
    environment = snapshot_environment(python_executable or None)
    executable = environment.get('executable')
    markers = _marker_environment(executable) if environment['status'] == 'recorded' else None
    installed = {canonicalize_name(k): v for k, v in environment.get('distributions', {}).items()}
    requirements = [Requirement('pytest>=8,<10'), *declarations['requirements']]
    dependencies = []
    for requirement in sorted({str(item): item for item in requirements}.values(), key=str):
        name = canonicalize_name(requirement.name)
        applies = _applies(requirement, markers)
        status = ('unknown' if applies is None or environment['status'] != 'recorded'
                  else 'not-applicable' if not applies
                  else _version_status(installed.get(name), str(requirement.specifier)))
        dependencies.append({'name': name, 'specifier': str(requirement.specifier),
                             'installed': installed.get(name), 'status': status,
                             'requirement': str(requirement)})
    issues = list(declarations['issues'])
    try:
        pytest_profile = read_pytest_profile(root)
    except ValueError as exc:
        pytest_profile = None
        issues.append(str(exc))
    for _ in declarations['issues']:
        dependencies.append({'name': '未支持的依赖声明', 'specifier': '',
                             'installed': None, 'status': 'needs-review',
                             'requirement': None})
    python_status = _version_status(environment.get('python_version'), declarations['python_specifier'])
    if python_status != 'satisfied':
        issues.append('需要满足项目版本声明的 Python >=3.11。')
    if environment['status'] != 'recorded':
        issues.append('所选 Python 的环境信息不可用。')
    if any(item['status'] == 'unknown' for item in dependencies):
        issues.append('部分依赖标记或版本无法确认，需人工检查。')
    if any(item['status'] in ('missing', 'incompatible') for item in dependencies):
        issues.append('所选环境有缺少或版本不兼容的依赖。')
    status = ('review' if declarations['issues'] or any(item['status'] == 'unknown' for item in dependencies)
              else 'attention' if issues else 'ready')
    reuse_blocked = (environment['status'] != 'recorded' or python_status != 'satisfied'
                     or pytest_profile is None
                     or any(row['status'] in ('missing', 'incompatible')
                            or (row['name'] == 'pytest' and row['status'] != 'satisfied')
                            for row in dependencies))
    reuse = {'status': 'blocked' if reuse_blocked else 'review' if status != 'ready' else 'ready',
             'reasons': list(issues)}
    return {'status': status, 'reuse': reuse, 'project': str(root), 'project_path': str(root),
            'runtime': {'executable': executable, 'python_version': environment.get('python_version'),
                        'specifier': declarations['python_specifier'], 'status': python_status},
            'environment': environment, 'candidate_pythons': candidate_pythons(root),
            'dependencies': dependencies, 'issues': issues, 'pytest_profile': pytest_profile,
            'execution_settings': inspect_execution_settings(root),
            'available_groups': declarations['available_groups'], 'selected_groups': declarations['selected_groups'],
            'pytest_plugins': inspect_pytest_plugins(root, executable, (pytest_profile or {}).get('plugins', [])),
            'files_sha256': declarations['files_sha256'],
            'manifest_fingerprint': declarations['manifest_fingerprint'], 'notice': NOTICE}


def _destination(project, destination):
    if not str(destination).strip():
        raise ValueError('请选择新环境目录。')
    original = Path(destination).expanduser()
    if original.exists() or original.is_symlink():
        raise ValueError('新环境目录已存在，请选择一个全新目录。')
    path = original.resolve()
    if path.is_relative_to(project):
        raise ValueError('新环境必须保存在项目目录之外。')
    if path.exists() or path.is_symlink():
        raise ValueError('新环境目录已存在，请选择一个全新目录。')
    if not path.parent.is_dir():
        raise ValueError('新环境的父目录不存在，请先选择已有父目录。')
    return path


def _base_python(base_python):
    if not isinstance(base_python, (str, os.PathLike)) or not str(base_python):
        raise ValueError('请选择独立 Python 解释器的完整路径。')
    candidate = Path(base_python).expanduser()
    if not candidate.is_absolute():
        raise ValueError('请选择独立 Python 解释器的完整路径。')
    candidate = Path(os.path.abspath(candidate))
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError('所选独立 Python 解释器不可执行。')
    if getattr(sys, 'frozen', False) and os.path.samefile(candidate, sys.executable):
        raise ValueError('应用内置运行器不能创建环境，请选择独立 Python >=3.11。')
    return str(candidate)


def plan_environment(project, destination, base_python, *, selected_groups=None):
    """Create a concrete read-only preview; this does not create any files."""
    root = _project_root(project)
    target = _destination(root, destination)
    executable = _base_python(base_python)
    declarations = _declarations(root, selected_groups)
    if declarations['issues']:
        raise ValueError('依赖声明需要人工检查，无法自动准备环境。')
    snapshot = snapshot_environment(executable)
    if snapshot['status'] != 'recorded' or _version_status(snapshot.get('python_version'), declarations['python_specifier']) != 'satisfied':
        raise ValueError('所选独立 Python 必须满足项目声明以及 Python >=3.11。')
    markers = _marker_environment(executable)
    requirements = [Requirement('pytest>=8,<10'), *declarations['requirements']]
    selected = []
    for requirement in requirements:
        applies = _applies(requirement, markers)
        if applies is None:
            raise ValueError('依赖环境标记无法确认，需要人工检查。')
        if applies:
            selected.append(str(requirement))
    return {'schema': 1, 'project': str(root), 'destination': str(target),
            'interpreter': executable, 'python_version': snapshot['python_version'],
            'requirements': sorted(set(selected)), 'files_sha256': declarations['files_sha256'],
            'selected_groups': declarations['selected_groups'],
            'manifest_fingerprint': declarations['manifest_fingerprint'],
            'index_url': INDEX_URL, 'only_binary': True,
            'notice': '创建全新外部 venv；仅从公开 PyPI 安装具名 wheel 依赖，不安装项目或运行其构建后端。'}


def _preparation_environment(destination):
    env = child_environment()
    isolated_home = str(destination / '.reprolab-home')
    env.update(HOME=isolated_home, USERPROFILE=isolated_home, XDG_CONFIG_HOME=isolated_home,
               PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
               PIP_KEYRING_PROVIDER='disabled')
    return env


def _run_step(command, *, cwd, control, phase):
    """Discard untrusted child output; retain only bounded, safe status summaries."""
    control.notify(phase)
    process = spawn(command, cwd=cwd, env=_preparation_environment(cwd),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=(os.name == 'posix'))
    try:
        deadline = time.monotonic() + 300
        while process.poll() is None:
            control.check()
            if time.monotonic() >= deadline:
                raise ValueError('环境准备步骤超时，请检查网络或所选 Python。')
            try:
                process.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                pass
        control.check()
        if process.returncode:
            if phase == 'installing':
                raise ValueError('依赖安装失败：请检查公开 PyPI 网络、依赖版本冲突或 wheel 是否支持当前 Python。')
            raise ValueError('创建环境失败：所选 Python 可能不提供 venv 或 ensurepip。')
    finally:
        terminate_tree(process)


def prepare_environment(plan, *, control=None):
    """Execute the exact reviewed plan after re-reading all declarations."""
    if not isinstance(plan, dict) or plan.get('schema') != 1:
        raise ValueError('环境计划无效，请重新预览。')
    control = control or RunControl()
    control.check()
    try:
        fresh = plan_environment(plan['project'], plan['destination'], plan['interpreter'],
                                 selected_groups=plan.get('selected_groups'))
    except KeyError:
        raise ValueError('环境计划无效，请重新预览。') from None
    except ValueError:
        raise ValueError('依赖声明或环境准备条件已变化，请重新预览。') from None
    if plan != fresh:
        raise ValueError('依赖声明或环境计划已改变，请重新预览。')
    control.check()
    destination = Path(fresh['destination'])
    destination.mkdir(mode=0o700)
    log = destination / 'preparation.log'
    record_path = destination / 'reprolab-environment.json'
    record = {'status': 'preparing', 'destination': str(destination), 'plan': fresh,
              'python': None, 'log_path': str(log), 'notice': '仅保存步骤状态，子进程原始输出已脱敏丢弃。'}
    lines = []

    def save(message):
        lines.append(message)
        log.write_text('\n'.join(lines)[-65536:] + '\n', encoding='utf-8')
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')

    try:
        (destination / '.reprolab-home').mkdir(mode=0o700)
        save('开始：已重新核对依赖声明。')
        _run_step([fresh['interpreter'], '-I', '-X', 'utf8', '-m', 'venv', str(destination)],
                  cwd=destination, control=control, phase='creating')
        executable = destination / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        requirements = destination / 'reprolab-requirements.txt'
        requirements.write_text('\n'.join(fresh['requirements']) + '\n', encoding='utf-8')
        save('虚拟环境已创建；开始从公开 PyPI 安装预览中的 wheel 依赖。')
        _run_step([str(executable), '-I', '-X', 'utf8', '-m', 'pip', '--isolated', '--disable-pip-version-check',
                   '--no-input', '--no-cache-dir', 'install', '--keyring-provider', 'disabled',
                   '--index-url', INDEX_URL, '--only-binary=:all:', '--no-compile',
                   '-r', str(requirements)], cwd=destination, control=control, phase='installing')
        control.notify('checking')
        inspection = inspect_project(fresh['project'], executable, selected_groups=fresh['selected_groups'])
        if inspection['manifest_fingerprint'] != fresh['manifest_fingerprint']:
            raise ValueError('准备期间依赖声明已变化，请重新检查项目环境。')
        if inspection['status'] != 'ready':
            raise ValueError('依赖安装后仍有未满足的声明，请检查环境状态。')
        control.commit()
        record.update(status='ready', python=str(executable), python_executable=str(executable),
                      inspection=inspection)
        save('完成：声明依赖版本检查通过。')
        return record
    except Exception as exc:
        record.update(status='cancelled' if isinstance(exc, RunCancelled) else 'failed',
                      error=('已取消环境准备；保留未完成目录，请选择新目录重试。' if isinstance(exc, RunCancelled)
                             else str(exc) if isinstance(exc, ValueError) else '环境准备失败，请检查目录权限或独立 Python。'))
        save(record['error'])
        if isinstance(exc, RunCancelled):
            raise
        raise ValueError(record['error']) from None
