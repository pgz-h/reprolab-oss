"""A short, shareable rendering of a checked project review."""
from .project_run_scope import changed_test_scope_text
from .project_review_summary import (
    COMPARISONS, OUTCOMES, RUNNERS, RUN_STATUS, _matching_markdown, review_summary,
)


_GATE = {
    'passed': ('Passed', '通过'),
    'failed': ('Failed', '失败'),
    'blocked': ('Blocked', '受阻'),
}
_REASONS = {
    'selected_scope_passed': ('the selected scope passed', '所选范围通过'),
    'new_failures': ('new failures were observed', '发现新增失败'),
    'existing_failures_allowed': ('existing failures are allowed by this policy', '此策略允许已有失败'),
    'existing_failures': ('existing failures remain', '已有失败仍存在'),
    'blocked_or_incomplete': ('results were incomplete or could not support a decision', '记录不完整或不足以支持判定'),
    'acceptance_requirements': ('evaluated with the supplied acceptance requirements', '已按提供的验收条件核对'),
    'pytest_discovery_scope': ('the discovery scope needs review', '发现范围需复核'),
    'same_snapshot_divergence': ('identical snapshots gave different results', '相同快照产生了不同结果'),
    'excluded_changes_only': ('the changes were outside the snapshots', '改动在快照范围之外'),
    'pytest_import_origin': ('the actual import origin needs review', '实际导入来源需复核'),
    'failure_shape_changed': ('the failure changed between versions', '两版故障表现发生变化'),
    'pytest_assertion_site_changed': ('the recorded failing assertion changed', '记录的失败断言发生变化'),
    'pytest_assertion_site_ambiguous': ('repeated failing assertions could not be aligned', '重复断言无法对齐'),
    'pytest_legacy_assertion_site_unverified': ('the older assertion record lacks occurrence evidence', '旧断言记录缺少出现位置证据'),
    'pytest_legacy_assertion_unverified': ('the older failure evidence needs review', '旧失败证据需复核'),
    'pytest_fixture_changed': ('a fixture used by an improved test changed', '改善测试使用的夹具发生变化'),
    'pytest_parameter_changed': ('parameters used by an improved test changed', '改善测试的参数发生变化'),
    'pytest_input_changed': ('recorded inputs used by an improved test changed', '改善测试的已记录输入发生变化'),
    'pytest_version_order': ('a version-order experiment needs review', '版本先后对照需复核'),
    'pytest_order_control': ('a fixed-order experiment needs review', '固定顺序对照需复核'),
    'pytest_fixture_control': ('a baseline-fixture comparison needs review', '基线夹具对照需复核'),
    'pytest_test_source_control': ('a baseline-test comparison needs review', '基线测试文件对照需复核'),
    'pytest_input_control': ('a fixed-input comparison needs review', '固定业务输入对照需复核'),
}
_COMPARISONS_ZH = {
    'direct': '两版直接对照', 'pr_head': 'PR 改动审查（共同祖先 → PR head）',
    'pr_merge': 'PR 合入验收（目标提交 → 已核对的合并结果）',
}
_OUTCOMES_ZH = {
    'regression_detected': '新增失败', 'inconclusive': '未定', 'baseline_failure': '已有失败',
    'improved': '由失败转为通过', 'regression_passed': '两版通过',
}
_RUN_STATUS_ZH = {
    'completed': '已结束', 'cancelled': '已取消', 'collection_failed': '收集失败',
    'dependencies_failed': '依赖准备失败', 'not_run': '未执行', 'timed_out': '超时',
    'worker_error': '执行记录不完整', 'unknown': '未知',
}


def brief_lines(summary, language='en'):
    """Return short lines using counts and fixed labels from the checked review."""
    zh = language == 'zh'
    gate = summary['gate']
    comparison = summary.get('comparison_type', 'direct')
    runner = summary['runner']
    reason = _REASONS.get(gate['reason'], ('see the full report for the reason', '原因见完整报告'))[zh]
    policy = {'strict': ('strict', '严格'), 'no-new-failures': ('no new failures', '允许已有失败')}[gate['policy']][zh]
    lines = [
        f'门禁 {_GATE[gate["status"]][zh]}（{policy}） · {reason}。' if zh else
        f'Gate {_GATE[gate["status"]][zh]} ({policy}) · {reason}.',
    ]
    selected = summary['selected_count']
    units = ('test file' if selected == 1 else 'test files') if runner == 'vitest' else ('test' if selected == 1 else 'tests')
    label = (_COMPARISONS_ZH if zh else COMPARISONS)[comparison]
    lines.append(f'范围 {label} · {RUNNERS[runner]} · 选择 {selected} ' + ('个测试文件' if runner == 'vitest' else '项测试')
                 if zh else f'Scope {label} · {RUNNERS[runner]} · {selected} selected {units}')
    counts = summary['counts']
    labels = _OUTCOMES_ZH if zh else OUTCOMES
    outcomes = [f'{name} {counts[key]}' for key, name in labels.items() if counts.get(key)]
    if not outcomes:
        outcomes = ['无逐项结果' if zh else 'No individual results']
    changes = summary.get('comparison_scope', {}).get('divergent_outcomes')
    if changes:
        outcomes.append(('同快照观察到' if zh else 'Observed on identical snapshots') +
                        f' ↑{changes.get("improved", 0)} ↓{changes.get("regression_detected", 0)}')
    lines.append(('结果 ' if zh else 'Results ') + ' · '.join(outcomes))
    if cases := summary.get('case_counts'):
        outcomes = [f'{name} {cases[key]}' for key, name in labels.items() if cases.get(key)]
        lines.append(('文件内用例 ' if zh else 'Cases within files ') + ' · '.join(outcomes))

    limits = ['结论限于所选范围' if zh else 'Only the selected scope is covered']
    if run_scope := summary.get('run_scope'):
        if changed := changed_test_scope_text(run_scope, language):
            lines.append(changed)
        discovery = run_scope['discovery']
        count = discovery['count']
        if count is None:
            limits.append('发现范围未知' if zh else 'discovery scope unknown')
        elif discovery['status'] != 'complete':
            limits.append(f'发现记录不完整，至少 {count} 项' if zh else f'incomplete discovery, at least {count} recorded')
        elif count > selected:
            omitted = count - selected
            limits.append(f'{omitted} 项已发现但未选' if zh else f'{omitted} discovered outside the selection')
        omitted = run_scope['recommendations']['omitted_count']
        if omitted:
            limits.append(f'{omitted} 项推荐未纳入，结果未知' if zh else f'{omitted} recommended but unselected, outcomes unknown')
    if (missing := len(summary.get('approved_scope', {}).get('missing_tests', ()))) > 0:
        limits.append(f'已批准范围缺少 {missing} 项' if zh else f'{missing} approved tests missing from the selection')
    if baseline_only := summary.get('discovery_audit', {}).get('baseline_only_count'):
        limits.append(f'{baseline_only} 项仅在基线发现' if zh else f'{baseline_only} tests discovered on the baseline only')
    if excluded := summary.get('comparison_scope', {}).get('excluded_tracked_changes'):
        limits.append(f'{excluded} 个已跟踪路径未纳入快照' if zh else f'{excluded} tracked paths excluded from the snapshots')
    if any(status != 'completed' for status in summary['runs'].values()):
        statuses = _RUN_STATUS_ZH if zh else RUN_STATUS
        before, after = (statuses.get(summary['runs'][side], 'Unknown') for side in ('before', 'after'))
        limits.append(f'基线 {before}，候选 {after}' if zh else f'baseline {before}, candidate {after}')
    lines.append(('局限 ' if zh else 'Limits ') + ' · '.join(limits) + ('。' if zh else '.'))
    return lines


def render_brief(summary, language='en', *, from_zip=True, handoff=True):
    zh = language == 'zh'
    if from_zip:
        trace = (
            '本次只核对已保存的证据。请导入刚核对的原 ZIP，再打开 `report.html` 或 `report.json` 查看逐项记录。'
            '导入用法见 `reprolab project unpack --help`。'
            if zh else 'This command checked saved evidence. Unpack the same ZIP you just checked and open '
                       '`report.html` or `report.json` for individual records. '
                       'For unpacking instructions, run `reprolab project unpack --help`.'
        )
    else:
        trace = (
            '本次只核对已保存的证据。打开同一报告目录中的 `report.html` 或 `report.json` 查看逐项记录。'
            if zh else 'This command checked saved evidence. Open `report.html` or `report.json` '
                       'in the same report directory for individual records.'
        )
    if handoff:
        trace += ('\n\n与原证据 ZIP 一起分享。接收方可用 `project check-summary --brief --language zh` 离线核对这份汇总。'
                  if zh else '\n\nShare this with the original evidence ZIP. A recipient can check the displayed '
                             'aggregates offline with `project check-summary --brief`.')
    return '\n'.join([
        '### ReproLab 决策摘要' if zh else '### ReproLab decision brief',
        '',
        *(f'- {line}' for line in brief_lines(summary, language)),
        f'- {"证据" if zh else "Evidence"} `report.json` · SHA-256 `{summary["report_sha256"]}`',
        '',
        trace,
        '',
    ])


def brief_summary(directory, **options):
    """Evaluate the same gate for generation and independent verification."""
    if options.get('plan') is None and options.get('policy') is None:
        options['policy'] = 'strict'
    return review_summary(directory, **options, public=False)


def check_brief(directory, markdown, *, language='en', **options):
    summary = brief_summary(directory, **options)
    # The trace paragraph depends on the sender's entry path, while the report
    # fingerprint and every decision line must match for either entry path.
    candidates = {
        (from_zip, handoff): render_brief(summary, language, from_zip=from_zip, handoff=handoff).encode('utf-8')
        for from_zip in (True, False)
        for handoff in (True, False)
    }
    matches = _matching_markdown(markdown, candidates) is not None
    return {
        'kind': 'project_review_brief_check',
        'schema_version': 1,
        'status': 'consistent' if matches else 'different',
        'language': language,
        'coverage': 'rendered_aggregate_markdown',
        'report_sha256': summary['report_sha256'],
        'gate': summary['gate'],
        'notice': (
            '核对范围为精简摘要全文及其中展示的汇总。门禁按本次提供的选项重新计算，可用完整摘要核对逐项信息。'
            if language == 'zh' else
            'This compares the complete brief and the aggregates it displays. The gate is recomputed with '
            'the options supplied here. Use the full summary to check individual details.'
        ),
    }
