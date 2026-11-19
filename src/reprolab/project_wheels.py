"""Verify real wheel installations against one pinned, trusted downstream pytest project."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time

from .git_source import export_revision, resolve_commit
from .persistence import save_json
from .process_tree import run_logged_command
from .project_selections import validate_selection
from .recipes import repository_root
from .runner import source_fingerprint, source_mode_fingerprint
from .runtime import child_environment, choose_runtime
from .wheel_results import (
    EXIT_CODES, MAX_RECORD_BYTES, PHASE_FILE, PHASE_PROBE, compare_wheel_results, junit_document, junit_totals,
)

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
""" + PHASE_PROBE + """
raise SystemExit(pytest.main(sys.argv[4:], plugins=[ReproLabPhases()]))
"""


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


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
                phases = artifact_root / PHASE_FILE
                row = {'install_status': install_status, 'install_exit_code': code, 'tests': None}
                if install_status == 'completed' and code == 0:
                    command = [runtime['executable'], '-I', '-c', _PROBE, str(site), module, str(phases),
                               '-q', '--junitxml', str(xml), *targets]
                    with test_log.open('wb') as stream:
                        status, result = run_logged_command(command, checkout, child_environment(), stream,
                                                            time.monotonic() + timeout, control)
                    row.update(status=status, exit_code=result, phase_record_expected=True,
                               tests=junit_totals(junit_document(xml)) if status == 'completed' else None)
                row['evidence_sha256'] = {file.name: _sha(file) for file in (install_log, test_log, xml, phases) if file.is_file()}
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
    return {**record, 'directory': str(output)}, EXIT_CODES[verdict]


def _read_checked(directory, control):
    root = Path(directory).expanduser().resolve()
    path = root / _RECORD
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError('请选择完整的 wheel 下游比较目录。')
    with path.open('rb') as stream:
        payload = stream.read(MAX_RECORD_BYTES + 1)
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError('wheel 下游证据记录超过大小上限。')
    record = json.loads(payload)
    if record.get('kind') != _KIND or record.get('schema_version') != 1:
        raise ValueError('wheel 下游证据格式无效。')
    snapshot = root / 'downstream'
    if snapshot.is_symlink() or record['downstream'] != {'sha256': source_fingerprint(snapshot)[1],
                                                         'mode_sha256': source_mode_fingerprint(snapshot)}:
        raise ValueError('下游固定源码指纹不一致。')
    documents, phases = {}, {}
    for side in ('before', 'after'):
        control.check()
        folder = root / side
        item = record['wheel_artifacts'][side]
        file = root / item['file']
        if (folder.is_symlink() or file.is_symlink() or file.resolve().parent != folder
                or file.suffix != '.whl' or _sha(file) != item['sha256']):
            raise ValueError('wheel 文件指纹不符。')
        run = record['runs'][side]
        hashes = run['evidence_sha256']
        if ('install.log' not in hashes or 'status' in run and 'pytest.log' not in hashes
                or any((folder / name).exists() and name not in hashes for name in ('pytest.xml', PHASE_FILE))):
            raise ValueError('安装或测试记录缺少证据指纹。')
        documents[side] = None
        phases[side] = b'' if run.get('phase_record_expected') is True else None
        for name, digest in hashes.items():
            path = folder / name
            if name not in ('install.log', 'pytest.log', 'pytest.xml', PHASE_FILE) or path.is_symlink():
                raise ValueError('保存的安装或测试记录路径不符。')
            if name in ('pytest.xml', PHASE_FILE):
                with path.open('rb') as stream:
                    data = stream.read(MAX_RECORD_BYTES + 1)
                if len(data) > MAX_RECORD_BYTES:
                    if _sha(path) != digest:
                        raise ValueError('保存的测试记录指纹不符。')
                    if name == PHASE_FILE:
                        phases[side] = b''
                elif hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('保存的测试记录指纹不符。')
                elif name == 'pytest.xml':
                    documents[side] = junit_document(path, payload=data)
                else:
                    phases[side] = data
            elif _sha(path) != digest:
                raise ValueError('保存的安装或测试日志指纹不符。')
        if run.get('status') == 'completed' and junit_totals(documents[side]) != run['tests']:
            raise ValueError('JUnit 事实与保存的结果不一致。')
    if _classification(record['runs']) != record['status']:
        raise ValueError('下游判定与安装和测试记录不一致。')
    return {**record, 'integrity': 'consistent'}, snapshot, documents, phases


def check_wheels(directory, *, control):
    return _read_checked(directory, control)[0]


def review_wheels(directory, *, policy, required=(), include_ids=False, control):
    record, snapshot, documents, phases = _read_checked(directory, control)
    comparison = compare_wheel_results(record, snapshot, documents, phases, policy=policy, required=required,
                                       include_ids=include_ids)
    if policy == 'legacy':
        status = record['status']
        comparison['gate'] = {'policy': policy, 'status': status, 'exit_code': EXIT_CODES[status]}
    # The default legacy output is unchanged. Detailed output deliberately
    # excludes locations, raw logs, XML metadata and the raw selector list.
    runs = {}
    for side in ('before', 'after'):
        run = record['runs'][side]
        row = {}
        for key in ('install_status', 'status'):
            if key in run:
                row[key] = run[key] if run[key] in ('completed', 'timed_out') else 'unknown'
        for key in ('install_exit_code', 'exit_code'):
            if key in run:
                row[key] = run[key] if type(run[key]) is int else None
        row['tests'] = junit_totals(documents[side]) if run.get('status') == 'completed' else None
        runs[side] = row
    return {**{key: record[key] for key in ('kind', 'schema_version', 'status', 'integrity')},
            'runs': runs, 'comparison': comparison}
