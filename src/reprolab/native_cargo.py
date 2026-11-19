"""Cargo single-package library unit tests using the stable libtest console protocol."""
import json
import os
from pathlib import Path
import re
import shutil
import time
import tomllib

from .persistence import save_json
from .process_tree import run_logged_command
from .runtime import child_environment

_MAX_LOG = 8 * 1024 * 1024
_MAX_CASES = 500
_RECORD = 'cargo-run.json'
_SUMMARY = re.compile(
    r'test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; '
    r'(\d+) measured; (\d+) filtered out(?:; finished in [0-9.]+s)?'
)


def _read_log(path):
    with path.open('rb') as stream:
        payload = stream.read(_MAX_LOG + 1)
    if len(payload) > _MAX_LOG:
        return None
    try:
        return payload.decode('utf-8')
    except UnicodeError:
        return None


def _test_binary(log):
    if log is None:
        return None
    paths = []
    for line in log.splitlines():
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if (isinstance(message, dict) and message.get('reason') == 'compiler-artifact'
                and message.get('profile', {}).get('test') is True and isinstance(message.get('executable'), str)):
            paths.append(message['executable'])
    return paths[0] if len(paths) == 1 else None


def _catalog(log):
    if log is None:
        return None
    tests, summary = [], None
    for line in log.splitlines():
        if not line:
            continue
        if line.endswith(': test') and summary is None:
            name = line[:-6]
            if not name or len(name) > 1000 or any(ord(c) < 32 for c in name):
                return None
            tests.append(name)
        elif match := re.fullmatch(r'([0-9]+) tests?, ([0-9]+) benchmarks?', line):
            if summary is not None or int(match[2]) != 0:
                return None
            summary = int(match[1])
        else:
            # Benches, custom harnesses and unrecognized formats are outside this pilot.
            return None
    if (summary is not None and summary != len(tests)) or len(tests) != len(set(tests)) or len(tests) > 10000:
        return None
    return tests


def _case_outcome(name, phase, text, *, catalog_size):
    if phase['status'] != 'completed' or text is None:
        return 'blocked'
    prefix = f'test {name} ... '
    lines = text.splitlines()
    reports = [line[len(prefix):].strip() for line in lines if line.startswith(prefix)]
    totals = [match for line in lines if (match := _SUMMARY.fullmatch(line))]
    if len(reports) != 1 or len(totals) != 1 or not any(line == 'running 1 test' for line in lines):
        return 'blocked'
    summary = totals[0]
    counts = tuple(map(int, summary.groups()[1:]))
    if counts[3:] != (0, catalog_size - 1):
        return 'blocked'
    result = reports[0]
    if result == 'ok' and summary[1] == 'ok' and counts[:3] == (1, 0, 0) and phase['exit_code'] == 0:
        return 'passed'
    if result == 'FAILED' and summary[1] == 'FAILED' and counts[:3] == (0, 1, 0) and phase['exit_code'] == 101:
        return 'failed'
    if ((result == 'ignored' or result.startswith('ignored,')) and summary[1] == 'ok'
            and counts[:3] == (0, 0, 1) and phase['exit_code'] == 0):
        return 'skipped'
    return 'blocked'


def _phase_ok(phase):
    return phase is not None and phase.get('status') == 'completed' and phase.get('exit_code') == 0


def _projection(evidence, record, selected):
    if record.get('reason'):
        return {'status': 'blocked', 'reason': record['reason'], 'tests': {}}
    if not (_phase_ok(record.get('build')) and _test_binary(_read_log(evidence / 'build.log'))):
        return {'status': 'blocked', 'reason': 'cargo_build_unverified', 'tests': {}}
    if not _phase_ok(record.get('list')):
        return {'status': 'blocked', 'reason': 'libtest_inventory_unverified', 'tests': {}}
    catalog = _catalog(_read_log(evidence / 'list.log'))
    if catalog is None:
        return {'status': 'blocked', 'reason': 'libtest_inventory_unverified', 'tests': {}}
    available = set(catalog)
    wanted = catalog if selected is None else [name for name in selected if name in available]
    if not wanted or len(wanted) > _MAX_CASES:
        reason = 'no_selected_tests' if not wanted else 'too_many_tests_choose_exact_targets'
        return {'status': 'blocked', 'reason': reason, 'tests': {}, 'discovered_count': len(catalog)}
    phases = record.get('cases')
    if not isinstance(phases, list) or len(phases) != len(wanted):
        raise ValueError('Cargo 单项运行记录不完整。')
    tests = {}
    for index, (name, phase) in enumerate(zip(wanted, phases), start=1):
        if phase.get('name') != name:
            raise ValueError('Cargo 单项运行顺序未匹配保存的清单。')
        tests[name] = _case_outcome(name, phase, _read_log(evidence / f'test-{index:04d}.log'),
                                    catalog_size=len(catalog))
    complete = (selected is None or len(selected) == len(wanted)) and 'blocked' not in tests.values()
    return {'status': 'completed' if complete else 'blocked', 'tests': tests, 'discovered_count': len(catalog)}


def run_cargo(snapshot, evidence_dir, *, selected, timeout, control, cargo=None):
    deadline = time.monotonic() + timeout
    record = {'schema_version': 1}
    manifest = snapshot / 'Cargo.toml'
    try:
        with manifest.open('rb') as stream:
            data = stream.read(1024 * 1024 + 1)
        config = tomllib.loads(data.decode('utf-8')) if len(data) <= 1024 * 1024 else {}
    except (OSError, ValueError):
        config = {}
    env = child_environment()
    env.update({key: os.environ[key] for key in ('CARGO_HOME', 'RUSTUP_HOME') if key in os.environ})
    env['CARGO_TERM_COLOR'] = 'never'
    env['CARGO_TARGET_DIR'] = str(snapshot.parent / 'cargo-target')
    executable = shutil.which(str(Path(cargo).expanduser()) if cargo else 'cargo')
    if not isinstance(config.get('package'), dict) or 'workspace' in config or 'workspace' in config['package']:
        record['reason'] = 'requires_single_root_package'
    elif isinstance(config.get('lib'), dict) and config['lib'].get('harness') is False:
        record['reason'] = 'unsupported_custom_harness'
    elif not (snapshot / 'Cargo.lock').is_file():
        record['reason'] = 'requires_tracked_cargo_lock'
    elif executable is None:
        record['reason'] = 'cargo_unavailable'
    else:
        env['PATH'] = os.pathsep.join((str(Path(executable).parent), env.get('PATH', os.defpath)))

        def launch(arguments, log_name):
            status, code = 'unavailable', None
            with (evidence_dir / log_name).open('wb') as log:
                try:
                    status, code = run_logged_command(arguments, snapshot, env, log, deadline, control)
                except OSError as exc:
                    log.write(str(exc).encode('utf-8', errors='replace'))
            return {'status': status, 'exit_code': code}

        record['build'] = launch([executable, 'test', '--lib', '--locked', '--offline', '--no-run',
                                  '--message-format=json'], 'build.log')
        binary = _test_binary(_read_log(evidence_dir / 'build.log')) if _phase_ok(record['build']) else None
        if binary:
            path = Path(binary).resolve()
            if (not path.is_relative_to(Path(env['CARGO_TARGET_DIR']).resolve()) or not path.is_file()
                    or Path(binary).is_symlink()):
                record['reason'] = 'cargo_test_binary_unverified'
            else:
                record['list'] = launch([str(path), '--list', '--format=terse'], 'list.log')
                catalog = _catalog(_read_log(evidence_dir / 'list.log')) if _phase_ok(record['list']) else None
                if catalog is not None:
                    available = set(catalog)
                    wanted = catalog if selected is None else [name for name in selected if name in available]
                    if 1 <= len(wanted) <= _MAX_CASES:
                        record['cases'] = [
                            {'name': name, **launch([str(path), '--exact', '--test-threads=1', '--color=never', '--', name],
                                                   f'test-{index:04d}.log')}
                            for index, name in enumerate(wanted, start=1)
                        ]
    save_json(evidence_dir / _RECORD, record)
    return _projection(evidence_dir, record, selected)


def read_cargo(evidence_dir, *, selected):
    path = evidence_dir / _RECORD
    if path.is_symlink() or not path.is_file():
        raise ValueError('Cargo 运行证据缺少阶段记录。')
    record = json.loads(_read_log(path) or '')
    if not isinstance(record, dict) or record.get('schema_version') != 1:
        raise ValueError('Cargo 阶段记录格式无效。')
    return _projection(evidence_dir, record, selected)
