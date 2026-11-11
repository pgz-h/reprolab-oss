"""Checked report identities and comparisons; never execute project code."""
from collections import Counter
import math
import hashlib
import json
from pathlib import Path
import re

from .case import parse_test_target
from .runtime_environment import compare_variable_records
from .project_sessions import _digest, check_project_report, MAX_RECORD_BYTES

BUNDLED_SOURCES = 'reprolab-sources'

CHANGE_LABELS = {
    'resolved': '相同已记录条件下转为通过／改善', 'still_failed': '仍失败',
    'conditions_changed': '检查条件已变化，转绿需复核',
    'conditions_unknown': '检查条件不完整，转绿需复核',
    'blocked': '本轮受阻', 'not_run': '本轮未完整执行',
    'test_changed': '测试文件已变化，需复核', 'test_unknown': '测试文件指纹未知，需复核',
    'now_passed': '本轮通过，前次未定／未执行', 'unchanged': '结果相同',
    'deteriorated': '转为失败', 'changed': '结果变化',
    'only_before': '未复查（仅前次选中）', 'only_after': '仅本次选中',
}
TEST_LABELS = {'same': '相同', 'changed': '已变化，需复核', 'unknown': '未知', 'not_common': '非共同选择'}
EXECUTION_LABELS = {'passed': '通过', 'failed': '失败', 'blocked': '受阻',
                    'not_run': '未完整执行', 'not_selected': '未复查'}
_ACCEPTABLE = {'regression_passed', 'improved'}
_FAILED = {'regression_detected', 'baseline_failure'}


def _hash(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) else None


def read_report_snapshot(directory):
    folder = Path(directory).expanduser().resolve()
    if folder.is_file():
        folder = folder.parent
    path = folder / 'report.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError('报告不存在、过大或不是普通文件，请重新定位报告目录。')
    with path.open('rb') as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('项目报告超过 128 MiB。')
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('项目报告不是有效 JSON。') from None
    fingerprint = hashlib.sha256(raw).hexdigest()
    if (not isinstance(report, dict) or report.get('kind') != 'project_regression' or report.get('schema_version') != 1
            or not isinstance(report.get('entries'), list)):
        raise ValueError('请选择项目回归报告目录。')
    if _digest(path) != fingerprint:
        raise ValueError('读取期间报告发生变化，请重新打开。')
    return {**report, 'report_dir': str(folder), '_report_sha256': fingerprint}


def _checked_report(directory, *, control=None):
    original = read_report_snapshot(directory)
    check = check_project_report(directory, control=control) if control else check_project_report(directory)
    if check['status'] != 'consistent':
        raise ValueError('运行证据不一致，已停止操作：' + '; '.join(check['issues']))
    report = read_report_snapshot(check['report_dir'])
    if report != original:
        raise ValueError('核对期间报告发生变化，请重新打开后比较。')
    return report


def validate_report_reference(reference):
    if (not isinstance(reference, dict) or reference.get('schema_version') != 1
            or reference.get('kind') != 'project_report_reference'
            or any(not isinstance(reference.get(key), str) or not reference[key]
                   for key in ('project_id', 'run_id', 'report_dir'))
            or not _hash(reference.get('report_sha256'))):
        raise ValueError('来源报告身份记录不完整，不能按目录、标题或最近一次运行猜测来源。')
    return reference


def report_reference(report):
    return validate_report_reference({
        'schema_version': 1, 'kind': 'project_report_reference',
        'project_id': report.get('project_id'), 'run_id': report.get('run_id'),
        'report_sha256': report.get('_report_sha256'), 'report_dir': report.get('report_dir'),
        'project_directory': report.get('project_directory'),
    })


def _match_reference(report, reference):
    validate_report_reference(reference)
    if (report.get('project_id') != reference['project_id'] or report.get('run_id') != reference['run_id']
            or report.get('_report_sha256') != reference['report_sha256']):
        raise ValueError('所选报告与记录的来源身份不符：会话、运行 ID 或报告指纹不同。未自动替换来源。')


def bundled_source_directory(report, reference):
    validate_report_reference(reference)
    folder = Path(report['report_dir'])
    if folder.parent.name == BUNDLED_SOURCES and folder.name == report['_report_sha256']:
        folder = folder.parent.parent
    source = folder / BUNDLED_SOURCES / reference['report_sha256']
    return source if source.exists() or source.is_symlink() else None


def source_report_directory(report, reference, preferred=None):
    bundled = bundled_source_directory(report, reference)
    if preferred and (bundled is None or Path(preferred).expanduser().exists()):
        return preferred
    return bundled or reference['report_dir']


def resolve_report_reference(reference, directory=None):
    validate_report_reference(reference)
    location = Path(directory or reference['report_dir']).expanduser()
    if not location.exists():
        raise ValueError('来源报告本次未找到，请重新定位原报告目录。须匹配原会话、运行 ID 和报告指纹。')
    report = _checked_report(location)
    _match_reference(report, reference)
    return report


def _environment(report):
    recorded, runtime = report.get('environment'), report.get('runtime')
    if not isinstance(recorded, dict) or not isinstance(runtime, dict):
        return None
    versions = [{key: (report.get(side) or {}).get(key) for key in ('python_version', 'pytest_version')}
                for side in ('before', 'after')]
    if (recorded.get('status') != 'recorded' or not isinstance(recorded.get('distributions'), dict)
            or any(not runtime.get(key) for key in ('executable', 'python_version', 'pytest_version'))
            or not recorded.get('platform') or not recorded.get('machine')
            or any(not value for pair in versions for value in pair.values())):
        return None
    return {'runtime': runtime, 'distributions': recorded['distributions'], 'versions': versions,
            'platform': recorded['platform'], 'machine': recorded['machine']}


def _same(left, right):
    return None if left is None or right is None else left == right


def _all_same(checks):
    return False if False in checks else True if all(value is True for value in checks) else None


def _source_hash(report, key, side):
    value = report.get(key)
    return _hash(value.get(side)) if isinstance(value, dict) else None


def _baseline(before, after):
    checks = [_same(before.get('before_commit') or None, after.get('before_commit') or None)]
    checks.extend(_same(_source_hash(before, key, 'before'), _source_hash(after, key, 'before'))
                  for key in ('source_sha256', 'source_mode_sha256'))
    return _all_same(checks)


def _timeout(report):
    value = report.get('timeout')
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def _entry(entry):
    if entry is None:
        return None
    return {'status': entry.get('status'), **{side: {
        key: (entry.get(side) or {}).get(key)
        for key in ('status', 'exception', 'duration_seconds')}
        for side in ('before', 'after')}}


def _execution(entry):
    if entry is None:
        return 'not_selected'
    if any((entry.get(side) or {}).get('status') == 'not_run' for side in ('before', 'after')):
        return 'not_run'
    if entry.get('status') in _ACCEPTABLE:
        return 'passed'
    if entry.get('status') in _FAILED:
        return 'failed'
    return 'blocked'


def _test_file(before, after, identity, common):
    path = parse_test_target(identity)
    left = _hash((before.get('shared_tests') or {}).get(path))
    right = _hash((after.get('shared_tests') or {}).get(path))
    same = _same(left, right)
    return {'path': path, 'before_sha256': left, 'after_sha256': right,
            'status': 'not_common' if not common else 'unknown' if same is None else 'same' if same else 'changed'}


def _state_change(old, new):
    """Describe actual state changes independently of check equivalence."""
    if old is None:
        return 'only_after'
    if new is None:
        return 'only_before'
    current, prior = _execution(new), _execution(old)
    if current in ('not_run', 'blocked'):
        return current
    if current == 'passed' and prior == 'failed':
        return 'became_passed'
    if current == 'passed' and prior in ('blocked', 'not_run'):
        return 'now_passed'
    if current == 'failed' and prior == 'failed':
        return 'still_failed'
    if current == 'failed' and prior == 'passed':
        return 'deteriorated'
    return 'unchanged' if old.get('status') == new.get('status') else 'changed'


_CONFIG_FILES = {'pytest.ini', '.pytest.ini', 'pytest.toml', '.pytest.toml',
                 'pyproject.toml', 'tox.ini', 'setup.cfg'}
CONDITION_LABELS = {'runtime_variables': '自定义运行变量（本机值不保存）', 'baseline': '固定基线', 'environment': '记录的运行环境',
                    'timeout': '每版时限', 'selection': '所选测试及顺序',
                    'test_support': '测试配置、fixture 与辅助材料', 'runner': 'ReproLab 执行版本'}


def _support_matcher(targets, plugins):
    configs, test_roots = set(), set()
    for name in targets:
        target = Path(name)
        ancestors = list(target.parents)
        root = next((parent for parent in reversed(ancestors)
                     if parent.name in ('tests', 'test', 'testing')), target.parent)
        if root != Path('.'):
            test_roots.add(root)
        configs.update((parent / name).as_posix()
                       for parent in ancestors for name in _CONFIG_FILES | {'conftest.py'})
    plugin_paths = [name.replace('.', '/') for name in plugins]

    def matches(relative):
        return relative not in targets and (
            relative in configs
            or any(Path(relative).is_relative_to(root) for root in test_roots)
            or any(relative in (path + '.py', 'src/' + path + '.py')
                   or relative.startswith((path + '/', 'src/' + path + '/')) for path in plugin_paths))
    return matches


def _support_manifest(report, identity):
    """Read test support from the checked evidence manifest."""
    hashes = (report.get('persistence') or {}).get('sha256')
    if not isinstance(hashes, dict) or not hashes:
        return None
    matches = _support_matcher({parse_test_target(identity)}, (report.get('pytest_profile') or {}).get('plugins') or [])
    prefix = 'evidence/project/inputs/'
    manifest = {}
    for name, digest in hashes.items():
        if name.startswith(prefix):
            side, _, relative = name[len(prefix):].partition('/')
            if side in ('before', 'after') and matches(relative):
                manifest[side + '/' + relative] = digest
    return manifest


def changed_candidate_support(report, snapshot, targets, plugins, *, control):
    """Compare relevant files in the frozen candidate with the checked report."""
    matches = _support_matcher(targets, plugins)
    prefix = 'evidence/project/inputs/after/'
    previous = {name[len(prefix):]: digest for name, digest in report['persistence']['sha256'].items()
                if name.startswith(prefix) and matches(name[len(prefix):])}
    current = {}
    for path in snapshot.rglob('*'):
        control.check()
        name = path.relative_to(snapshot).as_posix()
        if matches(name) and path.is_file():
            current[name] = _digest(path)
    return sorted(name for name in previous.keys() | current.keys() if previous.get(name) != current.get(name))


def _support_comparison(before, after, identity):
    left, right = _support_manifest(before, identity), _support_manifest(after, identity)
    same = _same(left, right)
    changed = [] if same is None else sorted(name for name in left.keys() | right.keys()
                                            if left.get(name) != right.get(name))
    return same, changed


def _change(old, new, test_file, conditions):
    state = _state_change(old, new)
    if old is None or new is None:
        return state
    if test_file['status'] != 'same':
        return 'test_changed' if test_file['status'] == 'changed' else 'test_unknown'
    if state == 'became_passed':
        if any(value is False for value in conditions.values()):
            return 'conditions_changed'
        if any(value is None for value in conditions.values()):
            return 'conditions_unknown'
        return 'resolved'
    return state


def compare_project_reports(before_directory, after_directory, *, expected_before=None, expected_after=None, control=None):
    before = _checked_report(before_directory, control=control) if control else _checked_report(before_directory)
    after = _checked_report(after_directory, control=control) if control else _checked_report(after_directory)
    if expected_before is not None:
        _match_reference(before, expected_before)
    if expected_after is not None:
        _match_reference(after, expected_after)
    return _compare_checked_reports(before, after)


def _compare_checked_reports(before, after, *, test_ids=None):
    if any(report.get('runner') in ('vitest', 'go') for report in (before, after)):
        raise ValueError('Go 和 Vitest 预览支持两版比较，暂不支持跨报告比较。')
    left = {entry['test_id']: entry for entry in before['entries']}
    right = {entry['test_id']: entry for entry in after['entries']}
    entries = []
    same_sources = _all_same([_same(_source_hash(before, key, side), _source_hash(after, key, side))
                              for key in ('source_sha256', 'source_mode_sha256') for side in ('before', 'after')])
    same_baseline = _baseline(before, after)
    same_environment = _same(_environment(before), _environment(after))
    same_variables = compare_variable_records(before, after)
    same_timeout = _same(_timeout(before), _timeout(after))
    same_selection = _same(before.get('selected_tests'), after.get('selected_tests'))
    same_runner = _same(before.get('reprolab_version'), after.get('reprolab_version'))
    context_cache = {}
    targets = left.keys() | right.keys()
    if test_ids is not None:
        targets = targets & set(test_ids)
    for identity in sorted(targets):
        old, new = left.get(identity), right.get(identity)
        test_file = _test_file(before, after, identity, old is not None and new is not None)
        path = parse_test_target(identity)
        if path not in context_cache:
            context_cache[path] = _support_comparison(before, after, identity)
        same_support, changed_files = context_cache[path]
        conditions = {'baseline': same_baseline, 'environment': same_environment,
                      'timeout': same_timeout, 'selection': same_selection, 'test_support': same_support,
                      'runner': same_runner, 'runtime_variables': same_variables}
        entries.append({'test_id': identity, 'change': _change(old, new, test_file, conditions),
                        'before': _entry(old), 'after': _entry(new), 'test_file': test_file,
                        'before_execution': _execution(old), 'after_execution': _execution(new),
                        'state_change': _state_change(old, new), 'conditions': conditions,
                        'changed_support_files': changed_files})
    notes = ['比较只读取校验后的历史证据，不重新执行，也不产生新的修复验收结论。',
             '仅前次选择的项目列为未复查；本轮未执行、受阻与实际失败分别显示。',
             '只有共享测试、基线、记录的环境、时限、选择、测试辅助材料及执行版本均相同，才计入相同已记录条件下转为通过。',
             '实际转绿单独保留；条件改变或记录不完整时需复核。仅复查部分测试也可能改变 fixture 和执行顺序。',
             '辅助材料按测试目录及配置文件保守核对；不代表已追踪所有动态依赖或外部服务状态。',
             '测试文件指纹按整文件核对；同文件其他测试的编辑也会标为变化。']
    for label, value in (('基线', same_baseline), ('记录的环境', same_environment), ('每版时限', same_timeout), ('选择及顺序', same_selection), ('ReproLab 执行版本', same_runner)):
        notes.append(label + ('不同，请结合条件变化复核。' if value is False else '未知：旧记录缺少可选信息。' if value is None else '相同。'))
    if same_variables is None:
        notes.append('本机运行变量值未保存或记录不足，不能确认跨轮相同；转绿仍需复核。')
    elif same_variables is False:
        notes.append('普通运行变量或本机变量名称已变化，请复核检查条件。')
    if same_sources is False:
        notes.append('源码快照已更新，这是工作区复查的正常输入变化；不会单独阻止比较。')
    counts = dict(Counter(item['change'] for item in entries))
    execution_counts = dict(Counter(item['after_execution'] for item in entries))
    state_counts = dict(Counter(item['state_change'] for item in entries))
    summary = (f'共同选中 {len(left.keys() & right.keys())} 项 · 前次 {len(left)} 项 · 本次 {len(right)} 项\n'
               + ' · '.join(f'{CHANGE_LABELS[key]} {counts.get(key, 0)}' for key in
                            ('resolved', 'conditions_changed', 'conditions_unknown', 'test_changed', 'test_unknown', 'only_before', 'only_after')
                            if counts.get(key) or key == 'resolved')
               + f'\n实际仍失败 {state_counts.get("still_failed", 0)} 项 · '
               + ' · '.join(f'本轮{EXECUTION_LABELS[key]} {execution_counts.get(key, 0)} 项'
                            for key in ('passed', 'failed', 'blocked', 'not_run')))
    return {'schema_version': 3, 'kind': 'project_run_comparison',
            'before': {key: before.get(key) for key in ('run_id', 'project_id', 'title', 'created_at', 'report_dir', 'status')},
            'after': {key: after.get(key) for key in ('run_id', 'project_id', 'title', 'created_at', 'report_dir', 'status')},
            'scope': {'common': len(left.keys() & right.keys()), 'before_count': len(left), 'after_count': len(right),
                      'same_sources': same_sources, 'same_baseline': same_baseline,
                      'same_recorded_environment': same_environment, 'same_runtime_variables': same_variables, 'same_timeout': same_timeout,
                      'same_selection': same_selection, 'same_runner': same_runner},
            'counts': counts, 'same_check_resolved': counts.get('resolved', 0),
            'execution_counts': execution_counts, 'state_counts': state_counts,
            'entries': entries, 'notices': notes, 'summary': summary}


def compare_with_source(directory, *, source_directory=None, expected_report=None, control=None):
    report = read_report_snapshot(directory)
    reference = report.get('review_source')
    if reference is None:
        raise ValueError('此报告没有绑定来源运行，可在运行记录中明确选择两份报告比较。')
    validate_report_reference(reference)
    location = source_directory or source_report_directory(report, reference)
    if not Path(location).expanduser().exists():
        raise ValueError('来源报告本次未找到，请使用“重新定位来源报告”选择原报告目录。')
    return compare_project_reports(location, directory, expected_before=reference,
                                   expected_after=expected_report or report_reference(report), control=control)


def read_recheck_progress(directory, *, source_directory=None, expected_report=None, control=None):
    """Read-only presentation data. Failure to compare never rewrites a saved run verdict."""
    from .runner import RunControl, RunCancelled
    control = control or RunControl()
    empty = {'comparison': None, 'scope_summary': ''}
    try:
        control.check()
        report = read_report_snapshot(directory)
        if expected_report is not None:
            _match_reference(report, expected_report)
        if report.get('review_source') is None:
            return {**empty, 'status': 'not_applicable', 'summary': '此报告没有绑定复查来源。'}
        comparison = compare_with_source(directory, source_directory=source_directory,
            expected_report=expected_report or report_reference(report), control=control)
        control.check()
    except (RunCancelled, KeyboardInterrupt):
        return {**empty, 'status': 'cancelled', 'summary': '已停止核对复查进展；本次执行报告保留，尚未确认跨轮变化。'}
    except (OSError, ValueError) as exc:
        return {**empty, 'status': 'unavailable', 'summary': '暂不能确认复查进展：' + str(exc)}
    counts, states = comparison['counts'], comparison['state_counts']
    executions, scope = comparison['execution_counts'], comparison['scope']
    review = sum(counts.get(key, 0) for key in ('conditions_changed', 'conditions_unknown', 'test_changed', 'test_unknown'))
    parts = [f'同条件转绿 {comparison["same_check_resolved"]} 项']
    for label, count in (('检查需复核', review), ('仍失败', states.get('still_failed', 0)),
                         ('转为失败', states.get('deteriorated', 0)), ('受阻后通过', states.get('now_passed', 0))):
        if count:
            parts.append(f'{label} {count} 项')
    scope_parts = [f'复查原范围 {scope["common"]}/{scope["before_count"]} 项',
                   f'未复查 {counts.get("only_before", 0)} 项']
    for label, count in (('仅本轮选中', counts.get('only_after', 0)),
                         ('本轮受阻', executions.get('blocked', 0)), ('未完整执行', executions.get('not_run', 0))):
        if count:
            scope_parts.append(f'{label} {count} 项')
    return {'status': 'compared', 'comparison': comparison, 'summary': ' · '.join(parts),
            'scope_summary': ' · '.join(scope_parts)}
