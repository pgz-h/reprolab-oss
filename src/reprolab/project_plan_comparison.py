"""Evaluate the plans saved with the two versions against one checked report."""
import json
from pathlib import Path

from .acceptance_plan import read_acceptance_plan
from .project_history import _checked_report, report_reference
from .project_review_summary import _approved_scope, _code, _revision, _revision_text, review_summary
from .project_selections import read_selection
from .project_sessions import _digest
from .project_snapshot_scope import normalize_source_scope
from .replay_verification import open_replay_evidence


_SIDES = {'target': 'before', 'proposed': 'after'}
_CHECKS = ('required_improvements', 'required_passes', 'approved_scope', 'baseline_check',
           'test_contract', 'protected_inputs', 'receipt', 'stability')
_RULE_DEFAULTS = {
    'runner': None, 'policy': None, 'required_tests': [], 'required_passes': [],
    'approved_selection': None, 'baseline': None, 'require_receipt': False, 'min_stability_rounds': None,
    'protected_inputs': None, 'test_contract': None,
}


def _snapshot_config(folder, report, side, name, reader):
    source = f'evidence/project/inputs/{side}/{name}'
    expected = report.get('persistence', {}).get('sha256', {}).get(source)
    path = folder / source
    if not expected or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(folder):
        raise ValueError('此文件未保存于该版本的可核对证据。')
    config = reader(path)
    if _digest(path) != expected:
        raise ValueError('配置文件与保存的证据不一致。')
    return config, expected


def _blocked(reason, policy=None):
    return {'policy': policy, 'status': 'blocked', 'exit_code': 2, 'reason': reason}


def _evaluate(folder, report, side, plan_path, selection_path, *, control):
    revision = _revision(report.get(side + '_commit'), report['source_sha256'][side])
    row = {'source': {'snapshot': side, 'revision': revision, 'plan_path': plan_path}}
    if side == 'before' and (context := report.get('pr_context')) and context['base'] != context['before']:
        row['gate'] = _blocked('target_revision_not_in_report')
        row['issues'] = [{'part': 'plan', 'message': '本报告的基线是共同祖先。请用 PR merge 模式取得目标版本证据。'}]
        return row, None, None

    try:
        plan, digest = _snapshot_config(folder, report, side, plan_path, read_acceptance_plan)
    except (OSError, ValueError) as exc:
        row['gate'] = _blocked('plan_unavailable')
        row['issues'] = [{'part': 'plan', 'message': str(exc)}]
        return row, None, None
    row['source']['plan_file_sha256'] = digest
    row['acceptance_plan_sha256'] = plan.fingerprint()
    if plan.runner != report.get('runner', 'pytest'):
        row['gate'] = _blocked('plan_runner_mismatch', plan.policy)
        row['issues'] = [{'part': 'plan', 'message': '计划执行器与报告不符。请使用该计划的执行器重新采集证据。'}]
        return row, plan, None

    checked = review_summary(folder, plan=plan, expected_report=report_reference(report), control=control)
    row.update(gate=checked['gate'], comparison_gate=checked['comparison_gate'])
    row['checks'] = {key: checked[key] for key in _CHECKS if key in checked}
    selection = None
    if selection_path is not None:
        row['source']['selection_path'] = selection_path
        try:
            selection, digest = _snapshot_config(folder, report, side, selection_path, read_selection)
        except (OSError, ValueError) as exc:
            row['issues'] = [{'part': 'selection', 'message': str(exc)}]
            if row['gate']['exit_code'] != 1:
                row['gate'] = _blocked('selection_unavailable', plan.policy)
        else:
            row['source']['selection_file_sha256'] = digest
            row['checks']['selection_file_scope'] = _approved_scope(report, selection)
            if row['checks']['selection_file_scope']['missing_tests'] and row['gate']['exit_code'] != 1:
                row['gate'] = _blocked('selection_not_covered', plan.policy)

    missing = [test for key in ('approved_scope', 'selection_file_scope')
               for test in row['checks'].get(key, {}).get('missing_tests', ())]
    goals = [goal for key in ('required_improvements', 'required_passes') for goal in checked.get(key, ())]
    missing_targets = [goal for goal in goals if goal['outcome'] is None]
    incomplete = [goal for goal in goals if goal['outcome'] == 'inconclusive']
    if missing_targets:
        row['missing_targets'] = missing_targets
    if missing or missing_targets or incomplete:
        missing = list(dict.fromkeys([*missing, *(goal.get('file', goal['test_id']) for goal in missing_targets)]))
        row['missing_tests'] = missing
        row['rerun_selection'] = list(dict.fromkeys([*report['selected_tests'], *missing]))
    return row, plan, selection


def _rule_changes(target, proposed):
    if target is None or proposed is None:
        return None
    old, new = target.record(), proposed.record()
    changes = {}
    for key, default in _RULE_DEFAULTS.items():
        before, after = old.get(key, default), new.get(key, default)
        if before != after:
            if key in ('protected_inputs', 'test_contract'):
                changes[key] = {'target_configured': before is not None, 'proposed_configured': after is not None,
                                'contents_changed': True}
            else:
                changes[key] = {'target': before, 'proposed': after}
    return changes


def compare_frozen_plans(location, plan_path, *, selection_path=None, control=None):
    plan_path = normalize_source_scope([plan_path])[0]
    if selection_path is not None:
        selection_path = normalize_source_scope([selection_path])[0]
    with open_replay_evidence(location, control=control) as source:
        report = _checked_report(source, control=control)
        if selection_path is not None and report.get('runner', 'pytest') != 'pytest':
            raise ValueError('独立选择清单的两计划核对目前支持 pytest。')
        folder = Path(report['report_dir'])
        target, old, target_selection = _evaluate(folder, report, _SIDES['target'], plan_path, selection_path, control=control)
        proposed, new, proposed_selection = _evaluate(folder, report, _SIDES['proposed'], plan_path, selection_path, control=control)
    result = {
        'kind': 'project_acceptance_plan_comparison', 'schema_version': 1,
        'report_sha256': report['_report_sha256'], 'selected_tests': report['selected_tests'],
        'target': target, 'proposed': proposed, 'rule_changes': _rule_changes(old, new),
        'decision_basis': 'target', 'gate': target['gate'],
    }
    if target_selection is not None and proposed_selection is not None and target_selection != proposed_selection:
        result['selection_change'] = {'target': target_selection, 'proposed': proposed_selection}
    return result


def render_plan_comparison(result):
    gate = result['gate']
    lines = ['### Acceptance plan comparison', '',
             f'**Target plan decision · {gate["status"].title()} · exit {gate["exit_code"]}**', '',
             f'Checked report SHA-256 · {_code(result["report_sha256"])}',
             f'Recorded scope · {len(result["selected_tests"])} selected tests', '',
             '| Plan | Snapshot | Policy | Result | Comparison gate |',
             '| --- | --- | --- | --- | --- |']
    for name in _SIDES:
        row = result[name]
        gate = row['gate']
        reason = row.get('comparison_gate', gate)['reason']
        lines.append(f'| {name.title()} | {_revision_text(row["source"]["revision"])} | '
                     f'{gate["policy"] or "Unavailable"} | {gate["status"].title()} · {gate["exit_code"]} | {_code(reason)} |')
    for name in _SIDES:
        row = result[name]
        lines.extend(['', f'**{name.title()} plan evidence** · {_code(row["source"]["plan_path"])}'])
        if digest := row.get('acceptance_plan_sha256'):
            lines.append(f'- Plan SHA-256 · {_code(digest)}')
        if selection := row['source'].get('selection_path'):
            digest = row['source'].get('selection_file_sha256')
            lines.append(f'- Selection · {_code(selection)}' + (f' · SHA-256 {_code(digest)}' if digest else ''))
        for issue in row.get('issues', ()):
            lines.append(f'- {issue["message"]}')
        for key, check in row.get('checks', {}).items():
            if isinstance(check, dict) and check.get('status') == 'blocked':
                lines.append(f'- Evidence needed · {_code(key)}')
        for key, label in (('required_improvements', 'Required improvement'), ('required_passes', 'Required continued pass')):
            for goal in row.get('checks', {}).get(key, ()):
                identity = ' · '.join(_code(goal[key]) for key in ('file', 'test_id') if key in goal)
                lines.append(f'- {label} · {identity} · {goal["status"].title()}')
        if row.get('missing_tests'):
            lines.append('- Missing tests · ' + ', '.join(map(_code, row['missing_tests'])))
        if row.get('rerun_selection'):
            lines.append('- Repeat both versions with · ' + ', '.join(map(_code, row['rerun_selection'])))
    changes = result['rule_changes']
    lines.extend(['', '**Plan rule changes**', ''])
    if changes is None:
        lines.append('Both plans are required to compare their rules.')
    elif not changes:
        lines.append('The plans specify the same rules.')
    else:
        for rule, values in changes.items():
            if values.get('contents_changed'):
                lines.append(f'- {_code(rule)} · protected configuration changed')
                continue
            before, after = (_code(json.dumps(values[name], ensure_ascii=False)) for name in _SIDES)
            lines.append(f'- {_code(rule)} · {before} → {after}')
    if selection := result.get('selection_change'):
        lines.extend(['', '**Selection file changes**', ''])
        for name in _SIDES:
            lines.append(f'- {name.title()} · ' + ', '.join(map(_code, selection[name])))
    return '\n'.join(lines) + '\n'
