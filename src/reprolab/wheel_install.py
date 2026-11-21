"""Install one trusted wheel and its local dependencies into an empty venv."""
from contextlib import nullcontext
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from zipfile import BadZipFile, ZipFile

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from .persistence import save_json
from .process_tree import run_logged_command
from .runtime import PYTHON_ENVIRONMENT_PROBE, child_environment

INSTALL_CHECK_KIND = 'reprolab_wheel_install_check'
INSTALL_CHECK_RECORD = 'wheel-install-check.json'
TEST_PYTEST_REQUIREMENT = 'pytest>=8,<10'
_MAX_WHEEL = 250 * 1024 * 1024
_MAX_WHEELHOUSE = 1024 * 1024 * 1024
_MAX_WHEELS = 128
_MAX_METADATA = 1024 * 1024
VENV_PYTHON = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
_ENVIRONMENT = PYTHON_ENVIRONMENT_PROBE + """
import importlib.metadata, json
print(json.dumps({
    **reprolab_python_environment(),
    'isolated_venv': sys.prefix != sys.base_prefix,
    'initial_distributions': sorted(d.metadata['Name'] for d in importlib.metadata.distributions()),
}))
"""


def _validate_wheel_filename(filename):
    if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.+!-]*\.whl', filename):
        raise ValueError('wheel 文件名格式无效。')
    try:
        parse_wheel_filename(filename)
    except InvalidWheelFilename:
        raise ValueError('wheel 文件名格式无效。') from None


def _wheel_size(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('本地安装检查只接受普通 wheel 文件。')
    _validate_wheel_filename(path.name)
    size = path.stat().st_size
    if size > _MAX_WHEEL:
        raise ValueError('单个 wheel 的大小上限为 250 MiB。')
    return size


def _wheelhouse_paths(path):
    if not path.is_dir():
        raise ValueError('请提供本地 wheelhouse 目录。')
    wheels = []
    names = set()
    total = 0
    for index, item in enumerate(path.iterdir(), start=1):
        if index > 512:
            raise ValueError('wheelhouse 目录条目上限为 512。')
        if item.suffix.lower() == '.whl':
            if item.name.casefold() in names:
                raise ValueError('wheelhouse 含仅大小写不同的重复文件名。')
            names.add(item.name.casefold())
            total += _wheel_size(item)
            wheels.append(item)
            if len(wheels) > _MAX_WHEELS or total > _MAX_WHEELHOUSE:
                raise ValueError('wheelhouse 上限为 128 个 wheel、合计 1 GiB。')
    return sorted(wheels, key=lambda item: item.name)


def _check_metadata(path):
    try:
        with ZipFile(path) as archive:
            members = [item for item in archive.infolist()
                       if re.fullmatch(r'[^/]+\.dist-info/METADATA', item.filename)]
            if len(members) != 1 or members[0].file_size > _MAX_METADATA:
                raise ValueError('wheel 必须有一份不超过 1 MiB 的 METADATA。')
            metadata = BytesParser().parsebytes(archive.read(members[0]), headersonly=True)
        if metadata.defects or not metadata['Name'] or not metadata['Version']:
            raise ValueError('wheel 的 METADATA 无效。')
        for value in metadata.get_all('Requires-Dist', ()):
            if Requirement(' '.join(str(value).splitlines())).url is not None:
                raise ValueError('本地安装检查拒绝 Requires-Dist 中的直接 URL 或路径引用。')
    except (BadZipFile, InvalidRequirement, RuntimeError, NotImplementedError):
        raise ValueError('无法核验 wheel 的 METADATA 或依赖声明。') from None


def _capture_wheel(source, limit, control, *, target=None):
    digest = hashlib.sha256()
    size = 0
    with source.open('rb') as reader, (target.open('xb') if target else nullcontext()) as writer:
        while chunk := reader.read(1024 * 1024):
            control.check()
            size += len(chunk)
            if size > limit:
                raise ValueError('读取期间 wheel 超出大小限制。')
            if writer is not None:
                writer.write(chunk)
            digest.update(chunk)
    return {'filename': source.name, 'sha256': digest.hexdigest()}, size


def _wheelhouse_records(inputs, control, *, destination=None):
    total = 0
    for source in inputs:
        control.check()
        copied = destination / source.name if destination is not None else None
        item, size = _capture_wheel(source, min(_MAX_WHEEL, _MAX_WHEELHOUSE - total), control, target=copied)
        total += size
        _check_metadata(copied or source)
        yield item


def wheelhouse_manifest_sha256(records):
    """Fingerprint the wheelhouse input list, independent of presentation order."""
    if not isinstance(records, list) or len(records) > _MAX_WHEELS:
        raise ValueError('wheelhouse 清单格式或数量无效。')
    names = set()
    for record in records:
        if (not isinstance(record, dict) or record.keys() != {'filename', 'sha256'}
                or not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
            raise ValueError('wheelhouse 清单项须包含 wheel 文件名和小写 SHA-256。')
        filename = record['filename']
        _validate_wheel_filename(filename)
        if filename.casefold() in names:
            raise ValueError('wheelhouse 清单含重复 wheel 文件名。')
        names.add(filename.casefold())
    payload = json.dumps(sorted(records, key=lambda item: item['filename']), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('ascii')).hexdigest()


def fingerprint_wheelhouse(wheelhouse, *, control):
    inputs = _wheelhouse_paths(wheelhouse.expanduser().resolve())
    records = list(_wheelhouse_records(inputs, control))
    return {'kind': 'reprolab_wheelhouse_fingerprint', 'schema_version': 1,
            'wheelhouse': records, 'wheelhouse_sha256': wheelhouse_manifest_sha256(records)}


def _run(command, phase, *, cwd, logs, env, deadline, control):
    path = logs / f'{phase}.log'
    with path.open('wb') as stream:
        try:
            status, code = run_logged_command(command, cwd, env, stream, deadline, control)
        except OSError:
            status, code = 'unavailable', None
    return {'status': status, 'exit_code': code}


def _pip_result(step, phase):
    if step == {'status': 'completed', 'exit_code': 0}:
        return 'passed', 0, None
    if step == {'status': 'completed', 'exit_code': 1}:
        return 'failed', 1, f'{phase}_failed'
    return 'blocked', 2, f'{phase}_unavailable'


def local_wheel_environment(work):
    env = child_environment()
    home = work / 'home'
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home), PIP_CONFIG_FILE=os.devnull)
    return env


def _install(target, candidate, wheelhouse, python, work, logs, deadline, control, *, requirements=()):
    env = local_wheel_environment(work)

    def run(command, phase):
        return _run(command, phase, cwd=work, logs=logs, env=env, deadline=deadline, control=control)

    steps = {}
    for folder, extra, phase in ((work / 'tools', [], 'tool-venv'), (target, ['--without-pip'], 'target-venv')):
        steps[phase] = run([python, '-I', '-m', 'venv', *extra, str(folder)], phase)
        if steps[phase] != {'status': 'completed', 'exit_code': 0}:
            return {}, steps, 'blocked', 2, f'{phase}_unavailable'
    executable = target / VENV_PYTHON
    steps['environment'] = run([str(executable), '-I', '-c', _ENVIRONMENT], 'environment')
    try:
        environment = json.loads((logs / 'environment.log').read_bytes())
        if (steps['environment']['exit_code'] != 0 or environment['isolated_venv'] is not True
                or environment['initial_distributions'] != []):
            raise ValueError('Expected an empty venv.')
    except (OSError, ValueError, KeyError, TypeError):
        return {}, steps, 'blocked', 2, 'target_environment_not_empty_or_unavailable'
    pip = [str(work / 'tools' / VENV_PYTHON), '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check',
           '--no-input', '--no-cache-dir', '--python', str(executable)]
    steps['pip-version'] = run([*pip, '--version'], 'pip-version')
    if steps['pip-version']['exit_code'] != 0:
        return environment, steps, 'blocked', 2, 'pip_unavailable'
    # pip's version line contains its local installation path. Publish only the version token.
    version = re.search(rb'^pip ([^\s]+) from ', (logs / 'pip-version.log').read_bytes(), re.MULTILINE)
    if version is None:
        return environment, steps, 'blocked', 2, 'pip_unavailable'
    environment['pip_version'] = version[1].decode('ascii', errors='replace')
    steps['install'] = run([*pip, 'install', '--no-index', '--only-binary=:all:', '--no-compile',
                            '--find-links', wheelhouse.as_uri(), str(candidate), *requirements], 'install')
    status, code, reason = _pip_result(steps['install'], 'install')
    if code == 0:
        steps['pip-check'] = run([*pip, 'check'], 'pip-check')
        status, code, reason = _pip_result(steps['pip-check'], 'pip_check')
    return environment, steps, status, code, reason


def verify_wheel_install(wheel, wheelhouse, output, *, python_executable, timeout, confirmed, control):
    wheel = wheel.expanduser().absolute()
    wheelhouse = wheelhouse.expanduser().resolve()
    output = output.expanduser().absolute()
    python = os.path.abspath(os.path.expanduser(python_executable or sys.executable))
    _wheel_size(wheel)
    inputs = _wheelhouse_paths(wheelhouse)
    if output.exists() or output.is_symlink():
        raise ValueError('请选择尚不存在的检查报告目录。')
    if not confirmed:
        return {'status': 'ready', 'filename': wheel.name, 'wheelhouse_count': len(inputs)}, 0

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wheel-install-', dir=output.parent) as temporary:
        work = Path(temporary)
        logs = work / 'report'
        local_wheels = work / 'wheelhouse'
        candidate = work / 'candidate' / wheel.name
        for directory in (logs, local_wheels, candidate.parent):
            directory.mkdir()
        artifact, _ = _capture_wheel(wheel, _MAX_WHEEL, control, target=candidate)
        house_records = []
        environment, steps = {}, {}
        try:
            _check_metadata(candidate)
        except ValueError as exc:
            status, code, reason = 'blocked', 2, 'candidate_metadata_rejected'
            (logs / 'input.log').write_text(str(exc) + '\n', encoding='utf-8')
        else:
            try:
                house_records.extend(_wheelhouse_records(inputs, control, destination=local_wheels))
            except (OSError, ValueError) as exc:
                status, code, reason = 'blocked', 2, 'wheelhouse_input_rejected'
                (logs / 'input.log').write_text(str(exc) + '\n', encoding='utf-8')
            else:
                environment, steps, status, code, reason = _install(
                    work / 'target', candidate, local_wheels, python, work, logs,
                    time.monotonic() + timeout, control,
                )
        record = {'kind': INSTALL_CHECK_KIND, 'schema_version': 1, 'wheel': artifact,
                  'environment': environment, 'wheelhouse': house_records, 'steps': steps,
                  'status': status, 'exit_code': code, 'reason': reason}
        save_json(logs / INSTALL_CHECK_RECORD, record)
        control.commit()
        if output.exists() or output.is_symlink():
            raise ValueError('检查报告目标已存在。')
        logs.rename(output)
    return record, code
