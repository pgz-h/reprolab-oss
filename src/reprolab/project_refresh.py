"""Refresh immutable inputs while retaining their baseline and selection intent."""
from pathlib import Path

from .case import parse_test_target
from .project_collection import CollectionPathUnavailable
from .project_selections import reconcile_selection, requested_selection, validate_selection
from .project_sessions import _collection, _identity, _prepare_project, read_project
from .recipes import repository_root
from .workspace import WORKTREE


def refresh_project(directory, output_root, *, selected_ids=None, python_executable=None, control=None,
                    review_source=None, expected_source=None):
    """Publish a new session with the verified baseline and the current worktree."""
    if control:
        control.check()
    source = read_project(directory)
    if source.get('runner') in ('vitest', 'go'):
        raise ValueError(f"请使用 project {source['runner']} 重新检查，或使用 project replay 重放报告。")
    if expected_source is not None and _identity(source) != _identity(expected_source):
        raise ValueError('来源报告的固定会话发生变化，请重新打开报告。')
    selected = (requested_selection(source) if selected_ids is None
                else validate_selection(selected_ids, allow_empty=True))
    output = Path(output_root).expanduser().resolve()
    root = repository_root(source['repository'])
    if output.is_relative_to(Path(source['directory'])) or output.is_relative_to(root):
        raise ValueError('刷新会话须保存在原会话和被测仓库之外。')
    metadata = {'refresh_origin': {
        'project_id': source['project_id'], 'directory': source['directory'],
        'source_sha256': source['source_sha256'],
        'source_mode_sha256': source['source_mode_sha256'],
        'before_commit': source['before_commit'], 'after_commit': source['after_commit'],
        'notice': '复用已核对的基线快照，改动后版本来自刷新时的当前工作区。',
    }}
    if selected is not None:
        metadata['selection_request'] = {
            'schema_version': 1, 'kind': 'project_selection_request', 'selected_tests': selected,
            'notice': '这是待重新发现并核对的选择意图，不是可运行清单或执行结论。',
        }
    origin_report = review_source if review_source is not None else source.get('review_source')
    if origin_report is not None:
        from .project_history import validate_report_reference
        metadata['review_source'] = dict(validate_report_reference(origin_report))
    return _prepare_project(root, source['before_commit'], WORKTREE, output,
        python_executable=python_executable or source['runtime']['executable'],
        title=source['title'], issue=source.get('issue', ''), source=source,
        refresh_metadata=metadata, control=control, collection_path=source.get('collection_path', ''))


def refresh_from_displayed_report(directory, output_root, reference, *, selected_ids=None, python_executable=None, control=None):
    """Keep the explicitly displayed run as parent while preserving current selection intent."""
    from .project_history import _checked_report, _match_reference, report_reference
    from .project_history import validate_report_reference
    validate_report_reference(reference)
    state = read_project(directory)
    report = _checked_report(reference['report_dir'], control=control)
    _match_reference(report, reference)
    if _identity(state) != _identity(report):
        raise ValueError('当前显示报告不属于这份固定会话，未关联其他运行。')
    origin = {**report_reference(report), 'selection_mode': 'workspace'}
    if selected_ids is not None:
        origin['selected_tests'] = validate_selection(selected_ids, allow_empty=True)
    return refresh_project(directory, output_root, selected_ids=selected_ids,
        python_executable=python_executable, control=control, review_source=origin, expected_source=report)


def review_project_selection(directory, *, selected_ids=None):
    """Read saved discovery evidence only; never start an interpreter or pytest."""
    state = read_project(directory)
    selected = (requested_selection(state) if selected_ids is None
                else validate_selection(selected_ids, allow_empty=True))
    collection = _collection(state, state['runtime'])
    if collection is None:
        return {'status': 'needs_collection', 'project_id': state['project_id'],
                'requested_tests': selected, 'ready': False,
                'range_state': 'unspecified' if selected is None else 'empty' if not selected else 'pending',
                'notice': '尚无此会话的有效测试发现记录。请先明确执行 project collect，再只读核对选择。'}
    result = reconcile_selection(selected, collection['tests'],
        collection_complete=not (collection.get('partial') or collection.get('truncated')))
    return {**result, 'project_id': state['project_id'],
            'ready': bool(result['matched_tests']) and not result['requires_confirmation'],
            'collection_partial': bool(collection.get('partial') or collection.get('truncated')),
            'notice': result['notice'] + '仅核对已保存发现结果，不保证当前环境或发现范围完整。'}


def refresh_from_report(directory, output_root, *, scope='all', python_executable=None,
                        expected_reference=None, control=None, selected_ids=None):
    """Bind an exact run and reuse its checked frozen session, without running tests."""
    from .project_history import _checked_report, _match_reference
    report = _checked_report(directory, control=control) if control else _checked_report(directory)
    if expected_reference is not None:
        _match_reference(report, expected_reference)
    return _refresh_checked_report(report, output_root, scope=scope, python_executable=python_executable,
                                   control=control, selected_ids=selected_ids)


def _refresh_checked_report(report, output_root, *, scope, python_executable, control, selected_ids):
    from .project_history import report_reference
    if scope not in ('all', 'attention'):
        raise ValueError('复查范围只能选择来源报告全部实际选择或失败／受阻项。')
    if selected_ids is not None:
        if scope != 'all':
            raise ValueError('明确测试清单与 attention 范围不能同时使用。')
        requested = validate_selection(selected_ids)
        members = set(requested)
        if not members <= set(report['selected_tests']):
            raise ValueError('复查清单只能包含来源报告实际选择的测试，未准备或执行其他测试。')
        selected = [identity for identity in report['selected_tests'] if identity in members]
        scope = 'selected'
    else:
        selected = list(report['selected_tests']) if scope == 'all' else [
            item['test_id'] for item in report['entries']
            if item.get('status') in {'regression_detected', 'baseline_failure', 'inconclusive'}]
        selected = validate_selection(selected, allow_empty=True)
    reference = {**report_reference(report), 'selection_mode': scope, 'selected_tests': selected}
    evidence = Path(report['report_dir']) / 'evidence/project'
    return refresh_project(evidence, output_root, selected_ids=selected,
                           python_executable=python_executable or report.get('runtime', {}).get('executable'),
                           control=control, review_source=reference, expected_source=report)


def recheck_and_run(directory, output_root, report_root, *, scope='all', python_executable=None,
                    timeout=None, expected_reference=None, control=None, selected_ids=None):
    """Explicit shortcut: freeze, discover, reconcile, then run unchanged checks.

    Ambiguous ranges or changed test inputs stop at the prepared session;
    the caller can show it for review without losing either source report.
    """
    from .project_history import _checked_report, _match_reference, changed_candidate_support
    from .project_sessions import collect_project, run_project, recorded_project_timeout, _digest, _timeout
    from .pytest_profile import read_pytest_profile
    from .runner import RunControl
    if timeout is not None:
        _timeout(timeout)
    control = control or RunControl()
    control.check()
    original = _checked_report(directory, control=control)
    if timeout is None:
        timeout = recorded_project_timeout(original) or 120
    if expected_reference is not None:
        _match_reference(original, expected_reference)
    control.notify('project_refresh', {'notice': '冻结当前修改，保留来源报告及固定基线。'})
    state = _refresh_checked_report(original, output_root, scope=scope, python_executable=python_executable,
                                   control=control.child(), selected_ids=selected_ids)
    selected = requested_selection(state)
    if not selected:
        return {'status': 'needs_review', 'state': state, 'report': None,
                'notice': '此范围没有可复查项；新会话已保留，请明确选择测试。'}
    try:
        collection = collect_project(state['directory'], python_executable=python_executable, timeout=timeout,
                                     control=control.child(progress=control.progress))
    except CollectionPathUnavailable as exc:
        return {'status': 'needs_review', 'state': state, 'report': None, 'notice': str(exc)}
    state = read_project(state['directory'])
    result = {'status': 'needs_review', 'state': state, 'collection': collection, 'report': None}
    control.check()
    if collection['status'] != 'collected':
        return {**result, 'notice': '测试发现未完成。已保留新会话，请检查环境或插件配置后重新发现。'}
    review = collection['selection_review']
    review = {**review, 'ready': bool(review['matched_tests']) and not review['requires_confirmation']}
    if not review['ready']:
        return {**result, 'selection_review': review, 'notice': '原测试范围需要核对，未开始两版执行。' + review['notice']}
    current = Path(state['directory']) / 'inputs/after'
    targets = {parse_test_target(identity) for identity in selected}
    shared_tests = original.get('shared_tests') or {}
    changed_tests = sorted(path for path in targets if shared_tests.get(path) != _digest(current / path))
    previous = Path(original['report_dir']) / 'evidence/project/inputs/after'
    profile = collection['pytest_profile']
    previous_profile = read_pytest_profile(previous)
    profile_changed = previous_profile != profile
    plugins = set(previous_profile['plugins']) | set(profile['plugins'])
    changed_support = changed_candidate_support(original, current, targets, plugins, control=control)
    if changed_tests or profile_changed or changed_support:
        reasons = []
        if changed_tests:
            reasons.append('原测试文件已变化或历史指纹不足：' + '、'.join(changed_tests))
        if profile_changed:
            reasons.append('测试插件、导入方式或运行变量配置已变化')
        if changed_support:
            reasons.append('测试配置、fixture 或辅助材料已变化：' + '、'.join(changed_support))
        return {**result, 'changed_tests': changed_tests, 'pytest_profile_changed': profile_changed,
                'changed_support_files': changed_support,
                'notice': '；'.join(reasons) + '。新会话已保留，请核对后点击“运行所选项”。'}
    control.check()
    report = run_project(state['directory'], report_root, selected_ids=selected,
                         python_executable=python_executable, timeout=timeout, confirmed=True, control=control)
    return {'status': 'cancelled' if report['status'] == 'cancelled' else 'completed',
            'state': read_project(state['directory']), 'collection': collection, 'report': report,
            'notice': '本次复查已取消，未完成项仍为受阻；已返回记录保留。' if report['status'] == 'cancelled'
                      else '已按原固定基线完成当前工作区复查，原报告保留。'}
