"""Read an explicit report ancestry and retain omitted tests as historical observations."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from . import project_history as history
from .persistence import atomic_write
from .runner import RunControl

MAX_ROUNDS = 50
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_TESTS = 10000
MAX_TEST_ID_BYTES = 8 * 1024 * 1024
MAX_EXPORT_BYTES = 32 * 1024 * 1024
MAX_NOTE_CHARS = 4000
STATUS_LABELS = {'regression_passed': '两版通过', 'improved': '已改善', 'regression_detected': '新增失败',
                 'baseline_failure': '两版均失败', 'inconclusive': '受阻', 'attention': '需要处理', 'cancelled': '已取消'}
NOTICE = ('沿报告保存的复查来源排列，不按标题或时间猜测关系；同会话的其他运行和并行分支不自动并入。'
          '本轮未复查项只保留最近观察，不能据此认定当前代码已通过或仍失败。')


class _HistoryReadLimit(ValueError):
    pass


def _key(reference):
    history.validate_report_reference(reference)
    return tuple(reference[key] for key in ('project_id', 'run_id', 'report_sha256'))


def _note_path(root, reference):
    history.validate_report_reference(reference)
    return Path(root).expanduser().resolve() / (reference['report_sha256'] + '.json')


def read_run_note(reference, root):
    if root is None:
        return {'text': '', 'sha256': None, 'updated_at': None}
    path = _note_path(root, reference)
    if not path.exists() and not path.is_symlink():
        return {'text': '', 'sha256': None, 'updated_at': None}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768:
        raise ValueError('本机处理说明文件无效或过大，请先核对该文件。')
    raw = path.read_bytes()
    if len(raw) > 32768:
        raise ValueError('本机处理说明超过大小限制。')
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("本机处理说明格式无效，原文件保留。") from exc
    if (not isinstance(value, dict) or value.get('kind') != 'project_run_note' or value.get('schema_version') != 1
            or _key(value.get('reference')) != _key(reference) or not isinstance(value.get('text'), str)
            or len(value['text']) > MAX_NOTE_CHARS):
        raise ValueError('本机处理说明与这份报告身份不一致，原文件保留。')
    return {'text': value['text'], 'sha256': hashlib.sha256(raw).hexdigest(), 'updated_at': value.get('updated_at')}


def save_run_note(reference, text, root, *, expected_note_sha256=None, control=None):
    if not isinstance(text, str) or len(text) > MAX_NOTE_CHARS:
        raise ValueError('每轮处理说明最多 4000 个字符。')
    control = control or RunControl()
    control.check()
    history.validate_report_reference(reference)
    report = history._checked_report(reference['report_dir'], control=control)
    history._match_reference(report, reference)
    path = _note_path(root, reference)
    if path.is_relative_to(Path(report['report_dir'])) or path.is_relative_to(Path(report['repository']).expanduser().resolve()):
        raise ValueError('本机处理说明须保存在报告和原项目之外。')
    previous = read_run_note(reference, root)
    if previous['sha256'] != expected_note_sha256:
        raise ValueError('处理说明已在其他窗口变化，请重新载入后再保存；当前草稿保留。')
    value = {'schema_version': 1, 'kind': 'project_run_note', 'reference': history.report_reference(report),
             'text': text, 'updated_at': datetime.now(timezone.utc).isoformat(),
             'notice': '用户填写的本机处理说明，不参与执行或验收结论。'}
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    if len(payload) > 32768:
        raise ValueError('处理说明记录超过大小限制，草稿保留。')
    path.parent.mkdir(parents=True, exist_ok=True)
    control.commit()
    atomic_write(path, payload)
    return read_run_note(reference, root)


def _file_changes(before, after):
    prefix = 'evidence/project/inputs/after/'
    def files(report):
        hashes = (report.get('persistence') or {}).get('sha256')
        if not isinstance(hashes, dict):
            return None
        return {name[len(prefix):]: digest for name, digest in hashes.items() if name.startswith(prefix)}
    left, right = files(before), files(after)
    if not left or not right:
        return {'available': False, 'count': 0, 'files': [], 'truncated': False}
    modes_left = (before.get('persistence') or {}).get('file_modes') or {}
    modes_right = (after.get('persistence') or {}).get('file_modes') or {}
    changes = []
    for name in sorted(left.keys() | right.keys()):
        mode_changed = (prefix + name in modes_left and prefix + name in modes_right
                        and modes_left[prefix + name] != modes_right[prefix + name])
        if left.get(name) != right.get(name) or mode_changed:
            changes.append({'path': name, 'kind': 'added' if name not in left else 'removed' if name not in right else 'modified'})
    return {'available': True, 'count': len(changes), 'files': changes[:200], 'truncated': len(changes) > 200,
            'notice': '比较两轮候选快照保存的文件内容和可用权限记录，不推断修改原因。'}


def _round(report, notes_root):
    reference = history.report_reference(report)
    try:
        note = read_run_note(reference, notes_root)
    except (OSError, ValueError, TypeError) as exc:
        note = {'text': '', 'sha256': None, 'updated_at': None, 'error': str(exc)}
    return {'reference': reference, 'title': report.get('title'), 'created_at': report.get('created_at'),
            'status': report.get('status'), 'selected_count': len(report['selected_tests']),
            'counts': dict(Counter(entry['status'] for entry in report['entries'])),
            'scope_mode': (report.get('review_source') or {}).get('selection_mode'),
            'comparison': None, 'candidate_changes': None, 'note': note,
            'repository': report.get('repository')}


def _comparison_summary(comparison):
    return {key: comparison[key] for key in ('scope', 'counts', 'state_counts', 'execution_counts', 'same_check_resolved', 'summary', 'notices')}


def read_processing_history(directory, *, locations=None, notes_root=None, expected_report=None, max_rounds=20, control=None):
    if type(max_rounds) is not int or not 1 <= max_rounds <= MAX_ROUNDS:
        raise ValueError('每次可读取 1–50 轮已记录来源。')
    locations = {} if locations is None else locations
    if (not isinstance(locations, dict) or len(locations) > 200
            or any(not history._hash(key) or not isinstance(value, str) or not value for key, value in locations.items())):
        raise ValueError('报告定位信息无效。')
    control = control or RunControl()
    consumed = 0
    def load(location):
        nonlocal consumed
        control.check()
        path = Path(location).expanduser()
        if path.is_dir():
            path = path / 'report.json'
        size = path.stat().st_size
        if consumed + size > MAX_REPORT_BYTES:
            raise _HistoryReadLimit('本次来源链的报告数据超过 64 MiB，已停止继续读取。')
        report = history._checked_report(location, control=control)
        actual_size = (Path(report['report_dir']) / 'report.json').stat().st_size
        if consumed + actual_size > MAX_REPORT_BYTES:
            raise _HistoryReadLimit('来源链的报告数据超过 64 MiB，已停止继续读取。')
        consumed += actual_size
        return report

    latest = load(directory)
    if expected_report is not None:
        history._match_reference(latest, expected_report)
    current_reference = history.report_reference(latest)
    current_ids = set(latest['selected_tests'])
    unresolved_previous = set(current_ids)
    rounds = []
    tests = {}
    seen = set()
    identities = set()
    gap = None

    def add_report(report):
        index = len(rounds)
        rounds.append(_round(report, notes_root))
        reference = history.report_reference(report)
        seen.add(_key(reference))
        identities.add((reference['project_id'], reference['run_id']))
        for entry in report['entries']:
            identity = entry['test_id']
            if identity not in tests:
                error = (entry.get('after') or {}).get('exception') or (entry.get('before') or {}).get('exception')
                tests[identity] = {'test_id': identity, 'selected_now': identity in current_ids,
                    'latest_observation': {'round_index': index, 'status': entry['status'], 'execution': history._execution(entry),
                        'exception': {'type': error.get('type'), 'message': str(error.get('message') or '')[:1000]} if isinstance(error, dict) else None},
                    'observations': [], 'comparison': None}
            tests[identity]['observations'].append({'round_index': index, 'status': entry['status'], 'execution': history._execution(entry)})
        control.notify('history_round', {'rounds': len(rounds), 'message': f'已核对 {len(rounds)} 份报告及执行证据。'})

    add_report(latest)
    child = latest
    while child.get('review_source') is not None:
        control.check()
        reference = child['review_source']
        try:
            history.validate_report_reference(reference)
        except ValueError as exc:
            gap = {'reason': 'reference', 'reference': None, 'message': str(exc)}
            break
        reason = None
        if _key(reference) in seen or (reference['project_id'], reference['run_id']) in identities:
            reason, message = 'cycle', '来源引用重复或形成循环，已停止追溯。'
        elif len(rounds) >= max_rounds:
            reason, message = 'limit', f'已达到本次 {max_rounds} 轮上限，更早来源尚未读取。'
        if reason:
            gap = {'reason': reason, 'reference': dict(reference), 'message': message}
            break
        location = history.source_report_directory(child, reference, locations.get(reference['report_sha256']))
        try:
            parent = load(location)
            history._match_reference(parent, reference)
        except _HistoryReadLimit as exc:
            gap = {'reason': 'data_limit', 'reference': dict(reference), 'message': str(exc)}
            break
        except (OSError, ValueError) as exc:
            missing = not Path(location).expanduser().exists()
            message = '来源报告本次未找到，请重新定位。原位置：' + str(location) if missing else str(exc)
            gap = {'reason': 'missing' if missing else 'unavailable',
                   'reference': dict(reference), 'message': message}
            break
        combined = set(tests) | set(parent['selected_tests'])
        if len(combined) > MAX_TESTS or sum(len(identity.encode('utf-8')) for identity in combined) > MAX_TEST_ID_BYTES:
            gap = {'reason': 'scope_limit', 'reference': dict(reference), 'message': '历史测试范围超过本次展示上限，更早来源未并入。'}
            break
        comparison = history._compare_checked_reports(parent, child)
        rounds[-1]['comparison'] = _comparison_summary(comparison)
        rounds[-1]['candidate_changes'] = _file_changes(parent, child)
        matched = unresolved_previous & set(parent['selected_tests'])
        if matched:
            previous = comparison if child is latest else history._compare_checked_reports(parent, latest, test_ids=matched)
            for row in previous['entries']:
                if row['test_id'] not in matched:
                    continue
                tests[row['test_id']]['comparison'] = {
                    'previous_round_index': len(rounds), 'change': row['change'], 'state_change': row['state_change'],
                    'conditions': row['conditions'], 'before_execution': row['before_execution'],
                    'test_file': {key: row['test_file'][key] for key in ('status', 'before_sha256', 'after_sha256')}}
            unresolved_previous -= matched
        add_report(parent)
        child = parent

    count = len(rounds)
    rounds.reverse()
    for index, item in enumerate(rounds, 1):
        item['number'] = index
        item['is_current'] = index == count
    for row in tests.values():
        row['latest_observation']['round'] = count - row['latest_observation'].pop('round_index')
        for observation in row['observations']:
            observation['round'] = count - observation.pop('round_index')
        row['observations'].reverse()
        if row['comparison']:
            row['comparison']['previous_round'] = count - row['comparison'].pop('previous_round_index')
    entries = sorted(tests.values(), key=lambda row: (row['selected_now'], row['test_id']))
    not_rechecked = [row for row in entries if not row['selected_now']]
    latest_counts = dict(Counter(history._execution(entry) for entry in latest['entries']))
    summary = {'rounds': count, 'unique_tests': len(entries), 'selected_now': len(current_ids),
               'current_execution_counts': latest_counts, 'not_rechecked': len(not_rechecked),
               'not_rechecked_last_attention': sum(row['latest_observation']['execution'] != 'passed' for row in not_rechecked)}
    result = {'schema_version': 1, 'kind': 'project_processing_history', 'status': 'partial' if gap else 'complete',
              'checked_at': datetime.now(timezone.utc).isoformat(), 'current': current_reference,
              'rounds': rounds, 'tests': entries, 'summary': summary, 'source_gap': gap, 'max_rounds': max_rounds,
              'notice': NOTICE, 'note_notice': '处理说明是另存的本机文字，不参与测试结论；导出此记录时会包含这些说明。'}
    result['summary_text'] = format_processing_history(result)
    return result


def format_processing_history(value):
    summary = value['summary']
    counts = summary['current_execution_counts']
    lines = ['ReproLab 多轮处理记录', '当前运行：' + value['current']['run_id'],
             f"已核对 {summary['rounds']} 轮已记录来源 · 历史出现过 {summary['unique_tests']} 项测试",
             f"本轮选择 {summary['selected_now']} 项：通过 {counts.get('passed', 0)} · 失败 {counts.get('failed', 0)} · 受阻 {counts.get('blocked', 0)} · 未完整执行 {counts.get('not_run', 0)}",
             f"本轮未复查 {summary['not_rechecked']} 项，其中最近一次需处理 {summary['not_rechecked_last_attention']} 项。"]
    if value['source_gap']:
        lines[-1] = '已核对范围内：' + lines[-1] + ' 更早的测试范围尚未读取。'
        lines.append('来源链未完整读取：' + value['source_gap']['message'] + ' 以上仅覆盖已核对区间。')
    for item in value['rounds']:
        lines.extend(['', f"第 {item['number']} 轮{'（当前）' if item['is_current'] else ''} · {item['reference']['run_id']}",
                      f"选择 {item['selected_count']} 项 · {STATUS_LABELS.get(item['status'], item['status'] or '未知')}"])
        if item['comparison']:
            lines.append(item['comparison']['summary'])
        if item['note'].get('text'):
            lines.append('本机处理说明：' + item['note']['text'])
        if item['note'].get('error'):
            lines.append('说明读取问题：' + item['note']['error'])
    omitted = [row for row in value['tests'] if not row['selected_now']]
    if omitted:
        lines.extend(['', '本轮未复查项（最多列出前 30 项）：'])
        for row in omitted[:30]:
            last = row['latest_observation']
            lines.append(f"{row['test_id']} · 最近第 {last['round']} 轮：{STATUS_LABELS.get(last['status'], last['status'])}")
    lines.extend(['', value['notice'], value['note_notice']])
    text = '\n'.join(lines)
    return text if len(text) <= 200000 else text[:200000] + '\n…摘要已截断；完整记录可导出 JSON。'


def export_processing_history(value, destination):
    target = Path(destination).expanduser().resolve()
    if target.exists() or any(target.is_relative_to(Path(item['reference']['report_dir']).resolve()) for item in value['rounds']):
        raise ValueError('请选择所有来源报告目录之外的新文件保存处理记录。')
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    if len(payload) > MAX_EXPORT_BYTES:
        raise ValueError('处理记录超过 32 MiB 导出限制，请减少读取轮数。')
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, payload)
    return {'status': 'exported', 'output': str(target), 'bytes': len(payload)}
