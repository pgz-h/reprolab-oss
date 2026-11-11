"""Record a bounded distribution inventory from an explicitly trusted Python.

Reading Name/Version metadata does not import the distributions, resolve project
requirements, lock dependencies, or certify that an environment is safe.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

from .runtime import child_environment

NOTICE = '仅记录可信 Python 的发行包名称和版本；不是依赖锁、导入检查或安全认证。'
SCOPE = 'python-distribution-metadata'
MAX_OUTPUT = 4 * 1024 * 1024

# Keep this standalone: the selected environment need not contain ReproLab.
_PROBE = '''import importlib.metadata,json,platform,re,sys
pairs=[]
for index,dist in enumerate(importlib.metadata.distributions()):
    if index >= 10000: raise ValueError('distribution inventory exceeds limit')
    name=dist.metadata.get('Name')
    version=dist.metadata.get('Version')
    if not isinstance(name,str) or not isinstance(version,str) or not name or not version: continue
    if len(name)>256 or len(version)>128 or any(ord(c)<32 for c in name+version): raise ValueError('invalid distribution metadata')
    pairs.append((re.sub(r'[-_.]+','-',name).lower(),version))
print(json.dumps({'python_version':sys.version.split()[0], 'platform':platform.system(), 'machine':platform.machine(), 'distributions':dict(sorted(pairs))},sort_keys=True))
'''


def _local_metadata():
    # Execute the same stdlib-only inventory logic in the own (including frozen)
    # interpreter without trying to use Python flags on an app dispatcher.
    pairs = []
    for index, dist in enumerate(importlib.metadata.distributions()):
        if index >= 10000:
            raise ValueError('distribution inventory exceeds limit')
        name, version = dist.metadata.get('Name'), dist.metadata.get('Version')
        if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
            continue
        if len(name) > 256 or len(version) > 128 or any(ord(c) < 32 for c in name + version):
            raise ValueError('invalid distribution metadata')
        pairs.append((re.sub(r'[-_.]+', '-', name).lower(), version))
    return {'python_version': sys.version.split()[0], 'platform': platform.system(),
            'machine': platform.machine(), 'distributions': dict(sorted(pairs))}


def snapshot_environment(executable=None):
    """Return metadata, or an explicit unavailable record; never resolve symlinks."""
    result = {'status': 'unavailable', 'executable': None, 'python_version': None,
              'platform': None, 'machine': None, 'distributions': {}, 'fingerprint': None,
              'scope': SCOPE, 'notice': NOTICE}
    try:
        selected = sys.executable if executable is None else executable
        if not isinstance(selected, (str, os.PathLike)) or not str(selected):
            raise ValueError('Python 解释器路径无效。')
        selected = os.path.abspath(Path(selected).expanduser())
        result['executable'] = selected
        if not Path(selected).is_file() or not os.access(selected, os.X_OK):
            raise ValueError('Python 解释器不存在或不可执行。')
        if selected == os.path.abspath(sys.executable):
            info = _local_metadata()
        else:
            # File-backed output bounds parent memory even if interpreter startup
            # emits unexpected output. Only our small metadata JSON is retained.
            with tempfile.TemporaryFile() as output:
                completed = subprocess.run([selected, '-I', '-X', 'utf8', '-c', _PROBE], stdout=output,
                                           stderr=subprocess.DEVNULL, timeout=10,
                                           env=child_environment(), cwd=tempfile.gettempdir())
                if completed.returncode or output.tell() > MAX_OUTPUT:
                    raise ValueError('元数据探测失败或超过大小限制。')
                output.seek(0)
                info = json.loads(output.read(MAX_OUTPUT).decode('utf-8'))
        if (not isinstance(info, dict) or any(not isinstance(info.get(k), str)
                for k in ('python_version', 'platform', 'machine'))
                or not isinstance(info.get('distributions'), dict)
                or len(info['distributions']) > 10000
                or any(not isinstance(k, str) or not isinstance(v, str)
                       or len(k) > 256 or len(v) > 128 for k, v in info['distributions'].items())):
            raise ValueError('无法识别 Python 环境元数据。')
        payload = {key: info[key] for key in ('python_version', 'platform', 'machine', 'distributions')}
        payload['executable'] = selected
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        result.update(payload, status='recorded', fingerprint=hashlib.sha256(encoded).hexdigest())
    except Exception:
        # Do not echo interpreter output, metadata URLs, or startup diagnostics.
        result['notice'] = '环境版本记录不可用；不能确认与历史环境一致。' + NOTICE
    return result


def _recorded(value):
    return (isinstance(value, dict) and value.get('status') == 'recorded'
            and all(isinstance(value.get(key), str) and value[key]
                    for key in ('executable', 'python_version', 'platform', 'fingerprint'))
            and isinstance(value.get('machine'), str)
            and isinstance(value.get('distributions'), dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in value['distributions'].items()))


def compare_environments(previous, current):
    """Compare available inventories; legacy reports are explicitly unknown."""
    result = {'status': 'unknown', 'notice': '缺少完整的历史或当前环境记录，无法比较。',
              'added': [], 'removed': [], 'changed': [], 'python_changed': False,
              'path_changed': False, 'platform_changed': False}
    if not _recorded(previous) or not _recorded(current):
        return result
    old, new = previous['distributions'], current['distributions']
    result['added'] = [{'name': name, 'version': new[name]} for name in sorted(new.keys() - old.keys())]
    result['removed'] = [{'name': name, 'version': old[name]} for name in sorted(old.keys() - new.keys())]
    result['changed'] = [{'name': name, 'before': old[name], 'after': new[name]}
                         for name in sorted(old.keys() & new.keys()) if old[name] != new[name]]
    result['python_changed'] = previous['python_version'] != current['python_version']
    result['path_changed'] = previous['executable'] != current['executable']
    result['platform_changed'] = any(previous[key] != current[key] for key in ('platform', 'machine'))
    result['python'] = {'before': previous['python_version'], 'after': current['python_version']}
    result['path'] = {'before': previous['executable'], 'after': current['executable']}
    different = any(result[key] for key in ('added', 'removed', 'changed', 'python_changed', 'path_changed', 'platform_changed'))
    result.update(status='changed' if different else 'same',
                  notice='环境版本或路径已变化；请结合本次测试结果判断。' if different else '所记录的 Python、路径与发行包版本一致。')
    return result


def read_report_environment(path):
    """Read only a bounded environment field; missing legacy records are unknown."""
    if path is None:
        return None
    try:
        path = Path(path)
        if path.is_dir():
            path = path / "report.json"
        if path.stat().st_size > MAX_OUTPUT:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("environment") if isinstance(data, dict) else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
