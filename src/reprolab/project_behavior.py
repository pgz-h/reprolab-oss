"""Capture version-bound JSON behavior or Python public APIs and compare independently."""
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
from .recipes import repository_root
from .runner import source_fingerprint, source_mode_fingerprint
from .runtime import child_environment, choose_runtime

_KIND = 'reprolab_behavior_comparison'
_RECORD = 'behavior.json'
_API = """
import importlib, inspect, json, os, sys
sys.path[:0] = [os.getcwd(), os.path.join(os.getcwd(), 'src')]
module = importlib.import_module(sys.argv[1])
names = getattr(module, '__all__', None)
if names is None:
    names = [name for name, value in vars(module).items()
             if not name.startswith('_') and getattr(value, '__module__', module.__name__) == module.__name__]
result = {}
for name in names:
    value = getattr(module, name)
    item = {'kind': 'callable' if callable(value) else 'value'}
    if callable(value):
        try:
            item['parameters'] = [
                {'name': param.name, 'kind': param.kind.name,
                 'required': param.default is inspect.Parameter.empty
                             and param.kind.name not in ('VAR_POSITIONAL', 'VAR_KEYWORD')}
                for param in inspect.signature(value).parameters.values()
            ]
        except (ValueError, TypeError):
            item['parameters'] = None
    else:
        item['type'] = type(value).__name__
    result[name] = item
print(json.dumps(result, sort_keys=True))
"""
_PROBE = """import os,runpy,sys
sys.path[:0] = [os.getcwd(), os.path.join(os.getcwd(), 'src')]
runpy.run_path(sys.argv[1], run_name='__main__')
"""


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _type(value):
    return type(value).__name__


def compare_values(before, after, *, policy):
    if policy not in ('exact', 'shape', 'python_api'):
        raise ValueError('比较策略须为 exact、shape 或 python_api。')
    findings = []

    def add(path, reason):
        if len(findings) >= 5000:
            raise ValueError('行为差异超过 5000 项，请缩小观察范围。')
        findings.append({'path': path, 'reason': reason})

    def walk(old, new, path):
        if type(old) is not type(new):
            add(path, f'type_changed:{_type(old)}:{_type(new)}')
        elif isinstance(old, dict):
            for key in sorted(old.keys() - new.keys()):
                add([*path, key], 'missing')
            if policy == 'exact':
                for key in sorted(new.keys() - old.keys()):
                    add([*path, key], 'added')
            for key in sorted(old.keys() & new.keys()):
                walk(old[key], new[key], [*path, key])
        elif isinstance(old, list):
            if len(new) < len(old):
                add(path, 'list_shortened')
            elif policy == 'exact' and len(new) > len(old):
                add(path, 'list_length_changed')
            for index, (left, right) in enumerate(zip(old, new)):
                walk(left, right, [*path, index])
        elif policy == 'exact' and old != new:
            add(path, 'value_changed')

    if policy == 'python_api':
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError('Python API 观察须为公开成员映射。')
        for name, reference in before.items():
            current = after.get(name)
            if current is None:
                add([name], 'removed_export')
            elif reference.get('kind') != current.get('kind'):
                add([name], 'export_kind_changed')
            elif reference['kind'] == 'value':
                if reference.get('type') != current.get('type'):
                    add([name], 'export_type_changed')
            else:
                old, new = reference.get('parameters'), current.get('parameters')
                if old is None or new is None:
                    add([name], 'signature_unknown')
                elif old != new[:len(old)] or any(item['required'] for item in new[len(old):]):
                    add([name], 'signature_changed')
    else:
        walk(before, after, [])
    return findings


def _outcome(before, after, policy):
    if any(item.get('status') != 'completed' for item in (before, after)):
        return 'blocked', []
    changes = compare_values(before['value'], after['value'], policy=policy)
    status = ('failed' if any(row['reason'] != 'signature_unknown' for row in changes)
              else 'blocked' if changes else 'passed')
    return status, changes


def capture_behavior(repository, before, after, outcome, *, probe=None, module=None,
                     python_executable, timeout, policy, confirmed, control):
    root = repository_root(repository)
    if bool(probe) == bool(module):
        raise ValueError('请选择 JSON 探针文件或 Python 模块。')
    if module and not all(re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', item) for item in module.split('.')):
        raise ValueError('Python 模块名无效。')
    if module:
        policy = 'python_api'
    revisions = {side: resolve_commit(root, value) for side, value in (('before', before), ('after', after))}
    if not confirmed:
        return {'status': 'ready', 'revisions': revisions, 'policy': policy}, 0
    output = Path(outcome).expanduser().absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(root):
        raise ValueError('请选择仓库之外的新行为证据目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime = choose_runtime(python_executable)
    with tempfile.TemporaryDirectory(prefix='.behavior-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'behavior'
        (stage / 'inputs').mkdir(parents=True)
        probe_digest = None
        if probe:
            path = Path(probe).expanduser()
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise ValueError('请选择不超过 1 MiB 的普通 Python 探针。')
            target = stage / 'probe.py'
            shutil.copyfile(path, target)
            probe_digest = _sha(target)
        fingerprints, runs = {}, {}
        for side, commit in revisions.items():
            control.check()
            snapshot = stage / 'inputs' / side
            export_revision(root, commit, snapshot)
            fingerprints[side] = {'sha256': source_fingerprint(snapshot)[1],
                                  'mode_sha256': source_mode_fingerprint(snapshot)}
            with tempfile.TemporaryDirectory(prefix='behavior-worker-') as work:
                copy = Path(work) / 'source'
                shutil.copytree(snapshot, copy)
                log = stage / (side + '.log')
                command = [runtime['executable'], '-I', '-X', 'utf8', '-c', _API if module else _PROBE,
                           module if module else str(target)]
                with log.open('wb') as stream:
                    status, code = run_logged_command(command, copy, child_environment(), stream, time.monotonic() + timeout, control)
                observed = {'status': 'blocked', 'process_status': status, 'exit_code': code}
                if status == 'completed' and code == 0 and log.stat().st_size <= 2 * 1024 * 1024:
                    try:
                        data = json.loads(log.read_bytes())
                        json.dumps(data, allow_nan=False)
                    except (ValueError, UnicodeError, RecursionError):
                        observed['issue'] = 'invalid_json_probe'
                    else:
                        observed.update(status='completed', value=data)
                runs[side] = observed
        verdict, findings = _outcome(runs['before'], runs['after'], policy)
        record = {'kind': _KIND, 'schema_version': 1, 'status': verdict, 'revisions': revisions,
                  'frozen_sources': fingerprints, 'probe_sha256': probe_digest, 'module': module, 'policy': policy,
                  'runs': runs, 'findings': findings, 'log_sha256': {side: _sha(stage / (side + '.log')) for side in runs}}
        save_json(stage / _RECORD, record)
        control.commit()
        if output.exists() or output.is_symlink():
            raise ValueError('报告目标已存在。')
        stage.rename(output)
    return {**record, 'directory': str(output)}, {'passed': 0, 'failed': 1, 'blocked': 2}[verdict]


def check_behavior(directory, *, control):
    directory = Path(directory).expanduser().resolve()
    file = directory / _RECORD
    if not file.is_file() or file.is_symlink() or file.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('请选择完整的行为证据目录。')
    record = json.loads(file.read_bytes())
    if record.get('kind') != _KIND or record.get('schema_version') != 1:
        raise ValueError('行为证据格式无效。')
    for side in ('before', 'after'):
        control.check()
        snapshot = directory / 'inputs' / side
        if record['frozen_sources'][side] != {'sha256': source_fingerprint(snapshot)[1],
                                             'mode_sha256': source_mode_fingerprint(snapshot)}:
            raise ValueError('行为证据的固定源码不一致。')
        log = directory / (side + '.log')
        if log.is_symlink() or _sha(log) != record['log_sha256'][side]:
            raise ValueError('行为日志指纹不符。')
        run = record['runs'][side]
        if run['status'] == 'completed' and json.dumps(json.loads(log.read_bytes()), sort_keys=True) != json.dumps(
                run['value'], sort_keys=True):
            raise ValueError('行为记录与原始观测不一致。')
    probe = directory / 'probe.py'
    if record['probe_sha256'] is not None and (probe.is_symlink() or _sha(probe) != record['probe_sha256']):
        raise ValueError('探针指纹不符。')
    if _outcome(record['runs']['before'], record['runs']['after'], record['policy']) != (record['status'], record['findings']):
        raise ValueError('行为差异与保存的观察不一致。')
    return {**record, 'integrity': 'consistent'}
