"""Inspect pytest entry-point metadata without importing third-party plugins."""
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .runtime import child_environment

_LIMIT = 512
_MAX_OUTPUT = 1024 * 1024
_PROBE = '''import importlib.metadata,json
rows=[]
for ep in importlib.metadata.entry_points(group="pytest11"):
    if len(rows)>=512: raise ValueError("too many pytest plugins")
    dist=ep.dist
    rows.append(dict(name=ep.name,module=ep.module,distribution=dist.metadata.get("Name", ""),version=dist.version))
print(json.dumps(rows))
'''


def _entry_points(executable):
    selected = os.path.abspath(executable or sys.executable)
    if selected == os.path.abspath(sys.executable):
        result = []
        for ep in importlib.metadata.entry_points(group='pytest11'):
            if len(result) >= _LIMIT:
                raise ValueError('pytest 插件数量超过限制。')
            result.append({'name': ep.name, 'module': ep.module,
                           'distribution': ep.dist.metadata.get('Name', ''), 'version': ep.dist.version})
        return result
    with tempfile.TemporaryFile() as output:
        process = subprocess.run([selected, '-I', '-X', 'utf8', '-c', _PROBE], stdout=output, stderr=subprocess.DEVNULL,
                                 env=child_environment(), cwd=tempfile.gettempdir(), timeout=10)
        if process.returncode or output.tell() > _MAX_OUTPUT:
            raise ValueError('无法读取所选 Python 的插件元数据。')
        output.seek(0)
        return json.loads(output.read(_MAX_OUTPUT))


def inspect_pytest_plugins(source, executable, declared=None):
    """Metadata presence is not an import or compatibility guarantee."""
    available, status = [], 'recorded'
    try:
        raw = _entry_points(executable)
        if not isinstance(raw, list) or len(raw) > _LIMIT:
            raise ValueError('插件元数据格式无效。')
        for row in raw:
            if (not isinstance(row, dict) or any(not isinstance(row.get(key), str)
                    or not row[key] or len(row[key]) > 256
                    or any(ord(c) < 32 for c in row[key]) for key in ('name', 'module', 'distribution', 'version'))
                    or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', row['name'])
                    or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', row['module'])):
                raise ValueError('插件元数据格式无效。')
            available.append({key: row[key] for key in ('name', 'module', 'distribution', 'version')})
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        available, status = [], 'unavailable'
    root = Path(source).resolve()
    checks = []
    for name in declared or []:
        matches = [entry for entry in available if name in (entry['name'], entry['module'])]
        module = name.replace('.', '/')
        local = any(path.is_file() and path.resolve().is_relative_to(root) for base in (root, root / 'src')
                    for path in (base / (module + '.py'), base / module / '__init__.py'))
        state = 'ambiguous' if len(matches) > 1 else 'installed' if matches else 'local' if local else 'unknown'
        checks.append({'name': name, 'status': state,
                       'distribution': matches[0]['distribution'] if len(matches) == 1 else None,
                       'version': matches[0]['version'] if len(matches) == 1 else None})
    return {'status': status, 'available': sorted(available, key=lambda row: (row['name'], row['distribution'])),
            'declared': checks,
            'notice': '只读取 pytest 插件入口元数据，不加载插件；已安装或存在本地文件仍须实际运行确认。'
                      if status == 'recorded' else '插件安装信息暂不可用，已声明配置仍保留；请在所选环境中核对。'}
