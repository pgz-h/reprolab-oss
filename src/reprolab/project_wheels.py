"""Verify real wheel installations against one pinned, trusted downstream pytest project."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET

from .git_source import export_revision, resolve_commit
from .persistence import save_json
from .process_tree import run_logged_command
from .project_selections import validate_selection
from .recipes import repository_root
from .runner import source_fingerprint, source_mode_fingerprint
from .runtime import child_environment, choose_runtime

_KIND = 'reprolab_downstream_wheels'
_RECORD = 'wheel-comparison.json'
_PROBE = """
import importlib, pathlib, sys
site = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(site))
module = importlib.import_module(sys.argv[2])
locations = getattr(module, '__path__', None) or [getattr(module, '__file__', None)]
if not all(item and pathlib.Path(item).resolve().is_relative_to(site) for item in locations):
    raise SystemExit(4)
import pytest
raise SystemExit(pytest.main(sys.argv[3:]))
"""


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _junit(path):
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        return None
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    return {'tests': len(cases), 'passed': sum(not any(field.tag in ('failure', 'error', 'skipped') for field in case)
                                              for case in cases),
            **{name: sum(any(field.tag == tag for field in case) for case in cases)
               for name, tag in (('failed', 'failure'), ('errors', 'error'), ('skipped', 'skipped'))}}


def _classification(runs):
    def outcome(run):
        facts = run.get('tests')
        if run.get('install_status') != 'completed' or run.get('install_exit_code') != 0 or not facts or not facts['tests']:
            return 'blocked'
        if run.get('exit_code') == 0 and facts['passed'] == facts['tests']:
            return 'passed'
        if run.get('exit_code') == 1 and (facts['failed'] or facts['errors']):
            return 'failed'
        return 'blocked'
    before, after = (outcome(runs[side]) for side in ('before', 'after'))
    return 'passed' if before == after == 'passed' else 'failed' if before == 'passed' and after == 'failed' else 'blocked'


def run_wheels(downstream, ref, before, after, output, *, module, selected, python_executable, timeout, confirmed, control):
    targets = validate_selection(selected)
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*', module):
        raise ValueError('请选择将从 wheel 加载的正常 Python 模块名。')
    root = repository_root(downstream)
    revision = resolve_commit(root, ref)
    paths = {side: Path(path).expanduser().resolve() for side, path in (('before', before), ('after', after))}
    if any(not path.is_file() or path.suffix != '.whl' or path.stat().st_size > 250 * 1024 * 1024
           for path in paths.values()):
        raise ValueError('请选择两份不超过 250 MiB 的实际 wheel。')
    if not confirmed:
        return {'status': 'ready', 'downstream_commit': revision, 'selected_tests': targets}, 0
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(root):
        raise ValueError('请选择下游仓库以外的新证据目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime = choose_runtime(python_executable)
    with tempfile.TemporaryDirectory(prefix='.wheel-downstream-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'matrix'
        stage.mkdir()
        snapshot = stage / 'downstream'
        export_revision(root, revision, snapshot)
        fingerprint = {'sha256': source_fingerprint(snapshot)[1], 'mode_sha256': source_mode_fingerprint(snapshot)}
        artifacts, runs = {}, {}
        for side, wheel in paths.items():
            artifact_root = stage / side
            artifact_root.mkdir()
            copy = artifact_root / wheel.name
            shutil.copyfile(wheel, copy)
            digest = _sha(copy)
            if digest != _sha(wheel):
                raise ValueError('读取期间 wheel 内容发生变化。')
            artifacts[side] = {'file': copy.relative_to(stage).as_posix(), 'sha256': digest}
            with tempfile.TemporaryDirectory(prefix='downstream-install-') as working:
                work = Path(working)
                site = work / 'site'
                checkout = work / 'downstream'
                shutil.copytree(snapshot, checkout)
                install = [runtime['executable'], '-I', '-m', 'pip', 'install', '--no-index', '--no-deps',
                           '--disable-pip-version-check', '--target', str(site), str(copy)]
                install_log, test_log, xml = (artifact_root / name for name in ('install.log', 'pytest.log', 'pytest.xml'))
                with install_log.open('wb') as stream:
                    install_status, code = run_logged_command(install, checkout, child_environment(), stream,
                                                              time.monotonic() + timeout, control)
                row = {'install_status': install_status, 'install_exit_code': code, 'tests': None}
                if install_status == 'completed' and code == 0:
                    command = [runtime['executable'], '-I', '-c', _PROBE, str(site), module,
                               '-q', '--junitxml', str(xml), *targets]
                    with test_log.open('wb') as stream:
                        status, result = run_logged_command(command, checkout, child_environment(), stream,
                                                            time.monotonic() + timeout, control)
                    row.update(status=status, exit_code=result, tests=_junit(xml) if status == 'completed' else None)
                row['evidence_sha256'] = {file.name: _sha(file) for file in (install_log, test_log, xml) if file.is_file()}
                runs[side] = row
        verdict = _classification(runs)
        record = {'kind': _KIND, 'schema_version': 1, 'status': verdict, 'wheel_artifacts': artifacts,
                  'downstream_commit': revision, 'downstream': fingerprint, 'module': module,
                  'selected_tests': targets, 'runs': runs}
        save_json(stage / _RECORD, record)
        control.commit()
        if output.exists() or output.is_symlink():
            raise ValueError('证据目标已存在。')
        stage.rename(output)
    return {**record, 'directory': str(output)}, {'passed': 0, 'failed': 1, 'blocked': 2}[verdict]


def check_wheels(directory, *, control):
    root = Path(directory).expanduser().resolve()
    path = root / _RECORD
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('请选择完整的 wheel 下游比较目录。')
    record = json.loads(path.read_bytes())
    if record.get('kind') != _KIND or record.get('schema_version') != 1:
        raise ValueError('wheel 下游证据格式无效。')
    if record['downstream'] != {'sha256': source_fingerprint(root / 'downstream')[1],
                                'mode_sha256': source_mode_fingerprint(root / 'downstream')}:
        raise ValueError('下游固定源码指纹不一致。')
    for side in ('before', 'after'):
        control.check()
        item = record['wheel_artifacts'][side]
        file = (root / item['file']).resolve()
        if file.parent != root / side or _sha(file) != item['sha256']:
            raise ValueError('wheel 文件指纹不符。')
        for name, digest in record['runs'][side]['evidence_sha256'].items():
            path = root / side / name
            if name not in ('install.log', 'pytest.log', 'pytest.xml') or path.is_symlink() or _sha(path) != digest:
                raise ValueError('保存的安装或测试日志指纹不符。')
        if record['runs'][side].get('status') == 'completed' and _junit(root / side / 'pytest.xml') != record['runs'][side]['tests']:
            raise ValueError('JUnit 事实与保存的结果不一致。')
    if _classification(record['runs']) != record['status']:
        raise ValueError('下游判定与安装和测试记录不一致。')
    return {**record, 'integrity': 'consistent'}
