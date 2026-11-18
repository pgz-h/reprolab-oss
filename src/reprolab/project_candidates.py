"""Collect independent candidates and their ancestry over one verified baseline."""
from .project_history import _checked_report, _match_reference
from .project_review_summary import _review_summary, review_summary
from .replay_verification import _conditions, open_replay_evidence


def compare_candidates(candidates, *, policy, control, plan=None, _allow_single=False):
    if not (1 if _allow_single else 2) <= len(candidates) <= 100:
        raise ValueError('候选历史需要 2–100 份报告。')
    names, digests = set(), set()
    reports, output, issues = {}, [], []
    anchor = None
    for name, path in candidates:
        if (not name.strip() or len(name) > 120 or not name.isprintable() or name in names):
            raise ValueError('每个候选须使用唯一、可显示的名称。')
        names.add(name)
        with open_replay_evidence(path, control=control) as source:
            report = _checked_report(source, control=control)
            digest = report['_report_sha256']
            if digest in digests:
                raise ValueError('候选历史不能重复同一份报告。')
            digests.add(digest)
            baseline = {field: report.get(field, {}).get('before') for field in
                        ('source_sha256', 'source_mode_sha256')}
            baseline['runner'] = report.get('runner', 'pytest')
            if anchor is None:
                anchor = baseline
            comparable = baseline == anchor and all(baseline.values())
            reference = next(iter(reports.values()))[0] if reports else report
            same_conditions = _conditions(reference, report, baseline['runner'])[0] if comparable else {}
            if False in same_conditions.values():
                issues.append({'name': name, 'reason': 'recorded_conditions_changed'})
            summary = (review_summary(source, plan=plan, control=control) if plan
                       else _review_summary(report, False, policy, None))
            if not comparable:
                issues.append({'name': name, 'reason': 'different_or_unknown_baseline'})
            reports[digest] = (report, name)
            output.append({'name': name, 'run_id': report['run_id'], 'report_sha256': digest,
                           'comparison_baseline': 'matched' if comparable else 'blocked',
                           'candidate': report.get('source_sha256', {}).get('after'),
                           'selected_tests': report['selected_tests'], 'gate': summary['gate'], 'conditions': same_conditions,
                           'outcomes': [{'test_id': item['test_id'], 'status': item['status'],
                                         'before': item['before']['status'], 'after': item['after']['status']}
                                        for item in report['entries']]})
    for row in output:
        raw = reports[row['report_sha256']][0]
        if reference := raw.get('review_source'):
            digest = reference['report_sha256']
            if digest in reports:
                parent, name = reports[digest]
                _match_reference(parent, reference)
                row['parent'] = {'name': name, 'report_sha256': digest}
            else:
                row['parent'] = {'name': None, 'report_sha256': digest}
    eligible = [row for row in output if row['comparison_baseline'] == 'matched']
    shared = (set.intersection(*(set(row['selected_tests']) for row in eligible)) if eligible else set())
    root_tests = set(output[0]['selected_tests'])
    same_scope = all(set(row['selected_tests']) == root_tests for row in output)
    # Tests stored from different candidate files have separate observations. They
    # are not treated as evidence of the same assertion.
    def check_fingerprint(row, target):
        fingerprints = reports[row['report_sha256']][0].get('shared_tests') or {}
        if anchor['runner'] == 'go':
            return tuple(sorted(fingerprints.items())) or None
        return fingerprints.get(target.split('::', 1)[0])

    same_checks = {target for target in shared if None not in (
        values := {check_fingerprint(row, target) for row in eligible}) and len(values) == 1}
    states = {row['gate']['status'] for row in eligible}
    status = ('failed' if 'failed' in states else 'blocked'
              if issues or 'blocked' in states or not same_scope or same_checks != shared else 'passed')
    code = {'passed': 0, 'failed': 1, 'blocked': 2}[status]
    return {'kind': 'project_candidate_history', 'schema_version': 1, 'status': status, 'exit_code': code,
            'baseline': anchor, 'shared_tests': sorted(shared), 'same_test_content': sorted(same_checks),
            'same_selection': same_scope, 'candidates': output, 'issues': issues,
            'notice': '候选来源和运行分开核对，共同测试不表示外部环境或未记录的辅助输入相同。'}, code


def run_candidates(repository, before, candidates, output, *, selected, plan, python_executable, timeout, confirmed, control):
    from pathlib import Path
    import tempfile

    from .acceptance_plan import execution_selection
    from .git_source import resolve_commit
    from .persistence import save_json
    from .project_selections import validate_selection
    from .project_sessions import prepare_project, run_project
    from .recipes import repository_root

    root = repository_root(repository)
    before = resolve_commit(root, before)
    if not 2 <= len(candidates) <= 20 or len({name for name, _ in candidates}) != len(candidates):
        raise ValueError('请选择 2–20 个具名且不重复的候选。')
    resolved = [(name, resolve_commit(root, version)) for name, version in candidates]
    targets = validate_selection(selected if selected is not None else execution_selection(plan, 'pytest') if plan else [])
    if not confirmed:
        return {'status': 'ready', 'baseline': before, 'candidates': resolved, 'tests': targets}, 0
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(root):
        raise ValueError('请选择仓库之外的新候选比较目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.candidate-comparison-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'comparison'
        stage.mkdir()
        reports, issues = [], []
        for name, candidate in resolved:
            control.check()
            try:
                state = prepare_project(root, before, candidate, stage / 'sessions', python_executable=python_executable)
                report = run_project(state['directory'], stage / 'reports', selected_ids=targets,
                                     python_executable=python_executable, timeout=timeout,
                                     control=control.child(progress=control.progress), confirmed=True, acceptance_plan=plan)
            except (OSError, ValueError) as exc:
                issues.append({'name': name, 'candidate': candidate, 'reason': str(exc)})
                continue
            reports.append((name, report['report_dir']))
        if reports:
            result, code = compare_candidates(reports, policy=plan.policy if plan else 'strict',
                                              control=control, plan=plan, _allow_single=True)
        else:
            result, code = {'kind': 'project_candidate_history', 'schema_version': 1, 'status': 'blocked',
                            'candidates': [], 'issues': []}, 2
        result['references'] = [{'name': name, 'report': Path(path).relative_to(stage).as_posix()}
                                for name, path in reports]
        result['issues'].extend(issues)
        if issues and code == 0:
            result['status'], code = 'blocked', 2
        result['exit_code'] = code
        save_json(stage / 'candidate-comparison.json', result)
        control.commit()
        if output.exists() or output.is_symlink():
            raise ValueError('目标候选比较目录已经存在。')
        stage.rename(output)
    return {**result, 'directory': str(output)}, code
