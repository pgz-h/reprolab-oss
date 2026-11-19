"""Run a trusted root CMake project and verify selected CTest observations offline."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET

from .persistence import save_json
from .process_tree import run_logged_command
from .runtime import child_environment


_LIMIT = 16 * 1024 * 1024
_LOGS = {'cmake': 'cmake-version.log', 'ctest': 'ctest-version.log',
         'configure': 'configure.log', 'build': 'build.log',
         'discover': 'discovery.json', 'run': 'ctest.log'}


def _read(path):
    with path.open('rb') as stream:
        data = stream.read(_LIMIT + 1)
    if len(data) > _LIMIT:
        raise ValueError('CTest evidence exceeds the supported size.')
    return data


def _version(folder, tool):
    header = _read(folder / _LOGS[tool]).splitlines()[:1]
    match = re.fullmatch(tool.encode() + rb' version (\d+)\.(\d+)\.(\d+)[^\r\n]*', header[0]) if header else None
    return tuple(int(value) for value in match.groups()) if match else ()


def _inventory(folder):
    data = json.loads(_read(folder / _LOGS['discover']))
    if (not isinstance(data, dict) or data.get('kind') != 'ctestInfo'
            or not isinstance(data.get('version'), dict) or data['version'].get('major') != 1
            or not isinstance(data.get('tests'), list) or len(data['tests']) > 10000):
        raise ValueError('Invalid CTest discovery.')
    names = {}
    for row in data['tests']:
        name = row.get('name') if isinstance(row, dict) else None
        if not isinstance(name, str) or not name or len(name) > 1000 or not name.strip() or not name.isprintable():
            raise ValueError('Invalid CTest identity.')
        if name in names:
            raise ValueError('Ambiguous CTest identity.')
        names[name] = row
    return names


def _settings_digest(row, roots):
    command = row.get('command')
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        return None

    def canonical(value):
        if isinstance(value, str):
            for label, paths in roots.items():
                for path in paths:
                    if value == path or value.startswith(path + '/'):
                        return ['local-path', label, value[len(path):]]
            return ['literal', value]
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        return value

    settings = [row.get('config'), command, row.get('properties', [])]
    data = json.dumps(canonical(settings), sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()


def _observations(folder, names, exit_code):
    raw = _read(folder / 'results.xml')
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise ValueError('Unsupported CTest XML.')
    suite = ET.fromstring(raw.decode('utf-8-sig'))
    cases = suite.findall('testcase')
    totals = {label: int(suite.attrib[label]) for label in ('tests', 'failures', 'disabled', 'skipped')}
    if (suite.tag != 'testsuite' or totals['tests'] != len(cases) or len(cases) != len(names)
            or totals['failures'] != sum(case.get('status') == 'fail' for case in cases)
            or totals['disabled'] != sum(case.get('status') == 'disabled' for case in cases)
            or totals['skipped'] != sum(case.get('status') == 'notrun' for case in cases)):
        raise ValueError('Incomplete CTest results.')
    observations = {}
    for case in cases:
        name = case.get('name')
        if name in observations:
            raise ValueError('Duplicate CTest result.')
        status = case.get('status')
        findings = [child for child in case if child.tag not in ('properties', 'system-out', 'system-err')]
        if status == 'run' and not findings:
            outcome = 'passed'
        elif (status == 'fail' and len(findings) == 1 and findings[0].tag == 'failure'
              and findings[0].get('message') == 'Failed'):
            outcome = 'failed'
        elif status == 'disabled' and not findings:
            outcome = 'skipped'
        elif (status == 'notrun' and len(findings) == 1 and findings[0].tag == 'skipped'
              and re.fullmatch(r'SKIP_RETURN_CODE=-?[0-9]+|SKIP_REGULAR_EXPRESSION_MATCHED',
                               findings[0].get('message', ''))):
            outcome = 'skipped'
        else:
            outcome = 'blocked'
        observations[name] = outcome
    if set(observations) != set(names) or exit_code not in (0, 8):
        raise ValueError('CTest results disagree with discovery or runner exit.')
    if exit_code == 0 and 'failed' in observations.values():
        raise ValueError('CTest returned success with a failed test.')
    if exit_code == 8 and all(value in ('passed', 'skipped') for value in observations.values()):
        raise ValueError('CTest returned failure without a recorded failure.')
    return observations


def _side_result(folder, selected, phases):
    def blocked(reason):
        return {'status': 'blocked', 'tests': {}, 'reason': reason}

    for stage in ('cmake', 'ctest', 'configure', 'build', 'discover', 'run'):
        row = phases.get(stage)
        if row is None:
            return blocked(stage + '_unavailable')
        if not isinstance(row, dict) or not (folder / _LOGS[stage]).is_file():
            return blocked('incomplete_ctest_evidence')
        if row.get('status') == 'completed' and type(row.get('exit_code')) is not int:
            return blocked('incomplete_ctest_evidence')
        if row['status'] != 'completed' or (stage != 'run' and row['exit_code'] != 0):
            return blocked(stage + '_' + ('failed' if row['status'] == 'completed' else row['status']))
        if stage in ('cmake', 'ctest') and _version(folder, stage) < (3, 29, 0):
            return blocked(stage + '_version_unsupported')
        if stage == 'discover':
            names = _inventory(folder)
            if not names:
                return blocked('selected_scope_mismatch' if selected else 'no_tests_discovered')
            if selected is not None and set(names) != set(selected):
                return blocked('selected_scope_mismatch')
    tests = _observations(folder, names, phases['run']['exit_code'])
    result = {'status': 'blocked' if 'blocked' in tests.values() else 'completed', 'tests': tests}
    if roots := phases.get('roots'):
        if (not isinstance(roots, dict) or set(roots) != {'source', 'build'}
                or any(not isinstance(paths, list) or not 1 <= len(paths) <= 2 for paths in roots.values())
                or any(not isinstance(path, str) or not path or path.endswith('/') for paths in roots.values() for path in paths)):
            return blocked('invalid_ctest_path_record')
        result['test_settings'] = {name: digest for name, row in names.items()
                                   if (digest := _settings_digest(row, roots)) is not None}
    return result


def read_cmake(evidence_dir, *, selected):
    try:
        phases = json.loads(_read(evidence_dir / 'commands.json'))
        if not isinstance(phases, dict):
            raise ValueError('Invalid CTest phase record.')
        return _side_result(evidence_dir, selected, phases)
    except (ValueError, TypeError, OSError, KeyError, ET.ParseError, RecursionError):
        return {'status': 'blocked', 'tests': {}, 'reason': 'incomplete_ctest_evidence'}


def run_cmake(snapshot, evidence_dir, *, selected, timeout, control):
    deadline = time.monotonic() + timeout
    phases = {}
    env = child_environment()

    def run(stage, command):
        with (evidence_dir / _LOGS[stage]).open('wb') as log:
            try:
                status, code = run_logged_command(command, snapshot, env, log, deadline, control)
            except OSError:
                status, code = 'unavailable', None
        phases[stage] = {'status': status, 'exit_code': code}
        return status == 'completed' and code == 0

    cmake, ctest = (shutil.which(name) for name in ('cmake', 'ctest'))
    with tempfile.TemporaryDirectory(prefix='reprolab-cmake-') as temporary:
        build = Path(temporary) / 'build'
        phases['roots'] = {key: sorted({path.as_posix(), path.resolve().as_posix()})
                           for key, path in (('source', snapshot), ('build', build))}
        options = ['--test-dir', str(build), '-C', 'Debug']
        if selected:
            selection = Path(temporary) / 'selected.txt'
            selection.write_text('\n'.join(selected) + '\n', encoding='utf-8')
            options.extend(['--tests-from-file', str(selection)])
        commands = [
            ('cmake', [cmake, '--version']),
            ('ctest', [ctest, '--version']),
            ('configure', [cmake, '-S', str(snapshot), '-B', str(build), '-DBUILD_TESTING=ON', '-DCMAKE_BUILD_TYPE=Debug']),
            ('build', [cmake, '--build', str(build), '--config', 'Debug']),
            ('discover', [ctest, *options, '--show-only=json-v1']),
        ]
        for stage, command in commands:
            if not command[0] or not run(stage, command):
                break
            if stage in ('cmake', 'ctest') and _version(evidence_dir, stage) < (3, 29, 0):
                break
        else:
            try:
                names = _inventory(evidence_dir)
            except (OSError, ValueError, RecursionError):
                names = []
            if names and (selected is None or set(names) == set(selected)):
                run('run', [ctest, *options, '-j', '1', '--no-tests=error',
                            '--output-junit', str(evidence_dir / 'results.xml')])
    save_json(evidence_dir / 'commands.json', phases)
    return read_cmake(evidence_dir, selected=selected)
