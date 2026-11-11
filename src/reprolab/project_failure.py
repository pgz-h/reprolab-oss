"""Report-bound repair material and portable cases. Preparation never runs code."""
from datetime import datetime, timezone
import difflib
import hashlib
import io
from itertools import islice
import json
from pathlib import Path
import re
import shutil
import tempfile
import tokenize
import uuid

from .case import parse_test_target, validate_project_failure_origin
from .draft_context import MAX_FILE_BYTES, MAX_CONTEXT_BYTES, MAX_FILES, _allowed
from .persistence import atomic_write, save_json
from .project_diagnostics import entry_guidance
from .project_history import _checked_report, _match_reference, report_reference
from .project_sessions import _read, _digest, read_project
from .recipes import repository_root
from .runner import RunControl, snapshot, source_fingerprint, source_mode_fingerprint
from .workspace import copy_workspace, publish_snapshot_case


def _now():
    return datetime.now(timezone.utc).isoformat()


def repair_eligibility(entry, report):
    """Cheap UI advice; the preparation boundary rechecks all saved evidence."""
    outcome, version = entry.get('after') or {}, report.get('after') or {}
    phases = outcome.get('phases') or []
    error = outcome.get('exception') or {}
    if not isinstance(error, dict):
        return False, '异常记录格式不完整，请重新运行后准备。'
    if report.get('status') == 'cancelled' or version.get('cancelled'):
        return False, '来源运行已取消；请先完成一次完整运行，再准备严格修复案例。'
    if version.get('timed_out') or version.get('status') == 'timed_out':
        return False, '变更后运行超时；先检查等待、时限及日志，再明确重跑。'
    if version.get('collection_errors') or version.get('status') == 'collection_failed':
        return False, '变更后有收集错误；先处理导入或项目环境，重新发现并运行。'
    if error.get('type') in ('ModuleNotFoundError', 'ImportError'):
        return False, '这是导入／缺少依赖问题；先通过项目环境入口核对依赖和解释器。'
    if any(p.get('phase') in ('setup', 'teardown') and p.get('outcome') == 'failed'
           for p in phases if isinstance(p, dict)):
        return False, 'fixture 准备／清理失败，尚未得到完整测试主体故障；先处理对应阶段。'
    if outcome.get('status') == 'skipped' or any(
            p.get('xfail') or p.get('outcome') == 'skipped' for p in phases if isinstance(p, dict)):
        return False, '测试被跳过或标记 xfail；需核对条件后重新完整运行，不能直接验收。'
    if (version.get('status') != 'completed' or version.get('worker_record_complete') is not True
            or version.get('exit_code') != 1):
        return False, '变更后没有完整失败运行记录；请查看两版日志和诊断后重新运行。'
    if outcome.get('status') != 'failed':
        return False, '第一版仅支持来源报告的变更后版本实际发生测试主体失败的单个测试。'
    if ([p.get('phase') for p in phases if isinstance(p, dict)] != ['setup', 'call', 'teardown']
            or [p.get('outcome') for p in phases if isinstance(p, dict)] != ['passed', 'failed', 'passed']
            or not isinstance(error.get('type'), str) or not re.fullmatch(r'[A-Za-z_]\w*', error['type'])
            or not isinstance(error.get('message'), str) or not error['message'].strip()
            or phases[1].get('exception') != error):
        return False, '测试阶段或异常记录不完整，不能预填可确认的稳定故障条件；请重新运行。'
    return True, '读取原报告实际失败的变更后快照，另存当前工作区；只准备，不运行。'


def _selected(directory, test_id, expected_reference, control):
    control.check()
    report = _checked_report(directory, control=control)
    if expected_reference is not None:
        _match_reference(report, expected_reference)
    name = parse_test_target(test_id)
    matches = [row for row in report['entries'] if row.get('test_id') == test_id]
    if len(matches) != 1 or report['selected_tests'].count(test_id) != 1 or '::' not in test_id:
        raise ValueError('请选择这份报告实际执行的一个完整测试 ID，不能使用文件或同名测试替代。')
    root = Path(report['report_dir']) / 'evidence/project/inputs/after'
    test = root / name
    digest = (report.get('shared_tests') or {}).get(name)
    if (not digest or test.is_symlink() or not test.is_file()
            or not test.resolve().is_relative_to(root.resolve()) or _digest(test) != digest):
        raise ValueError('来源报告缺少有效的共享测试指纹，或测试证据已变化；请恢复完整来源材料后重试。')
    control.check()
    return report, matches[0], test


def _material_allowed(name):
    parts = Path(name).parts
    return (_allowed(name) and not any(re.search(
        r'(?i)(credential|secret|password|token|connection|ai[._-]local|model[._-]config|^\.aws$|^\.codex$)', p)
        for p in parts))


def _clean(text, report):
    """Remove machine roots and obvious credential/connection lines from shareable text."""
    text = str(text)
    for root, replacement in ((report.get('repository'), ''), (report.get('report_dir'), '<来源报告>'),
                              (report.get('project_directory'), '<来源会话>')):
        if isinstance(root, str) and root:
            text = text.replace(root.rstrip('/') + '/', replacement + ('/' if replacement else ''))
            text = text.replace(root, replacement or '<项目>')
    text = re.sub(r'/(?:Users|home)/[^/\s]+/', '<用户目录>/', text)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)',
                  '[已排除私钥]', text)
    for quote in ('"""', "'''"):
        pattern = (r'(?is)(?:api[_-]?key|password|passwd|secret|private[_-]?key|access[_-]?token)'
                   + r'["\'\]]*\s*[=:]\s*[rubf]*' + re.escape(quote) + r'.*?(?:' + re.escape(quote) + r'|$)')
        text = re.sub(pattern, '[已排除多行凭据]', text)
    text = re.sub(r'(?i)Bearer\s+[A-Za-z0-9_.~+/-]+=*', 'Bearer [已排除凭据]', text)
    text = re.sub(r'(?i)\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}', '[已排除凭据]', text)
    lines = []
    for line in text.splitlines():
        if re.search(r'(?i)(api[_-]?key|access[_-]?token|authorization|password|passwd|client[_-]?secret|'
                     r'api[_-]?(?:base|url)|base[_-]?url|endpoint|secret|private[_-]?key|token|model[_-]?(?:config|connection))[\"\'\]]*\s*[=:]', line):
            lines.append('[已排除凭据或模型连接配置行]')
        else:
            lines.append(re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[已排除凭据]@', line))
    return '\n'.join(lines)


def _bounded(text, limit=MAX_FILE_BYTES):
    raw = str(text).encode('utf-8')
    if len(raw) <= limit:
        return str(text)
    marker = f'\n[已截断：本段原有 {len(raw)} 字节；仅保留前部，完整记录仍在来源证据中。]'
    return raw[:max(0, limit - len(marker.encode('utf-8')))].decode('utf-8', 'ignore') + marker


def _test_text(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    text = raw[:MAX_FILE_BYTES].decode(encoding, 'replace')
    if len(raw) > MAX_FILE_BYTES:
        text += '\n[已截断：测试文件仅保留前 24 KiB，完整文件保存在来源报告。]'
    return text


def load_repair_material(directory, test_id, *, expected_reference=None, control=None):
    """Read only checked historical material; no live worktree or model config."""
    control = control or RunControl()
    report, entry, test = _selected(directory, test_id, expected_reference, control)
    blocks = {'test': {'title': '历史实际共享测试 · ' + parse_test_target(test_id),
                       'text': _bounded(_clean(_test_text(test), report))}}
    for side in ('before', 'after'):
        control.check()
        path = Path(report['report_dir']) / (side + '.log')
        with path.open('rb') as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_FILE_BYTES))
            raw = stream.read(MAX_FILE_BYTES)
        text = raw.decode('utf-8', 'replace')
        if size > MAX_FILE_BYTES:
            text = f'[已省略日志前部：原有 {size} 字节；以下仅为末尾 24 KiB。]\n' + text
        blocks['log:' + side] = {'title': '历史整版运行日志片段 · ' + side + '（可能含其他测试）',
                                'text': _bounded(_clean(text, report))}
    saved_state = read_project(Path(report['report_dir']) / 'evidence/project')
    for item in (saved_state.get('changes') or {}).get('files', []):
        if _material_allowed(item.get('path', '')):
            blocks['diff:' + item['path']] = {'title': '历史基线 → 变更后差异 · ' + item['path'],
                'text': _bounded(_clean((item.get('diff') or '') + '\n' + (item.get('note') or ''), report))}
    # Detect changed evidence/identity again after reading the chosen historical files.
    control.check()
    _match_reference(_checked_report(directory, control=control), report_reference(report))
    return {'schema_version': 1, 'kind': 'project_repair_material', 'reference': report_reference(report),
            'test_id': test_id, 'test_sha256': _digest(test), 'entry': entry,
            'issue': _clean(saved_state.get('issue', ''), report),
            'source_sha256': report['source_sha256'], 'created_at': report.get('created_at'),
            'blocks': blocks, 'diagnostics': entry_guidance(entry, report),
            'diff_notice': ('历史差异列表已截断，未列出的文件未加入。' if (saved_state.get('changes') or {}).get('truncated') else ''),
            'path_roots': {key: report.get(key) for key in ('repository', 'report_dir', 'project_directory')}}


def render_repair_brief(material, *, problem, expected_behavior, allowed_changes, selected_blocks=None, current_material=''):
    """One exact shareable string; editors, clipboard and export all use this value."""
    for label, value in (('问题描述', problem), ('用户确认的期望行为', expected_behavior), ('允许修改的目标', allowed_changes)):
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 8000:
            raise ValueError(label + '须填写，且不超过 8000 字节。')
    keys = ['test'] if selected_blocks is None else list(dict.fromkeys(selected_blocks))
    if len(keys) > MAX_FILES or any(key not in material['blocks'] for key in keys):
        raise ValueError('请选择最多 12 段可用的历史材料。')
    ref, entry = material['reference'], material['entry']
    lines = ['# 修复任务（本地整理，发送由用户决定）', '', '## 用户确认的任务',
             '问题：' + problem.strip(), '期望行为：' + expected_behavior.strip(),
             '允许修改：' + allowed_changes.strip(),
             '保持现有断言表达的行为要求；若认为预期本身应修改，先列出事实依据，交由用户决定。',
             '原因尚待确认。下面是历史观察，不把推测写成已证实根因。', '', '## 来源与实际观察',
             f"会话：{ref['project_id']}\n运行：{ref['run_id']}\n报告 SHA-256：{ref['report_sha256']}",
             '执行记录时间：' + str(material.get('created_at') or '未记录'),
             '完整测试 ID：' + material['test_id'], '实际共享测试 SHA-256：' + material['test_sha256']]
    for side, label in (('before', '原报告基线'), ('after', '原报告变更后')):
        row = entry.get(side) or {}
        lines.extend([label + '快照 SHA-256：' + material['source_sha256'][side],
                      label + '实际状态：' + str(row.get('status', '未记录')),
                      '异常：' + json.dumps(row.get('exception'), ensure_ascii=False),
                      '阶段（保存的记录）：' + json.dumps(row.get('phases', []), ensure_ascii=False)])
    lines.extend(['', '处理建议：' + '\n'.join(material['diagnostics']), '', '## 验收方式',
        '修改原项目业务源码后，从本条实际失败准备严格案例：故障版用本报告真正失败的变更后快照，候选版用新冻结的当前工作区。',
        '默认两版运行本报告的同一份测试；先确认稳定非空异常片段并保存，再由用户明确执行。',
        '只有本次故障版复现声明的故障、候选版通过且记录完整，才可称修复验证通过；单项未复现须如实记录。',
        '随后用已有项目复查检查相关回归；历史通过的原报告基线只是背景。', '', '## 选择加入的历史材料',
        '未选择的日志、代码或差异均已省略；以下各段可能经过用户删减，请保留省略标记。', material.get('diff_notice', '')])
    for key in keys:
        block = material['blocks'][key]
        lines.extend(['', '### ' + block['title'], block['text']])
    omitted = [block['title'] for key, block in material['blocks'].items() if key not in keys]
    if omitted:
        lines.extend(['', '明确省略：' + '；'.join(omitted)])
    if current_material.strip():
        lines.extend(['', '## 当前材料（用户另行提供，未经来源运行验证）',
                      '以下内容不属于上述历史证据；可能来自修复后的当前工作区。', _bounded(current_material)])
    text = _clean('\n'.join(lines), material.get('path_roots', {}))
    return _bounded(text, MAX_CONTEXT_BYTES)


def export_repair_brief(path, text, *, protected=()):
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_CONTEXT_BYTES:
        raise ValueError('预览须为非空文本，且不超过 96 KiB；请先删减预览。')
    target = Path(path).expanduser().resolve()
    if any(target.is_relative_to(Path(root).expanduser().resolve()) for root in protected if root):
        raise ValueError('请将修复说明保存在来源报告和被测仓库之外，保留原证据。')
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, text)
    return {'file': str(target), 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'bytes': len(text.encode('utf-8')), 'notice': '已保存与预览完全一致的本地文本。'}


def _copy_checked(source, destination, expected_hash, expected_mode, control):
    control.check()
    entries, digest = source_fingerprint(source)
    if digest != expected_hash or source_mode_fingerprint(source) != expected_mode:
        raise ValueError('待复制快照与记录不一致。')
    if (snapshot(source, destination, entries, control=control) != digest
            or source_mode_fingerprint(destination) != expected_mode
            or source_fingerprint(source)[1] != digest or source_mode_fingerprint(source) != expected_mode):
        raise ValueError('复制期间快照发生变化，未保存；请重试。')
    control.check()


def _test_change(original, current, name):
    if not current.is_file():
        return {'status': 'missing', 'sha256': None, 'diff': '当前候选快照没有此测试；正式执行仍会覆入历史原检查。'}
    digest = _digest(current)
    if _digest(original) == digest:
        return {'status': 'same', 'sha256': digest, 'diff': '当前测试文件与历史原检查相同。'}
    old, new = _test_text(original), _test_text(current)
    diff = '\n'.join(islice(difflib.unified_diff(old.splitlines(), new.splitlines(),
                    fromfile='report/' + name, tofile='candidate/' + name, lineterm=''), 201))
    return {'status': 'changed', 'sha256': digest, 'diff': _bounded(diff +
        '\n[差异仅预览前 200 行及每个文件前 24 KiB；实际采用原报告完整测试，不采用候选中的改动。]')}


def prepare_report_repair(directory, test_id, output_root, *, repository=None, expected_reference=None, control=None):
    """Freeze actual failing AFTER and the live candidate, without runtime probing."""
    control = control or RunControl()
    report, entry, original = _selected(directory, test_id, expected_reference, control)
    ready, reason = repair_eligibility(entry, report)
    if not ready:
        raise ValueError(reason)
    raw = _read(Path(report['report_dir']) / 'after.events.json')
    if raw['collected_tests'].count(test_id) != 1 or raw['started_tests'].count(test_id) != 1:
        raise ValueError('来源事件未唯一收集并启动这个完整测试 ID，不能直接创建修复案例。')
    state = read_project(Path(report['report_dir']) / 'evidence/project')
    if not state.get('created_at') or not report.get('created_at'):
        raise ValueError('来源记录缺少快照冻结或执行时间，请先取得完整来源记录。')
    root = repository_root(repository or state['repository'])
    output = Path(output_root).expanduser().resolve()
    if any(output.is_relative_to(path) or path.is_relative_to(output)
           for path in (root, Path(report['report_dir']).resolve())):
        raise ValueError('准备材料目录不能与被测仓库或来源报告重叠。')
    output.mkdir(parents=True, exist_ok=True)
    identity = 'repair-' + uuid.uuid4().hex[:12]
    final = output / identity
    with tempfile.TemporaryDirectory(prefix='.repair-prepare-', dir=output) as temp:
        stage = Path(temp) / identity
        inputs = stage / 'inputs'
        inputs.mkdir(parents=True)
        _copy_checked(Path(report['report_dir']) / 'evidence/project/inputs/after', inputs / 'before',
                      report['source_sha256']['after'], report['source_mode_sha256']['after'], control)
        control.notify('repair_candidate', {'notice': '正在冻结当前工作区；不会发现或执行测试。'})
        candidate = copy_workspace(root, inputs / 'after', control=control)
        frozen_at = _now()
        control.check()
        shutil.copyfile(original, inputs / 'regression.py')
        hashes = {'before': report['source_sha256']['after'], 'after': candidate['sha256']}
        modes = {side: source_mode_fingerprint(inputs / side) for side in ('before', 'after')}
        test_hash = _digest(inputs / 'regression.py')
        if test_hash != report['shared_tests'][parse_test_target(test_id)]:
            raise ValueError('复制时原测试已变化，请重开报告后重试。')
        change = _test_change(inputs / 'regression.py', inputs / 'after' / parse_test_target(test_id), parse_test_target(test_id))
        record = {'schema_version': 1, 'kind': 'project_repair_preparation', 'preparation_id': identity,
            'directory': str(final), 'created_at': frozen_at, 'title': state['title'] + ' · 目标修复',
            'issue': report.get('issue', ''), 'repository': str(root),
            'python': report['runtime']['executable'], 'test_target': test_id,
            'source_sha256': hashes, 'source_mode_sha256': modes, 'test_sha256': test_hash,
            'source_report': report_reference(report), 'observation': entry,
            'historical_execution': {'status': report['status'], 'after': {key: report['after'].get(key)
                for key in ('status', 'exit_code', 'worker_record_complete', 'timed_out', 'cancelled', 'collection_errors')}},
            'failure_snapshot': {'side': 'after', 'frozen_at': state['created_at'],
                                 'observed_at': report.get('created_at'), 'sha256': hashes['before']},
            'baseline_background': {'commit': state['before_commit'], 'sha256': report['source_sha256']['before'],
                                    'status': entry['before']['status'], 'notice': '原报告基线仅为背景，不是本次故障版。'},
            'candidate': {'repository': root.name, 'frozen_at': frozen_at, 'sha256': hashes['after']},
            'test_change': change, 'notice': '准备成功不代表修复通过；两版及完整测试已自带，正式执行仍须用户明确发起。'}
        _match_reference(_checked_report(directory, control=control), report_reference(report))
        save_json(stage / 'preparation.json', record)
        read_repair_preparation(stage)
        control.commit()
        stage.rename(final)
    return read_repair_preparation(final)


def read_repair_preparation(directory):
    try:
        return _read_repair_preparation(directory)
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError('无法读取完整修复准备，请检查准备目录及自带材料，或重新准备：' + str(exc)) from exc


def _read_repair_preparation(directory):
    candidate = Path(directory).expanduser()
    if candidate.is_symlink():
        raise ValueError('修复准备目录不能是符号链接。')
    folder = candidate.resolve()
    before_digest = _digest(folder / 'preparation.json')
    record = _read(folder / 'preparation.json')
    if (record.get('schema_version') != 1 or record.get('kind') != 'project_repair_preparation'
            or not isinstance(record.get('test_target'), str)):
        raise ValueError('请选择已保存的修复准备目录。')
    parse_test_target(record['test_target'])
    for side in ('before', 'after'):
        source = folder / 'inputs' / side
        if source.is_symlink() or not source.is_dir():
            raise ValueError('准备目录缺少普通源码快照：' + side)
        if (source_fingerprint(folder / 'inputs' / side)[1] != record['source_sha256'][side]
                or source_mode_fingerprint(folder / 'inputs' / side) != record['source_mode_sha256'][side]):
            raise ValueError('准备材料的源码或文件权限发生变化；请重新准备。')
    if ((folder / 'inputs/regression.py').is_symlink()
            or _digest(folder / 'inputs/regression.py') != record['test_sha256']):
        raise ValueError('准备材料的测试已变化；请重新准备。')
    actual_change = _test_change(folder / 'inputs/regression.py',
        folder / 'inputs/after' / parse_test_target(record['test_target']), parse_test_target(record['test_target']))
    if any(actual_change[key] != record['test_change'][key] for key in ('status', 'sha256')):
        raise ValueError('准备记录中的测试变化提示与固定候选不一致，请重新准备。')
    if _digest(folder / 'preparation.json') != before_digest:
        raise ValueError('读取期间准备记录发生变化，请重新打开。')
    # Reuse the case contract to check stored identities and historical phases.
    origin = _case_origin(record, before_digest, '待用户确认的问题', '待用户确认的行为',
                          record['observation']['after']['exception']['type'],
                          record['observation']['after']['exception']['message'])
    validate_project_failure_origin(origin, record['test_target'], {
        'type': origin['confirmation']['type'], 'message_contains': origin['confirmation']['message_contains']})
    return {**record, 'directory': str(folder), '_preparation_sha256': before_digest}


def _case_origin(plan, digest, problem, expected_behavior, failure_type, message_contains):
    origin = {key: plan[key] for key in ('preparation_id', 'source_report', 'observation', 'historical_execution', 'failure_snapshot',
        'baseline_background', 'candidate', 'test_change', 'source_sha256', 'source_mode_sha256', 'test_sha256', 'test_target')}
    origin.update(schema_version=1, kind='project_report_failure', preparation_sha256=digest,
        confirmation={'confirmed': True, 'problem': problem.strip(), 'expected_behavior': expected_behavior.strip(),
                      'type': failure_type, 'message_contains': message_contains})
    return origin


def validate_repair_confirmation(plan, *, problem, expected_behavior, failure_type, message_contains):
    """Validate the same confirmation fields in desktop and CLI save paths."""
    for value, label in ((problem, '问题描述'), (expected_behavior, '期望行为')):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('请填写' + label + '，再确认保存。')
        if len(value.encode('utf-8')) > 8000:
            raise ValueError(label + '超过 8000 字节，请精简后再确认。')
    observed = plan['observation']['after']['exception']
    if failure_type != observed['type']:
        raise ValueError('异常类型须与历史实际记录一致，请重新核对。')
    if not isinstance(message_contains, str) or not message_contains.strip():
        raise ValueError('请选择稳定非空的异常消息片段。')
    if message_contains not in observed['message']:
        raise ValueError('消息片段须来自上方历史实际异常，请重新选择。')


def save_report_repair(directory, case_path, *, problem, expected_behavior, failure_type,
                       message_contains, confirmed=False, expected_preparation=None, control=None):
    """Publish a retryable portable case from saved preparation, never execute it."""
    control = control or RunControl()
    control.check()
    plan = read_repair_preparation(directory)
    if expected_preparation is not None and plan['_preparation_sha256'] != expected_preparation:
        raise ValueError('已审阅的准备记录发生变化，请重新打开并确认。')
    if confirmed is not True:
        raise ValueError('请确认问题、期望行为、实际异常类型和稳定非空消息片段；片段须来自历史实际异常。')
    validate_repair_confirmation(plan, problem=problem, expected_behavior=expected_behavior,
                                 failure_type=failure_type, message_contains=message_contains)
    candidate_path = Path(case_path).expanduser()
    if candidate_path.is_symlink() or candidate_path.exists():
        raise ValueError('案例文件已经存在，未覆盖；请选择新文件。')
    path = candidate_path.resolve()
    source = Path(plan['directory'])
    if any(path.is_relative_to(Path(root).resolve()) for root in
           (source, plan['repository'], plan['source_report']['report_dir'])):
        raise ValueError('正式案例须保存在准备目录、被测仓库及来源报告之外，原材料保留以便重试。')
    path.parent.mkdir(parents=True, exist_ok=True)
    origin = _case_origin(plan, plan['_preparation_sha256'], problem, expected_behavior, failure_type, message_contains)
    config = {'source': 'snapshot', 'check_mode': 'repair', 'title': plan['title'],
              'description': problem.strip() + '\n期望行为：' + expected_behavior.strip() +
                '\n故障版来自原报告真正失败的变更后快照；候选版为准备时冻结的当前工作区；使用原报告完整测试。'
                + '\n来源运行：' + plan['source_report']['run_id'] + '\n故障版冻结：' + plan['failure_snapshot']['frozen_at']
                + '\n候选版冻结：' + plan['candidate']['frozen_at'],
              'test_target': plan['test_target'], 'python': plan['python'],
              'expected_failure': {'type': failure_type, 'message_contains': message_contains},
              'project_failure_origin': origin}
    # Publishing moves caller-owned inputs; copy so any failed save remains retryable.
    with tempfile.TemporaryDirectory(prefix='.repair-save-', dir=path.parent) as temp:
        inputs = Path(temp) / 'inputs'
        inputs.mkdir()
        for side in ('before', 'after'):
            _copy_checked(source / 'inputs' / side, inputs / side, plan['source_sha256'][side],
                          plan['source_mode_sha256'][side], control)
        shutil.copyfile(source / 'inputs/regression.py', inputs / 'regression.py')
        if _digest(inputs / 'regression.py') != plan['test_sha256']:
            raise ValueError('复制时测试发生变化；未保存。')
        if _digest(source / 'preparation.json') != plan['_preparation_sha256']:
            raise ValueError('保存期间确认来源发生变化；未保存。')
        control.commit()
        publish_snapshot_case(path, inputs, config)
    return {'case': str(path), 'preparation': str(source), 'test_target': plan['test_target'],
            'notice': '正式案例及自带快照、完整测试、来源和确认条件已保存；尚未执行。'}
