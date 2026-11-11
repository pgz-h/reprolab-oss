"""Explicit pytest compatibility settings, read from frozen project inputs."""
from pathlib import Path
import json
import re
import tomllib

from .runtime_environment import validate_variables

MAX_CONFIG_BYTES = 256 * 1024


def read_pytest_profile(source):
    """Read data only. Plugins are imported later, by the selected pytest worker."""
    profile = {'plugins': [], 'import_mode': None, 'source': None}
    path = Path(source) / 'pyproject.toml'
    if not path.exists() and not path.is_symlink():
        return profile
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError('pytest 运行配置须来自不超过 256 KiB 的普通 pyproject.toml 文件。')
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8-sig'))
        table = data.get('tool', {}).get('reprolab', {}).get('pytest')
    except (ValueError, UnicodeError, AttributeError) as exc:
        raise ValueError('无法读取 pyproject.toml 的 pytest 运行配置，请检查 TOML 格式。') from exc
    if table is None:
        return profile
    return {**_validate_profile(table), 'source': 'pyproject.toml [tool.reprolab.pytest]'}


def _validate_profile(table):
    if not isinstance(table, dict) or set(table) - {'plugins', 'import_mode', 'environment', 'pass_env'}:
        raise ValueError('[tool.reprolab.pytest] 仅支持 plugins、import_mode、environment 和 pass_env。')
    plugins = table.get('plugins', [])
    if (not isinstance(plugins, list) or len(plugins) > 32
            or any(not isinstance(name, str) or len(name) > 200
                   or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', name) for name in plugins)):
        raise ValueError('plugins 须为最多 32 个 pytest 插件模块名或入口名，不能填写命令或路径。')
    mode = table.get('import_mode')
    if mode is not None and mode not in ('prepend', 'append', 'importlib'):
        raise ValueError('import_mode 须为 prepend、append 或 importlib。')
    values, names = validate_variables(table.get('environment'), table.get('pass_env'))
    return {'plugins': list(dict.fromkeys(plugins)), 'import_mode': mode,
            **({'environment': values} if values else {}), **({'pass_env': names} if names else {})}


def render_pytest_profile(plugins, import_mode=None, *, environment=None, pass_env=None):
    profile = _validate_profile({'plugins': plugins, 'import_mode': import_mode,
                                 'environment': environment or {}, 'pass_env': pass_env or []})
    lines = ['[tool.reprolab.pytest]', 'plugins = ' + json.dumps(profile['plugins'], ensure_ascii=False)]
    if profile['import_mode']:
        lines.append('import_mode = ' + json.dumps(profile['import_mode']))
    if profile.get('pass_env'):
        lines.append('pass_env = ' + json.dumps(profile['pass_env']))
    if profile.get('environment'):
        lines.extend(['', '[tool.reprolab.pytest.environment]'])
        lines.extend(name + ' = ' + json.dumps(value, ensure_ascii=False) for name, value in profile['environment'].items())
    return '\n'.join(lines) + '\n'


def profile_args(profile):
    args = [value for name in profile['plugins'] for value in ('-p', name)]
    if profile['import_mode']:
        args.append('--import-mode=' + profile['import_mode'])
    return args


def describe_pytest_profile(profile):
    if profile is None:
        return '没有记录 pytest 运行配置；请结合当次日志核对插件和导入方式。'
    plugins = '、'.join(profile.get('plugins') or []) or '未额外启用'
    mode = profile.get('import_mode') or 'pytest 默认'
    return f'测试插件：{plugins}；导入方式：{mode}。两版使用同一配置，项目默认附加参数不自动执行。'
