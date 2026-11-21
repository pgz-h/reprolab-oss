"""Verify real wheel installations against one pinned, trusted downstream pytest project."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from packaging.utils import parse_wheel_filename

from .git_source import export_revision, resolve_commit
from .persistence import save_json
from .process_tree import run_logged_command
from .project_selections import validate_selection
from .recipes import repository_root
from .runner import source_fingerprint, source_mode_fingerprint
from .runtime import PYTHON_ENVIRONMENT_FIELDS, PYTHON_ENVIRONMENT_PROBE, child_environment, choose_runtime
from .wheel_install import (
    TEST_PYTEST_REQUIREMENT, VENV_PYTHON, _MAX_WHEEL, _capture_wheel, _check_metadata, _install, _wheel_size, _wheelhouse_paths,
    _wheelhouse_records, local_wheel_environment, wheelhouse_manifest_sha256,
)
from .wheel_results import (
    EXIT_CODES, MAX_RECORD_BYTES, PHASE_FILE, PHASE_PROBE, compare_wheel_results, junit_document, junit_totals,
)

_KIND = 'reprolab_downstream_wheels'
_RECORD = 'wheel-comparison.json'
MODULE_NAME = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*')
_PROBE = PYTHON_ENVIRONMENT_PROBE + """
import importlib, json, pathlib, sys, sysconfig
from importlib.metadata import distribution, distributions

environment = reprolab_python_environment()
setup = json.loads(sys.argv[1])
test_installation = {'mode': 'existing_environment'}
if 'target' in setup:
    if sys.prefix == sys.base_prefix or pathlib.Path(sys.prefix).resolve() != pathlib.Path(setup['target']).resolve():
        raise SystemExit('Expected the configured isolated virtual environment.')
    sites = {pathlib.Path(sysconfig.get_path(key)).resolve() for key in ('purelib', 'platlib')}
    test_installation = {'mode': 'isolated_venv', 'wheelhouse_sha256': setup['wheelhouse_sha256']}
else:
    site = pathlib.Path(setup['site']).resolve()
    sites = {site}
    sys.path.insert(0, str(site))

module = importlib.import_module(sys.argv[2])
locations = getattr(module, '__path__', None) or [getattr(module, '__file__', None)]
isolated = test_installation['mode'] == 'isolated_venv'
if not all(item and any(pathlib.Path(item).resolve().is_relative_to(site) for site in sites) for item in locations):
    print('The requested module was not loaded from the wheel installation.', file=sys.stderr)
    raise SystemExit(74 if isolated else 4)
if isolated:
    def owners_of(file):
        return [dist.metadata['Name'] for dist in distributions(path=[str(site) for site in sites])
                if any(pathlib.Path(dist.locate_file(entry)).resolve() == file for entry in dist.files or ())]

    origin = getattr(module, '__file__', None)
    expected = distribution(setup['distribution']).metadata['Name']
    if not origin or owners_of(pathlib.Path(origin).resolve()) != [expected]:
        print('The requested module cannot be attributed uniquely to the selected wheel. '
              'For namespace packages, select a concrete submodule.', file=sys.stderr)
        raise SystemExit(74)
import pytest
""" + PHASE_PROBE + """
raise SystemExit(pytest.main(sys.argv[4:], plugins=[ReproLabPhases(environment, pytest.__version__, test_installation)]))
"""


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _snapshot_record(snapshot):
    entries, digest = source_fingerprint(snapshot)
    return {'sha256': digest, 'mode_sha256': source_mode_fingerprint(snapshot, entries)}


class WheelEvidenceMismatch(ValueError):
    def __init__(self, message, fields):
        super().__init__(message)
        self.fields = fields


def wheel_artifact_mismatches(artifacts, expected):
    fields = []
    for side, item in artifacts.items():
        for field, value in (('filename', Path(item['file']).name), ('sha256', item['sha256'])):
            if value != expected[side][field]:
                fields.append(f'wheel_artifacts.{side}.{field}')
    return fields


def _require_planned_artifacts(artifacts, plan):
    fields = wheel_artifact_mismatches(artifacts, plan['wheel_artifacts'])
    if fields:
        raise WheelEvidenceMismatch('wheel 制品与功能计划不匹配：' + ', '.join(fields), fields)


def _require_planned_environment(executable, expected):
    if expected is None:
        return
    command = PYTHON_ENVIRONMENT_PROBE + '\nimport json\nprint(json.dumps(reprolab_python_environment()))'
    try:
        probe = subprocess.run([executable, '-I', '-c', command], env=child_environment(),
                               capture_output=True, check=True, timeout=10)
        observed = json.loads(probe.stdout)
        fields = [f'test_environment.{key}' for key in PYTHON_ENVIRONMENT_FIELDS
                  if observed.get(key) != expected[key]]
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as exc:
        raise ValueError('无法从所选解释器核对 required_test_environment。') from exc
    if fields:
        raise WheelEvidenceMismatch('所选 Python 与功能计划不匹配：' + ', '.join(fields), fields)


def _require_planned_wheelhouse(observed, expected):
    if expected is not None and observed != expected:
        raise WheelEvidenceMismatch('功能 wheelhouse 与计划不匹配：required_test_wheelhouse_sha256。',
                                    ['required_test_wheelhouse_sha256'])


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


def _require_pip(executable):
    try:
        probe = subprocess.run([executable, '-I', '-m', 'pip', '--version'],
                               env=child_environment(), capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'无法检查所选 Python 解释器的 pip：{executable}。{exc}') from exc
    if probe.returncode:
        if 'No module named pip' in probe.stderr:
            raise ValueError(f'所选 Python 解释器缺少 pip：{executable}。请在该环境安装 pip 后重试。')
        raise ValueError(f'所选 Python 解释器的 pip 无法运行：{executable}。{probe.stderr.strip()[-1000:]}')


def _setup_wheel_test(executable, wheel, work, checkout, install_log, wheelhouse, house_sha256, timeout, control):
    if wheelhouse is None:
        site = work / 'site'
        env = child_environment()
        install = [executable, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps',
                   '--disable-pip-version-check', '--target', str(site), str(wheel)]
        with install_log.open('wb') as stream:
            status, code = run_logged_command(install, checkout, env, stream, time.monotonic() + timeout, control)
        return {'install_status': status, 'install_exit_code': code}, executable, env, {'site': str(site)}

    target = work / 'target'
    _, steps, _, code, reason = _install(
        target, wheel, wheelhouse, executable, work, work, time.monotonic() + timeout, control,
        requirements=(TEST_PYTEST_REQUIREMENT,),
    )
    with install_log.open('wb') as stream:
        for name, step in steps.items():
            stream.write(f'[{name}] {step["status"]} ({step["exit_code"]})\n'.encode())
            with (work / f'{name}.log').open('rb') as source:
                shutil.copyfileobj(source, stream)
            stream.write(b'\n')
    interpreter = str(target / VENV_PYTHON)
    env = local_wheel_environment(work)
    env['PATH'] = str(Path(interpreter).parent) + os.pathsep + env.get('PATH', os.defpath)
    env['VIRTUAL_ENV'] = str(target)
    setup = {'target': str(target), 'distribution': parse_wheel_filename(wheel.name)[0],
             'wheelhouse_sha256': house_sha256}
    return ({'install_status': 'completed' if code in (0, 1) else 'unavailable',
             'install_exit_code': code, 'install_reason': reason}, interpreter, env, setup)


def run_wheels(downstream, ref, before, after, output, *, module, selected, python_executable, timeout, confirmed,
               control, test_wheelhouse=None, review_plan=None):
    targets = validate_selection(selected)
    if not MODULE_NAME.fullmatch(module):
        raise ValueError('请选择将从 wheel 加载的正常 Python 模块名。')
    root = repository_root(downstream)
    planned = review_plan is not None
    revision = resolve_commit(root, ref, local_only=planned)
    if planned and revision != ref:
        raise ValueError('下游 Git 提交与功能计划的 downstream_commit 不匹配。')
    paths = {side: Path(path).expanduser().resolve() for side, path in (('before', before), ('after', after))}
    if any(not path.is_file() or path.suffix != '.whl' or path.stat().st_size > _MAX_WHEEL
           for path in paths.values()):
        raise ValueError('请选择两份不超过 250 MiB 的实际 wheel。')
    expected_house = review_plan.get('required_test_wheelhouse_sha256') if planned else None
    expected_environment = review_plan.get('required_test_environment') if planned else None
    if expected_house is not None and test_wheelhouse is None:
        raise ValueError('功能计划的 required_test_wheelhouse_sha256 需要 --test-wheelhouse。')
    executable = os.path.abspath(os.path.expanduser(python_executable or sys.executable))
    if test_wheelhouse is not None:
        inputs = _wheelhouse_paths(test_wheelhouse.expanduser().resolve())
        for path in paths.values():
            _wheel_size(path)
    if not confirmed:
        preview = {'status': 'ready', 'downstream_commit': revision, 'selected_tests': targets}
        if planned:
            artifacts = {side: {'file': path.name, 'sha256': _capture_wheel(path, _MAX_WHEEL, control)[0]['sha256']}
                         for side, path in paths.items()}
            _require_planned_artifacts(artifacts, review_plan)
            if expected_house is not None:
                _require_planned_wheelhouse(wheelhouse_manifest_sha256(list(_wheelhouse_records(inputs, control))),
                                            expected_house)
            _require_planned_environment(executable, expected_environment)
            preview['preflight_only'] = True
        if test_wheelhouse is not None:
            preview['test_installation'] = {'mode': 'isolated_venv', 'wheelhouse_count': len(inputs),
                                            'pytest_requirement': TEST_PYTEST_REQUIREMENT}
        return preview, 0
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(root):
        raise ValueError('请选择下游仓库以外的新证据目录。')
    if test_wheelhouse is None and not planned:
        executable = choose_runtime(python_executable)['executable']
        _require_pip(executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wheel-downstream-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'matrix'
        stage.mkdir()
        local_wheels, house_sha256 = None, None
        if test_wheelhouse is not None:
            local_wheels = Path(temporary) / 'wheelhouse'
            local_wheels.mkdir()
            wheelhouse_records = list(_wheelhouse_records(inputs, control, destination=local_wheels))
            house_sha256 = wheelhouse_manifest_sha256(wheelhouse_records)
            _require_planned_wheelhouse(house_sha256, expected_house)
        snapshot = stage / 'downstream'
        export_revision(root, revision, snapshot, local_only=planned)
        fingerprint = _snapshot_record(snapshot)
        artifacts, runs = {}, {}
        for side, wheel in paths.items():
            artifact_root = stage / side
            artifact_root.mkdir()
            copy = artifact_root / wheel.name
            if planned:
                digest = _capture_wheel(wheel, _MAX_WHEEL, control, target=copy)[0]['sha256']
            else:
                shutil.copyfile(wheel, copy)
                digest = _sha(copy)
                if digest != _sha(wheel):
                    raise ValueError('读取期间 wheel 内容发生变化。')
            artifacts[side] = {'file': copy.relative_to(stage).as_posix(), 'sha256': digest}
        if planned:
            _require_planned_artifacts(artifacts, review_plan)
            _require_planned_environment(executable, expected_environment)
            if test_wheelhouse is None:
                executable = choose_runtime(executable)['executable']
                _require_pip(executable)
        if local_wheels is not None:
            for artifact in artifacts.values():
                _check_metadata(stage / artifact['file'])
        for side, artifact in artifacts.items():
            copy = stage / artifact['file']
            artifact_root = copy.parent
            with tempfile.TemporaryDirectory(prefix='downstream-install-') as working:
                work = Path(working)
                checkout = work / 'downstream'
                shutil.copytree(snapshot, checkout)
                install_log, test_log, xml = (artifact_root / name for name in ('install.log', 'pytest.log', 'pytest.xml'))
                row, interpreter, env, setup = _setup_wheel_test(
                    executable, copy, work, checkout, install_log, local_wheels, house_sha256, timeout, control,
                )
                phases = artifact_root / PHASE_FILE
                row['tests'] = None
                if row['install_status'] == 'completed' and row['install_exit_code'] == 0:
                    command = [interpreter, '-I', '-c', _PROBE, json.dumps(setup), module, str(phases),
                               '-q', '--junitxml', str(xml), *targets]
                    with test_log.open('wb') as stream:
                        status, result = run_logged_command(command, checkout, env, stream,
                                                            time.monotonic() + timeout, control)
                    row.update(status=status, exit_code=result, phase_record_expected=True,
                               tests=junit_totals(junit_document(xml)) if status == 'completed' else None)
                row['evidence_sha256'] = {file.name: _sha(file) for file in (install_log, test_log, xml, phases) if file.is_file()}
                runs[side] = row
        verdict = _classification(runs)
        record = {'kind': _KIND, 'schema_version': 1, 'status': verdict, 'wheel_artifacts': artifacts,
                  'downstream_commit': revision, 'downstream': fingerprint, 'module': module,
                  'selected_tests': targets, 'runs': runs}
        if local_wheels is not None:
            record['test_wheelhouse'] = wheelhouse_records
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
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise WheelEvidenceMismatch('下游固定源码快照缺失或路径无效。', ['downstream'])
    fingerprint = _snapshot_record(snapshot)
    if record['downstream'] != fingerprint:
        fields = [f'downstream.{key}' for key, value in fingerprint.items()
                  if record['downstream'].get(key) != value]
        raise WheelEvidenceMismatch('下游固定源码指纹不一致。', fields or ['downstream'])
    documents, phases = {}, {}
    for side in ('before', 'after'):
        control.check()
        folder = root / side
        item = record['wheel_artifacts'][side]
        file = root / item['file']
        if (folder.is_symlink() or file.is_symlink() or file.resolve().parent != folder
                or file.suffix != '.whl' or not file.is_file()):
            raise WheelEvidenceMismatch('wheel 文件缺失或路径无效。', [f'wheel_artifacts.{side}.filename'])
        if _sha(file) != item['sha256']:
            raise WheelEvidenceMismatch('wheel 文件指纹不符。', [f'wheel_artifacts.{side}.sha256'])
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
    return _review_checked(*_read_checked(directory, control), policy=policy, required=required, include_ids=include_ids)


def _review_checked(record, snapshot, documents, phases, *, policy, required, include_ids):
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
        if run.get('install_reason') in ('tool-venv_unavailable', 'target-venv_unavailable',
                                          'target_environment_not_empty_or_unavailable', 'pip_unavailable',
                                          'install_failed', 'install_unavailable', 'pip_check_failed', 'pip_check_unavailable'):
            row['install_reason'] = run['install_reason']
        row['tests'] = junit_totals(documents[side]) if run.get('status') == 'completed' else None
        runs[side] = row
    return {**{key: record[key] for key in ('kind', 'schema_version', 'status', 'integrity')},
            'runs': runs, 'comparison': comparison}
