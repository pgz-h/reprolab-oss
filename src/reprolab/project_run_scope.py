"""Describe a pytest run's saved discovery, recommendations, and observed execution."""

OMITTED_LIMIT = 20

_DISCOVERY_LABELS = {
    'complete': '完整', 'partial': '部分发现', 'truncated': '已截断',
    'partial_truncated': '部分发现且已截断', 'unknown': '完整性未知',
}
_RUN_LABELS = {
    'completed': '已结束', 'cancelled': '已取消', 'timed_out': '已超时',
    'not_run': '未执行', 'collection_failed': '收集失败', 'worker_error': '执行记录不完整',
    'unknown': '状态未知',
}


def _execution(version, selected):
    status = version.get('status', 'unknown')
    if status not in _RUN_LABELS:
        status = 'unknown'
    progress = version.get('progress') or {}
    if status == 'not_run':
        started = finished = 0
        accuracy = 'exact'
    else:
        started, finished = progress.get('started'), progress.get('completed')
        if (progress.get('status') == 'recorded' and type(started) is int and type(finished) is int
                and 0 <= finished <= started <= len(selected)):
            accuracy = ('exact' if progress.get('session_finished') is True and progress.get('tail_ignored') is False
                        else 'at_least')
        else:
            # Older reports may have phase observations without a progress journal.
            tests = version.get('tests') or {}
            phases = [(tests.get(test) or {}).get('phases') or () for test in selected]
            started = sum(bool(events) for events in phases) or None
            finished = sum(any(event.get('phase') == 'teardown' for event in events) for events in phases) or None
            accuracy = 'at_least' if started is not None else 'unknown'
    return {'status': status, 'accuracy': accuracy, 'started': started, 'finished': finished,
            'not_started': len(selected) - started if accuracy == 'exact' else None}


def _changed_test_scope(collection, selected):
    definitions = collection.get('changed_test_definitions')
    if not isinstance(definitions, dict) or not definitions.keys() <= set(collection['tests']):
        return None
    added = 0
    for identity, row in definitions.items():
        if (not isinstance(row, dict) or row.get('status') not in ('added', 'modified')
                or row.get('path') != identity.partition('::')[0]
                or type(row.get('line')) is not int or row['line'] < 1):
            return None
        added += row['status'] == 'added'
    return {'added': added, 'modified': len(definitions) - added,
            'selected': len(definitions.keys() & selected), 'omitted': len(definitions.keys() - selected)}


def changed_test_scope_text(scope, language):
    counts = scope.get('changed_test_ids')
    if not counts or not (counts['added'] or counts['modified']):
        return ''
    if language == 'zh':
        return (f'发现的测试 ID 中，关联到新增定义 {counts["added"]} 项、修改定义 {counts["modified"]} 项；'
                f'纳入 {counts["selected"]} 项，未纳入 {counts["omitted"]} 项')
    return (f'Discovered test IDs linked to added definitions: {counts["added"]}; '
            f'modified definitions: {counts["modified"]}. Selected {counts["selected"]}, omitted {counts["omitted"]}.')


def pytest_run_scope(report, *, collection=None, include_omitted=False):
    """Use the saved recommendations as observed hints, without recomputing them."""
    selected = set(report.get('selected_tests', ()))
    discovery = {'status': 'unknown', 'count': None, 'accuracy': 'unknown'}
    suggested = {'status': 'unknown', 'count': None, 'selected_count': None, 'omitted_count': None}
    definition_scope = None
    if collection is not None:
        definition_scope = _changed_test_scope(collection, selected)
        tests = set(collection['tests'])
        partial, truncated = collection.get('partial'), collection.get('truncated')
        if partial is True:
            status = 'partial_truncated' if truncated is True else 'partial'
        elif truncated is True:
            status = 'truncated'
        elif partial is False and truncated is False and collection.get('count') == len(tests):
            status = 'complete'
        else:
            status = 'unknown'
        discovery.update(status=status, count=len(tests), accuracy='exact' if status == 'complete' else 'at_least')
        if path := collection.get('collection_path'):
            discovery['path'] = path
        recommendations = collection.get('recommendations')
        if (isinstance(recommendations, dict) and recommendations.keys() <= tests
                and all(isinstance(reason, str) and reason.strip() for reason in recommendations.values())):
            omitted = recommendations.keys() - selected
            suggested.update(status='recorded', count=len(recommendations), omitted_count=len(omitted),
                             selected_count=len(recommendations) - len(omitted))
            if include_omitted and omitted:
                suggested['omitted_tests'] = [
                    {'test_id': identity, 'reason': recommendations[identity], 'outcome': 'not_observed'}
                    for identity in sorted(omitted)[:OMITTED_LIMIT]
                ]
    return {
        **({'changed_test_ids': definition_scope} if definition_scope is not None else {}),
        'discovery': discovery, 'recommendations': suggested, 'selected_count': len(selected),
        'execution': {side: _execution(report.get(side) or {}, selected) for side in ('before', 'after')},
    }


def public_run_scope(scope):
    return {
        **({'changed_test_ids': scope['changed_test_ids']} if 'changed_test_ids' in scope else {}),
        'discovery': {key: scope['discovery'][key] for key in ('status', 'count', 'accuracy')},
        'recommendations': {key: scope['recommendations'][key]
                            for key in ('status', 'count', 'selected_count', 'omitted_count')},
        'selected_count': scope['selected_count'], 'execution': scope['execution'],
    }


def run_scope_lines(scope):
    """Compact Chinese wording shared by the desktop and HTML report."""
    def total(count, accuracy):
        return '未知' if count is None else f'至少 {count} 项' if accuracy == 'at_least' else f'{count} 项'

    discovery = scope['discovery']
    recommended = scope['recommendations']
    found = total(discovery['count'], discovery['accuracy'])
    coverage = ('本轮发现未知' if discovery['count'] is None
                else f'发现 {found}（{_DISCOVERY_LABELS[discovery["status"]]}）')
    if discovery.get('path'):
        coverage += f' · 发现范围 {discovery["path"]}'
    if recommended['status'] == 'recorded':
        coverage += (f' · 已记录推荐 {recommended["count"]} 项，纳入 {recommended["selected_count"]} 项，'
                     f'未纳入 {recommended["omitted_count"]} 项')
        if recommended['omitted_count']:
            coverage += '（本轮结果未知）'
    else:
        coverage += ' · 本轮推荐未知'
    coverage += f' · 实际选择 {scope["selected_count"]} 项'
    records = []
    for side, label in (('before', '基线'), ('after', '候选')):
        run = scope['execution'][side]
        started, finished = (total(run[key], run['accuracy']) for key in ('started', 'finished'))
        text = f'{label}（{_RUN_LABELS[run["status"]]}）开始 {started}，完成记录 {finished}'
        if run['not_started']:
            text += f'，{run["not_started"]} 项未开始'
        records.append(text)
    lines = [coverage, ' · '.join(records)]
    if changed := changed_test_scope_text(scope, 'zh'):
        lines.append(changed)
    return lines
