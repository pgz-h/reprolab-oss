"""Explain execution overrides by reading bounded configuration data only."""
import configparser
from pathlib import Path
import tomllib

from .pytest_profile import MAX_CONFIG_BYTES, read_pytest_profile
from .runtime import BASE_ENVIRONMENT_KEYS
from .runtime_environment import variable_record, describe_variables


def inspect_execution_settings(project):
    root = Path(project)
    declarations, issues = [], []
    for name in ('pytest.ini', '.pytest.ini', 'pytest.toml', '.pytest.toml', 'pyproject.toml', 'tox.ini', 'setup.cfg'):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError('不是受支持的普通配置文件')
            raw = path.read_bytes()
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError('配置文件超过大小限制')
            text = raw.decode('utf-8-sig')
            values = []
            if name.endswith('.toml'):
                data = tomllib.loads(text)
                if name == 'pyproject.toml':
                    pytest = data.get('tool', {}).get('pytest', {})
                    values = [('tool.pytest.ini_options', pytest.get('ini_options', {}).get('addopts')),
                              ('tool.pytest', pytest.get('addopts'))]
                else:
                    values = [('pytest', data.get('pytest', {}).get('addopts'))]
            else:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(text)
                section = 'tool:pytest' if name == 'setup.cfg' else 'pytest'
                values = [(section, parser.get(section, 'addopts', fallback=None))]
            for section, value in values:
                if value is None or value == '' or value == []:
                    continue
                if not (isinstance(value, str) or isinstance(value, list) and all(isinstance(item, str) for item in value)):
                    raise ValueError('附加参数格式无法识别')
                shown = ' '.join(value) if isinstance(value, list) else value
                declarations.append({'file': name, 'section': section, 'value': shown[:4000], 'truncated': len(shown) > 4000})
        except (OSError, ValueError, AttributeError, TypeError, configparser.Error):
            issues.append(name + ' 无法静态读取，请结合实际测试收集结果核对。')
    try:
        profile = read_pytest_profile(root)
    except (OSError, ValueError):
        profile = None
        issues.append('ReproLab 插件配置无法读取，请先检查 pyproject.toml。')
    return {'schema_version': 1, 'pytest_profile': profile, 'plugin_autoload': False,
            'project_addopts_applied': False, 'detected_addopts': declarations,
            'allowed_environment_names': list(BASE_ENVIRONMENT_KEYS), 'runtime_environment': variable_record(profile), 'issues': issues,
            'notice': '这里只列出检测到的声明，不推断多个配置文件的优先级，不加载项目或插件。'}


def describe_execution_settings(settings):
    profile = settings.get('pytest_profile')
    plugins = '、'.join((profile or {}).get('plugins') or []) or '未额外启用'
    mode = (profile or {}).get('import_mode') or 'pytest 默认'
    lines = ['本次实际运行方式', '',
             '插件：' + plugins + '。环境中的其他插件不会自动加载。',
             '导入方式：' + mode + '。两版共用候选快照中的这份插件及导入配置。',
             '项目配置与 fixture：各版本分别保留；两版使用同一份所选测试文件。', '',
             '项目默认附加参数（addopts）会清空；实际范围由所选测试清单决定。']
    declarations = settings.get('detected_addopts') or []
    lines.extend(('检测到但不会自动执行的声明：',) if declarations else ('没有检测到非空的附加参数声明。',))
    for row in declarations:
        lines.append(row['file'] + ' [' + row['section'] + ']：' + row['value'] + (' …（显示截断）' if row['truncated'] else ''))
    lines.extend(['', '环境变量：仅允许继承 PATH、临时目录、语言等基础运行变量。',
                  '未声明的自定义变量不会自动带入；普通值来自快照，本机值只读取明确声明的名称。',
                  describe_variables(settings.get('runtime_environment')),
                  '运行变量页可预览并保存声明；PYTHONPATH、PYTEST_* 等控制变量不可覆盖。',
                  '如果终端可运行但这里受阻，请核对插件、导入方式、附加参数及项目需要的变量。', '',
                  '普通子 Python 进程继承本版快照的 src／根目录；PATH 优先使用所选 Python 的命令目录。',
                  '显式 -I／-E、重建环境或自行改写导入路径的子进程不在此保证范围；需要 Python 子进程的项目请选独立 Python。',
                  '允许继承的变量名称：' + '、'.join(settings.get('allowed_environment_names') or []),
                  *settings.get('issues', []), settings.get('notice', '')])
    if profile is None:
        lines.insert(2, '插件配置尚未读到有效结果；下方默认描述不代表项目可运行。')
    return '\n'.join(lines)
