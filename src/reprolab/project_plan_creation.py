"""Create an existing-format acceptance plan from explicitly reviewed pytest IDs."""
from pathlib import Path

from .acceptance_plan import AcceptancePlan, PlanSelectionBlocked, baseline_pin, review_pytest_plan, save_acceptance_plan
from .project_selections import read_selection, validate_selection
from .project_suggest import project_command


def _complete_discovery(collection):
    if collection is None:
        return False
    tests = collection['tests']
    # A usable list can still be partial. Verify the positive evidence that
    # collection finished and that no recorded IDs were lost.
    return (collection.get('partial') is False and collection.get('truncated') is False
            and not collection.get('scope_mismatch') and collection.get('worker_record_complete') is True
            and collection.get('worker_status') == 'completed'
            and type(collection.get('exit_code')) is int and collection['exit_code'] == 0
            and collection.get('collection_errors') == [] and type(collection.get('count')) is int
            and all(isinstance(identity, str) for identity in tests)
            and collection['count'] == len(tests) == len(set(tests)))


def _saved_evidence_root(directory):
    from .project_sessions import _read

    for name, kind in (('project.json', 'project_session'), ('report.json', 'project_regression')):
        marker = directory / name
        if not marker.is_file():
            continue
        try:
            record = _read(marker)
        except (OSError, ValueError):
            continue
        if record.get('kind') == kind and record.get('schema_version') == 1:
            return True
    return False


def _git_metadata(repository):
    item = repository / '.git'
    if item.is_file():
        with item.open('rb') as stream:
            pointer = stream.read(4097)
        if len(pointer) <= 4096 and pointer.startswith(b'gitdir: '):
            return (repository / pointer[8:].strip().decode('utf-8')).resolve()
    return item.resolve()


def _plan_destination(path, state):
    target = Path(path).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('该文件已存在，请选择新的验收计划文件名。')
    resolved = target.resolve()
    repository = Path(state['repository']).resolve()
    git_dir = _git_metadata(repository)
    if ('.git' in {part.casefold() for part in (*target.parts, *resolved.parts)}
            or resolved.is_relative_to(git_dir)
            or resolved.is_relative_to(Path(state['directory']))
            or any(_saved_evidence_root(parent) for parent in resolved.parents)):
        raise ValueError('请将计划保存到 Git 元数据、冻结会话和报告证据目录之外。')
    return resolved


def _report_destination(target, state):
    repository = Path(state['repository']).resolve()
    if not target.is_relative_to(repository):
        return target.parent / 'reports'
    session = Path(state['directory'])
    output = session.parent.parent if session.parent.name == 'sessions' else session.parent
    reports = (output / 'reports').resolve()
    if reports.is_relative_to(repository) or reports.is_relative_to(session):
        raise ValueError('固定会话的默认报告路径须位于被测仓库和冻结会话之外。')
    return reports


def plan_from_selection(directory, selection_file, destination, *, required_tests=(), policy='strict', control=None):
    """Check only saved evidence and publish a new version 3 acceptance plan."""
    from .project_sessions import _collection, read_project

    if control:
        control.check()
    selected = read_selection(selection_file, require_unique=True)
    required = list(required_tests)
    targets = validate_selection(required, allow_empty=True)
    if len(targets) != len(required):
        raise ValueError('必验改善目标含重复测试 ID。')
    selected_set = set(selected)
    if outside := [identity for identity in targets if identity not in selected_set]:
        raise ValueError('必验改善目标须属于明确审阅的范围：' + ', '.join(outside))
    state = read_project(directory)
    if state.get('runner', 'pytest') != 'pytest':
        raise ValueError('此入口只接受 pytest 会话。')
    pin = baseline_pin(state)
    if pin is None:
        raise ValueError('会话没有可核验的冻结基线。')
    plan = AcceptancePlan('pytest', policy, tuple(targets), baseline=pin, approved_selection=tuple(selected))
    collection = _collection(state, state['runtime'])
    if collection is not None and not _complete_discovery(collection):
        raise ValueError('保存的测试发现缺少完整性证据。请先重新发现，再核对审阅清单。')
    review = review_pytest_plan(plan, state, collection, selected=selected)
    if not review['ready']:
        raise PlanSelectionBlocked(review)

    target = _plan_destination(destination, state)
    reports = _report_destination(target, state)
    if control:
        control.commit()
    save_acceptance_plan(plan, target)
    return {
        'kind': 'project_acceptance_plan_created', 'status': 'saved',
        'output': str(target), 'project_directory': state['directory'], 'project_id': state['project_id'],
        'before_commit': state['before_commit'], 'baseline': pin.record(),
        'collection_path': collection.get('collection_path', ''), 'collection_sha256': state['collection_sha256'],
        'policy': plan.policy, 'approved_selection': selected, 'required_tests': targets,
        'acceptance_plan_sha256': review['acceptance_plan_sha256'],
        'run_command': project_command('run', state['directory'], '--acceptance-plan', target,
                                       '--verify-plan', '--output', reports),
        'offline_summary_arguments': [
            'summary', '<REPORT_OR_ZIP>', '--acceptance-plan', str(target),
            '--expect-plan-sha256', review['acceptance_plan_sha256'],
        ],
        'notice': '计划记录审阅范围和验收条件。它不包含执行结果或签名。离线重算时在 reprolab project 后使用 offline_summary_arguments，并替换其中的报告占位符。',
    }
