"""Observe an old-version failure, then explicitly adopt its frozen inputs."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from .pytest_profile import read_pytest_profile
import shutil
import tempfile
import uuid

from .case import parse_test_target, valid_source_mode_hashes, legacy_mode_notice
from .diagnostics import diagnose_run
from .git_source import export_revision, resolve_commit
from .persistence import save_json
from .recipes import repository_root
from .runner import RunCancelled, RunControl, _classify, run_snapshot, snapshot, source_fingerprint, source_mode_fingerprint
from .runtime import choose_runtime
from .workspace import WORKTREE, copy_workspace, publish_snapshot_case


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _check_inputs(record, directory):
    hashes = record.get('source_sha256')
    modes = record.get('source_mode_sha256')
    before = record.get('before')
    if (not isinstance(hashes, dict) or set(hashes) != {'before', 'after'}
            or not isinstance(before, dict) or type(before.get('exit_code')) is not int):
        raise ValueError('试运行证据记录不完整，请重新试运行。')
    digests = [*hashes.values(), record.get('test_sha256'), before.get('log_sha256'), before.get('events_sha256')]
    if any(not isinstance(value, str) or len(value) != 64 for value in digests):
        raise ValueError('试运行证据指纹无效，请重新试运行。')
    if 'source_mode_sha256' in record and not valid_source_mode_hashes(modes):
        raise ValueError('试运行文件权限指纹无效，请重新试运行。')
    for label in ('before', 'after'):
        if (source_fingerprint(directory / 'inputs' / label)[1] != record['source_sha256'][label]
                or (modes is not None and source_mode_fingerprint(directory / 'inputs' / label) != modes[label])):
            raise ValueError('试运行源码证据发生变化，请重新试运行。')
    if _digest(directory / 'inputs/regression.py') != record['test_sha256']:
        raise ValueError('试运行测试证据发生变化，请重新试运行。')
    before = record['before']
    for filename, field in (('before.log', 'log_sha256'), ('before.events.json', 'events_sha256')):
        if _digest(directory / filename) != before[field]:
            raise ValueError('试运行事件或日志证据发生变化，请重新试运行。')


def probe_failure(repository, before_ref, after_ref, test_target, output_root, *,
                  python_executable=None, timeout=15, control=None):
    control = control or RunControl()
    control.check()
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 300:
        raise ValueError('试运行时限须为0.1至300秒。')
    root = repository_root(repository)
    output_root = Path(output_root).expanduser().resolve()
    if output_root.is_relative_to(root):
        raise ValueError('试运行材料必须保存在被测仓库之外。')
    target_file = parse_test_target(test_target)
    before_sha = resolve_commit(root, before_ref)
    after_sha = WORKTREE if after_ref == WORKTREE else resolve_commit(root, after_ref)
    runtime = choose_runtime(python_executable)
    probe_id = 'probe-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    directory = output_root / probe_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {'schema_version': 1, 'kind': 'failure_probe', 'status': 'preparing', 'probe_id': probe_id,
              'probe_dir': str(directory), 'repository_path': str(root), 'runtime': runtime,
              'test_target': test_target, 'requested_test_target': test_target, 'suggestion': None, 'before': None}
    save_json(directory / 'probe.json', record)
    try:
        inputs = directory / 'inputs'
        inputs.mkdir()
        control.notify('preparing')
        export_revision(root, before_sha, inputs / 'before')
        if after_sha == WORKTREE:
            current = copy_workspace(root, inputs / 'after')
        else:
            export_revision(root, after_sha, inputs / 'after')
        selected_test = inputs / 'after' / target_file
        if not selected_test.is_file():
            raise ValueError('修复后版本中没有所选测试文件。')
        shutil.copyfile(selected_test, inputs / 'regression.py')
        hashes = {label: source_fingerprint(inputs / label)[1] for label in ('before', 'after')}
        modes = {label: source_mode_fingerprint(inputs / label) for label in ('before', 'after')}
        test_hash = _digest(inputs / 'regression.py')
        template = {'source': 'snapshot', 'title': f'{root.name} 修复验收',
                    'description': '根据确认的旧版试运行故障创建，使用试运行时冻结的两版代码与测试。',
                    'test_target': test_target, 'python': runtime['executable']}
        if after_sha == WORKTREE:
            template['workspace_origin'] = {'repository': root.name, 'before_ref': before_ref, 'before_commit': before_sha,
                                            'before_sha256': hashes['before'], 'after_sha256': hashes['after'],
                                            'source_mode_sha256': modes,
                                            'test_sha256': test_hash, 'file_count': current['file_count']}
        else:
            template['git_revision'] = {'repository': root.name, 'before_ref': before_ref, 'after_ref': after_ref,
                                        'before_commit': before_sha, 'after_commit': after_sha}
        record.update(status='running', source_sha256=hashes, source_mode_sha256=modes, test_sha256=test_hash, case_template=template)
        save_json(directory / 'probe.json', record)
        control.notify('before')
        before = run_snapshot(inputs / 'before', inputs / 'regression.py', directory, 'before', timeout,
                              test_target=test_target, python_executable=runtime['executable'], control=control,
                              pytest_profile=read_pytest_profile(inputs / 'after') if test_target else None)
        if before.get('test_id'):
            record['test_target'] = before['test_id']
            template['test_target'] = before['test_id']
        diagnosis = diagnose_run(before, label='before')
        record.update(before=before, diagnosis=diagnosis, status='inconclusive',
                      explanation=diagnosis['summary'])
        if before['kind'] == 'passed':
            record.update(status='before_passed', explanation='旧版也通过这个测试，请检查版本、测试目标和断言。')
        elif before['kind'] == 'failed' and diagnosis['code'] != 'module_not_found':
            failure = before['failure']
            if failure['message'].strip():
                record.update(status='failure_observed', explanation='已观察到旧版测试主体失败，请确认它对应你要修复的问题。',
                              suggestion={'type': failure['type'], 'message_contains': failure['message'][:4000]})
            else:
                record['explanation'] = '旧版测试失败但异常消息为空，请审阅测试并提供可识别的故障信息。'
        _check_inputs(record, directory)
        control.commit()
        save_json(directory / 'probe.json', record)
        return record
    except Exception as exc:
        record.update(status='cancelled' if isinstance(exc, RunCancelled) else 'inconclusive', suggestion=None, error=str(exc))
        try:
            save_json(directory / 'probe.json', record)
        except OSError:
            pass
        if isinstance(exc, RunCancelled):
            raise
        raise ValueError(f'试运行未完成，材料保存在 {directory}。{exc}') from exc


def save_probe_case(probe_directory, path, *, confirmed=False, failure_type=None, failure_message=None, title=None):
    if confirmed is not True:
        raise ValueError('请先确认观察到的故障对应目标问题，再保存验收案例。')
    directory = Path(probe_directory).expanduser().resolve()
    probe_path = directory / 'probe.json'
    if probe_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('试运行记录过大。')
    raw = probe_path.read_bytes()
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get('kind') != 'failure_probe' or record.get('status') != 'failure_observed':
        raise ValueError('本次试运行没有可采用的完整测试主体故障。')
    if (record.get('schema_version') != 1 or not isinstance(record.get('case_template'), dict)
            or any(not isinstance(record.get(key), str) or not record[key]
                   for key in ('repository_path', 'probe_id', 'test_target'))
            or record['case_template'].get('test_target') != record['test_target']
            or record['case_template'].get('source') != 'snapshot'
            or any(not isinstance(record['case_template'].get(key), str) for key in ('title', 'description', 'python'))):
        raise ValueError('试运行记录字段不完整，请重新试运行。')
    _check_inputs(record, directory)
    events = json.loads((directory / 'before.events.json').read_bytes())
    kind, _, observed, test_id = _classify(events, record['before']['exit_code'])
    if kind != 'failed' or observed != record['before'].get('failure') or test_id != record['test_target']:
        raise ValueError('试运行故障与事件证据不一致。')
    failure_type = failure_type if failure_type is not None else observed['type']
    failure_message = failure_message if failure_message is not None else observed['message'][:4000]
    if failure_type != observed['type'] or not isinstance(failure_message, str) or not failure_message.strip() or failure_message not in observed['message']:
        raise ValueError('确认的异常类型和消息片段需要匹配这次观察到的故障。')
    destination = Path(path).expanduser()
    if destination.exists() or destination.is_symlink():
        raise ValueError('案例文件已经存在。')
    destination = destination.resolve()
    if destination.is_relative_to(Path(record['repository_path']).resolve()) or destination.is_relative_to(directory):
        raise ValueError('案例须保存到原仓库及试运行材料之外。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    config = dict(record['case_template'])
    config['title'] = title or config['title']
    config['expected_failure'] = {'type': failure_type, 'message_contains': failure_message}
    config['failure_origin'] = {'probe_id': record['probe_id'], 'probe_sha256': hashlib.sha256(raw).hexdigest(),
                                'source_sha256': record['source_sha256'], 'test_sha256': record['test_sha256'],
                                'test_target': record['test_target'],
                                'observed_failure': {'type': observed['type'], 'message': failure_message}, 'confirmed': True}
    if 'source_mode_sha256' in record:
        config['failure_origin']['source_mode_sha256'] = record['source_mode_sha256']
    with tempfile.TemporaryDirectory(prefix='.reprolab-probe-case-', dir=destination.parent) as temp:
        inputs = Path(temp) / 'inputs'
        inputs.mkdir()
        for label in ('before', 'after'):
            source = directory / 'inputs' / label
            entries, _ = source_fingerprint(source)
            copied = snapshot(source, inputs / label, entries)
            if (copied != record['source_sha256'][label]
                    or ('source_mode_sha256' in record and source_mode_fingerprint(inputs / label) != record['source_mode_sha256'][label])):
                raise ValueError('复制的试运行源码证据发生变化。')
        shutil.copyfile(directory / 'inputs/regression.py', inputs / 'regression.py')
        if _digest(inputs / 'regression.py') != record['test_sha256']:
            raise ValueError('复制的试运行测试证据发生变化。')
        publish_snapshot_case(destination, inputs, config)
    return {'case': str(destination), 'probe_id': record['probe_id'], 'expected_failure': config['expected_failure'],
            'runtime': {'executable': config['python']},
            'notice': '已用确认的故障和试运行时的固定输入创建案例，可以开始正式两版验收。' + legacy_mode_notice(record)}
