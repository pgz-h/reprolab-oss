"""Plain-language next steps derived only from recorded outcomes and phases."""


def entry_action(entry, report):
    """Choose one useful next action; preserve secondary repair and evidence tools."""
    if not entry:
        return ('details', '查看结果详情', '请选择一个结果行。')
    for side in ('after', 'before'):
        outcome, version = entry.get(side) or {}, report.get(side) or {}
        if version.get('timed_out') or version.get('cancelled') or version.get('worker_record_complete') is False:
            return ('logs', '查看中断位置与日志', '先核对最后阶段与执行日志，再决定调整时限或重新检查。')
        if outcome.get('status') == 'passed':
            continue
        error = outcome.get('exception') or {}
        if error.get('type') in ('ModuleNotFoundError', 'ImportError'):
            return ('environment', '检查项目环境', '先核对所选 Python、插件与导入配置，再复查当前修改。')
        if version.get('collection_errors'):
            return ('logs', '查看测试收集日志', '先处理测试收集问题，再核对完整测试范围。')
        if any(event.get('phase') in ('setup', 'teardown') and event.get('outcome') == 'failed'
               for event in outcome.get('phases') or []):
            return ('logs', '查看准备／清理日志', '先处理 fixture 或共享资源异常，再继续业务修复验收。')
    if entry.get('status') in ('regression_detected', 'baseline_failure'):
        return ('repair', '准备修复说明', '根据保存的测试与实际异常，整理本地可编辑的修复说明。')
    if entry.get('status') in ('regression_passed', 'improved'):
        return ('test', '查看本次测试内容', '核对断言覆盖范围；本次结论仅覆盖实际选择的测试。')
    return ('details', '查看异常与阶段', '查看具体阶段、跳过原因与所需条件，再决定下一步。')


def entry_guidance(entry, report):
    status = entry.get('status')
    hints = []
    if status == 'regression_detected':
        hints.append('此项在基线通过、变更后失败。先比较变更后的异常与代码差异，再决定修复或调整测试预期。')
    elif status == 'baseline_failure':
        hints.append('两版都失败。先核对原有问题和测试预期；这一项不能证明本次修改引入了新故障。')
    elif status == 'improved':
        hints.append('此项从失败转为通过。若要正式验收一个目标修复，还需在案例验收中预先声明旧版故障并重新执行。')
    elif status == 'regression_passed':
        hints.append('两版均通过，结论限于这一测试覆盖的场景。可以检查实际测试代码确认断言范围。')
    for side, name in (('before', '基线'), ('after', '变更后')):
        outcome = entry.get(side) or {}
        version = report.get(side) or {}
        phases = outcome.get('phases') or []
        if 'observed_status' in outcome:
            observed = {'passed': '通过', 'failed': '失败', 'skipped': '跳过', 'not_run': '尚无阶段记录'}.get(outcome['observed_status'], '阶段未完整结束')
            hints.append(name + '中断前观察：' + observed + '；保留实际阶段，本版未完整结束，不计为正式通过。')
        if version.get('timed_out'):
            hints.append(name + '超时：核对是否存在等待或长任务；限时按每版全部所选测试计算，可调整后重跑。')
        elif version.get('cancelled') or version.get('status') == 'not_run':
            hints.append(name + '没有完整运行结果，请在准备好后明确重跑，不能据此计为通过。')
        if any(phase.get('xfail') for phase in phases if isinstance(phase, dict)):
            hints.append(name + '带有 xfail 标记，不作为本工具的通过证据；请核对标记与测试预期。')
        elif outcome.get('status') == 'skipped':
            hints.append(name + '被跳过：核对跳过条件与所需服务，具备条件后重新运行。')
        failed = [phase.get('phase') for phase in phases
                  if isinstance(phase, dict) and phase.get('outcome') == 'failed']
        if 'setup' in failed or 'teardown' in failed:
            hints.append(name + '的 fixture 准备或清理失败；先处理相关阶段，不能把它当作测试主体已通过。')
        error = outcome.get('exception') or {}
        if isinstance(error, dict) and error.get('type') in ('ModuleNotFoundError', 'ImportError'):
            hints.append(name + '导入失败：核对缺少的模块、所选 Python 环境与项目导入配置。')
        if version.get('collection_errors'):
            hints.append(name + '存在测试收集错误，请在两版日志中查看收集阶段的具体消息。')
        elif outcome.get('status') == 'inconclusive' and version.get('worker_record_complete') is False:
            hints.append(name + '的工作进程记录不完整，请查看完整日志和解释器状态，不能推断测试未执行。')
    if not hints:
        hints.append('现有记录不足以判定，请先查看异常、阶段和两版日志，再选择重跑。')
    return list(dict.fromkeys(hints))


# Groups describe recorded symptoms, never a proven shared root cause.
_ATTENTION = {'regression_detected', 'baseline_failure', 'inconclusive'}
_SIDE = {'before': '基线', 'after': '变更后', 'unknown': '版本未记录'}
_PHASE = {'setup': '测试准备', 'call': '测试主体', 'teardown': '测试清理',
          'collection': '测试收集', 'process': '整版执行', 'mark': '测试标记', 'unknown': '阶段未记录'}
_KINDS = {
    'import': ('导入异常', 'environment', '核对所选 Python、插件和项目导入配置；不要仅凭模块名猜测应安装的包。'),
    'collection': ('收集问题', 'logs', '查看这版的收集日志；先处理收集阻碍，再核对完整测试范围。'),
    'setup': ('准备阶段失败', 'logs', '先查看 fixture 或准备代码的异常；这些记录不能作为测试主体的修复验收。'),
    'teardown': ('清理阶段失败', 'logs', '检查 fixture 清理及共享资源状态；测试主体通过不能替代清理完成。'),
    'test_failure': ('测试主体失败', 'representative', '先定位组内代表项并比较两版结果；相同异常也可能来自不同问题。'),
    'timeout': ('整版超时', 'logs', '时限覆盖本版本的全部所选测试。查看最后日志和慢操作，再决定调整时限或范围。'),
    'cancelled': ('运行已取消', 'logs', '已完成记录仍保留，未完成部分不能算通过；准备好后明确复查。'),
    'incomplete': ('执行记录不完整', 'logs', '查看完整日志与进程状态；缺少记录不代表没有执行，也不能推断通过。'),
    'not_run': ('未执行或未记录', 'logs', '核对本版的执行状态和所选范围，不能把未记录项视为通过。'),
    'skipped': ('测试被跳过', 'representative', '查看实际跳过原因及所需条件，具备条件后再明确复查。'),
    'xfail': ('预期失败标记', 'representative', '核对 xfail 标记与测试预期；这类记录不作为本工具的通过证据。'),
    'unknown': ('需要进一步核对', 'logs', '现有记录不能确定具体失败阶段，请先查看逐项结果和两版日志。'),
}
DIAGNOSTIC_NOTICE = ('按记录中的版本、阶段、异常类型和保存的消息归类，不证明共同根因。'
                     '同一测试可以属于多个组；原始结果与证据不变。')


def group_project_failures(report):
    """Pure interpretation of saved report data; no I/O, model request or execution."""
    from collections import Counter
    import hashlib
    import json
    entries = [item for item in (report.get('entries') or []) if isinstance(item, dict) and item.get('test_id')]
    known = {item['test_id'] for item in entries}
    for identity in (report.get('selected_tests') or []):
        if identity not in known:
            entries.append({'test_id': identity, 'status': 'inconclusive', 'before': {}, 'after': {}})
            known.add(identity)
    attention = [entry for entry in entries if entry.get('status') in _ATTENTION]
    entry_statuses = {entry['test_id']: entry['status'] for entry in attention}
    groups = {}

    def add(kind, side, phase, message, error_type='', identity=None, *, scope='test'):
        # Missing exception details are insufficient to group independent assertions.
        discriminator = identity if (kind in ('test_failure', 'setup', 'teardown', 'import') and not (error_type and message)
                                     or phase == 'unknown' and error_type) else None
        key = (kind, side, phase, error_type, message, scope, discriminator)
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
        if digest not in groups:
            title, action, advice = _KINDS[kind]
            groups[digest] = {'id': digest, 'category': kind, 'title': title, 'side': side,
                'side_label': _SIDE[side], 'phase': phase, 'phase_label': _PHASE.get(phase, phase),
                'exception_type': error_type, 'message': message, 'scope': scope,
                'action': action, 'advice': advice, 'test_ids': [], 'observations': 0}
        group = groups[digest]
        if identity is not None:
            group['observations'] += 1
            if identity not in group['test_ids']:
                group['test_ids'].append(identity)
        return group

    version_groups = {}
    for side in ('before', 'after'):
        version = report.get(side) or {}
        signals = []
        errors = version.get('collection_errors') or []
        if errors and version.get('worker_record_complete') is not False:
            signals.append(('collection', 'collection', '\n\n'.join(str(error) for error in errors)))
        if version.get('timed_out'):
            signals.append(('timeout', 'process', '本版本超过所选测试共用的总时限。'))
        elif version.get('cancelled') or version.get('status') == 'cancelled':
            signals.append(('cancelled', 'process', '本版本执行被取消。'))
        elif version.get('status') == 'not_run':
            signals.append(('not_run', 'process', '本版本没有开始执行。'))
        elif version.get('worker_record_complete') is False:
            signals.append(('incomplete', 'process', '工作进程没有返回完整执行记录。'))
        version_groups[side] = [add(kind, side, phase, message, scope='version') for kind, phase, message in signals]

    for entry in attention:
        identity, associated = entry['test_id'], False
        for side in ('after', 'before'):
            outcome = entry.get(side) or {}
            if outcome.get('status') == 'passed':
                continue
            related = version_groups[side]
            for group in related:
                if identity not in group['test_ids']:
                    group['test_ids'].append(identity)
                group['observations'] += 1
                associated = True
            phases = [phase for phase in (outcome.get('phases') or []) if isinstance(phase, dict)]
            if any(phase.get('xfail') for phase in phases):
                add('xfail', side, 'mark', '实际记录含 xfail 标记。', identity=identity)
                associated = True
                continue
            failures = [phase for phase in phases if phase.get('outcome') in ('failed', 'skipped')]
            if not failures and outcome.get('status') == 'failed':
                failures = [{'phase': 'unknown', 'outcome': 'failed', 'exception': outcome.get('exception')}]
            for observed in failures:
                phase = observed.get('phase') or 'unknown'
                error = observed.get('exception') or {}
                error_type, message = str(error.get('type') or ''), str(error.get('message') or '')
                kind = ('skipped' if observed.get('outcome') == 'skipped' else
                        'import' if error_type in ('ModuleNotFoundError', 'ImportError') else
                        phase if phase in ('setup', 'teardown') else 'test_failure' if phase == 'call' else 'unknown')
                add(kind, side, phase, message, error_type, identity)
                associated = True
            if not failures and not related and outcome.get('status') != 'passed':
                kind = 'skipped' if outcome.get('status') == 'skipped' else 'not_run' if outcome.get('status') == 'not_run' else 'unknown'
                add(kind, side, 'unknown', '没有记录更具体的阶段或异常。', identity=identity)
                associated = True
        if not associated:
            add('unknown', 'unknown', 'unknown', '逐项状态需要核对，现有阶段记录不足。', identity=identity)
    result = list(groups.values())
    for group in result:
        group['count'] = len(group['test_ids'])
        group['status_counts'] = dict(Counter(entry_statuses[identity] for identity in group['test_ids']))
        group['label'] = f"{group['side_label']} · {group['phase_label']} · {group['title']} · {group['count']} 项"
        if group['exception_type']:
            group['label'] += ' · ' + group['exception_type']
        group['basis'] = ('整版运行记录；所列测试没有本版通过结果。不能据此断定每项触发了同一异常。'
                          if group['scope'] == 'version' else '相同版本、阶段及已记录异常；未记录的异常不会被猜测补齐。')
    priority = {'import': 0, 'collection': 0, 'setup': 1, 'teardown': 1, 'test_failure': 2,
                'timeout': 1, 'incomplete': 1, 'cancelled': 3, 'not_run': 3, 'skipped': 4, 'xfail': 4, 'unknown': 5}
    result.sort(key=lambda group: (group['side'] != 'after', priority[group['category']], -group['count'], group['id']))
    return {'schema_version': 1, 'kind': 'project_diagnostics', 'run_id': report.get('run_id'),
            'project_id': report.get('project_id'), 'recorded_status': report.get('status'),
            'attention_test_count': len(attention),
            'group_count': len(result), 'groups': result, 'notice': DIAGNOSTIC_NOTICE}


def format_diagnostic_group(report, group):
    """Local, bounded copy/export text; full source evidence remains separate."""
    labels = {'regression_detected': '新增失败', 'baseline_failure': '原有失败', 'inconclusive': '受阻／未定'}
    lines = ['ReproLab 失败现象', '来源运行：' + str(report.get('run_id') or '未知'), group['label'],
             '组内结果：' + ' · '.join(f'{labels.get(key, key)} {count}' for key, count in group['status_counts'].items()),
             '归类依据：' + group['basis'], '建议：' + group['advice'], '',
             '实际异常：' + group['exception_type'], group['message'] or '未记录具体消息。', '',
             '受影响的完整测试 ID：', *(group['test_ids'] or ['未关联到某个已选测试，请查看整版日志。']), '', DIAGNOSTIC_NOTICE]
    text = '\n'.join(lines)
    return text if len(text) <= 200000 else text[:200000] + '\n…摘要已截断，完整范围请导出诊断 JSON。'


def diagnose_project_report(directory):
    from .project_history import _checked_report
    report = _checked_report(directory)
    return {**group_project_failures(report), 'report_dir': report['report_dir'],
            'source_report_sha256': report['_report_sha256']}
