"""Portable requirements for checking saved project reports."""
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from .persistence import atomic_write
from .project_selections import MAX_SELECTION_BYTES, reconcile_selection, validate_selection


GATE_POLICIES = ('strict', 'no-new-failures')
_KIND = 'project_acceptance_plan'
_MAX_BYTES = 2 * 1024 * 1024
_FIELDS = {'kind', 'schema_version', 'runner', 'policy', 'required_tests',
           'require_receipt', 'min_stability_rounds'}
_BASELINE_FIELDS = {'source_sha256', 'source_mode_sha256'}


@dataclass(frozen=True)
class BaselinePin:
    source_sha256: str
    source_mode_sha256: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
               for value in (self.source_sha256, self.source_mode_sha256)):
            raise ValueError('基线锁定须包含源码和普通文件权限的 SHA-256。')

    def record(self):
        return {'source_sha256': self.source_sha256, 'source_mode_sha256': self.source_mode_sha256}


def baseline_pin(report):
    source = (report.get('source_sha256') or {}).get('before')
    modes = (report.get('source_mode_sha256') or {}).get('before')
    return BaselinePin(source, modes) if source is not None and modes is not None else None


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('验收计划含重复字段。')
    return fields


@dataclass(frozen=True)
class AcceptancePlan:
    runner: str
    policy: str
    targets: tuple
    require_receipt: bool = False
    min_stability_rounds: int | None = None
    baseline: BaselinePin | None = None
    required_passes: tuple = ()
    approved_selection: tuple | None = None
    protected_inputs: dict | None = None
    test_contract: dict | None = None

    def __post_init__(self):
        if self.baseline is not None and not isinstance(self.baseline, BaselinePin):
            raise ValueError('验收计划的基线锁定无效。')
        if self.runner not in ('pytest', 'go', 'vitest'):
            raise ValueError('验收计划的执行器须为 pytest、go 或 vitest。')
        if self.policy not in GATE_POLICIES:
            raise ValueError('验收计划的回归门槛无效。')
        if type(self.require_receipt) is not bool:
            raise ValueError('require_receipt 须为 true 或 false。')
        if self.min_stability_rounds is not None:
            from .replay_stability import MAX_ROUNDS, MIN_ROUNDS

            if type(self.min_stability_rounds) is not int or not MIN_ROUNDS <= self.min_stability_rounds <= MAX_ROUNDS:
                raise ValueError(f'最少稳定性轮数须为 {MIN_ROUNDS}–{MAX_ROUNDS}。')
        goals = (*self.targets, *self.required_passes)
        if self.approved_selection is not None:
            values = list(self.approved_selection)
            if self.runner == 'go':
                from .go_project import _tests
                validated = _tests(values)
            elif self.runner == 'vitest':
                from .vitest_project import _test_files
                validated = _test_files(values)
            else:
                validated = validate_selection(values)
            if tuple(validated) != tuple(self.approved_selection):
                raise ValueError('已批准范围须包含唯一且规范的目标。')
        if self.runner == 'vitest':
            valid = all(isinstance(target, tuple) and len(target) == 2 and all(map(_text, target))
                        for target in goals)
        else:
            valid = all(map(_text, goals))
        if not valid:
            raise ValueError('验收计划须包含完整测试 ID。Vitest 目标须包含文件和用例名。')
        if len(set(goals)) != len(goals):
            raise ValueError('验收计划含重复测试目标。')

        if self.protected_inputs is not None:
            from ._input_samples import sample_digests

            protected = self.protected_inputs
            fields = {'source_report_sha256', 'control_report_sha256', 'values'}
            if (self.runner != 'pytest' or not isinstance(protected, dict) or protected.keys() != fields
                    or any(not isinstance(protected[key], str) or not re.fullmatch(r'[0-9a-f]{64}', protected[key])
                           for key in fields - {'values'}) or not protected['values']):
                raise ValueError('固定失败样本须包含 pytest 来源、对照报告指纹和业务输入。')
            sample_digests(protected['values'], self.required_passes)
            validate_selection(list(protected['values']))

        if self.test_contract is not None:
            from .project_test_contract import validate_test_contract

            if self.protected_inputs is None:
                raise ValueError('测试契约须配合固定失败样本。')
            validate_test_contract(self.test_contract, self.required_passes)

    def fixed_input_record(self, selected):
        if self.protected_inputs is None:
            return None
        values = self.protected_inputs['values']
        chosen = {identity: values[identity] for identity in selected if identity in values}
        if not chosen:
            return None
        return {'source_report_sha256': self.protected_inputs['source_report_sha256'],
                'plan_sha256': self.fingerprint(), 'values': chosen}

    def record(self):
        tests = self.targets
        if self.runner == 'vitest':
            tests = [{'file': file, 'case': case} for file, case in self.targets]
        record = {'kind': _KIND, 'schema_version': 1, 'runner': self.runner, 'policy': self.policy,
                  'required_tests': list(tests), 'require_receipt': self.require_receipt,
                  'min_stability_rounds': self.min_stability_rounds}
        if self.baseline is not None:
            record.update(schema_version=2, baseline=self.baseline.record())
        if self.required_passes or self.approved_selection is not None or self.protected_inputs is not None:
            passes = ([{'file': file, 'case': case} for file, case in self.required_passes]
                      if self.runner == 'vitest' else list(self.required_passes))
            record.update(schema_version=3, baseline=self.baseline.record() if self.baseline else None,
                          required_passes=passes, approved_selection=list(self.approved_selection)
                          if self.approved_selection is not None else None)
        if self.protected_inputs is not None:
            record.update(schema_version=4, protected_inputs=deepcopy(self.protected_inputs))
        if self.test_contract is not None:
            record.update(schema_version=5, test_contract=deepcopy(self.test_contract))
        return record

    def fingerprint(self):
        content = json.dumps(self.record(), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


class PlanSelectionBlocked(ValueError):
    def __init__(self, review):
        super().__init__(review['notice'])
        self.review = review


def require_plan_runner(plan, runner):
    if plan.runner != runner:
        raise ValueError(f'此入口需要 {runner} 验收计划，所选计划使用 {plan.runner}。')


def execution_selection(plan, runner):
    require_plan_runner(plan, runner)
    goals = (*plan.targets, *plan.required_passes)
    targets = (tuple(file for file, _ in goals) if runner == 'vitest' else goals)
    selection = list(dict.fromkeys([*(plan.approved_selection or ()), *targets]))
    if not selection:
        raise ValueError('计划没有指定测试目标，请添加目标后再用于测试选择。')
    return selection


def pytest_plan_targets(plan):
    return validate_selection(execution_selection(plan, 'pytest'))


def plan_matches_baseline(plan, state):
    return plan.baseline is None or baseline_pin(state) == plan.baseline


def check_plan_scope(plan, state, selected, *, available=None, **scope):
    """Check a frozen Go or Vitest selection before running the two versions."""
    if not plan_matches_baseline(plan, state):
        status = 'baseline_mismatch'
        notice = '计划的基线与本次冻结的基线不符。请使用原基线。'
        missing = []
    else:
        missing = [] if available is None else [name for name in selected if name not in available]
        if not missing:
            return
        status = 'missing_targets'
        notice = f'有 {len(missing)} 项所选测试未在本次范围中找到。'
    raise PlanSelectionBlocked({
        'status': status, 'ready': False, 'runner': plan.runner,
        'before_commit': state['before_commit'], 'after_commit': state['after_commit'],
        'acceptance_plan_sha256': plan.fingerprint(), 'notice': notice,
        'requested_tests': selected, 'missing_tests': missing, **scope,
    })


def review_pytest_plan(plan, state, collection, *, selected=None):
    """Match the execution scope against one saved discovery record."""
    if selected is None:
        selected = pytest_plan_targets(plan)
    complete = bool(collection and collection.get('status') == 'collected'
                    and not (collection.get('partial') or collection.get('truncated')))
    available = collection['tests'] if collection and collection.get('status') == 'collected' else []
    review = reconcile_selection(selected, available, collection_complete=complete)
    if not plan_matches_baseline(plan, state):
        status, notice = 'baseline_mismatch', '计划的基线与当前会话不符。请打开或准备使用原基线的会话。'
    elif collection is None:
        status, notice = 'needs_collection', '当前会话尚无可核对的测试发现记录。请先发现测试。'
    elif not complete:
        status, notice = 'incomplete_collection', '测试发现不完整。请处理收集问题并重新发现测试。'
    elif review['missing_tests']:
        status = 'missing_targets'
        notice = f'有 {review["missing_count"]} 项所选测试未在本次发现中找到，请核对测试 ID 和发现范围。'
    else:
        status, notice = 'matched', '本次所选测试已全部匹配。'
    return {**review, 'status': status, 'ready': status == 'matched', 'notice': notice,
            'project_directory': state['directory'], 'project_id': state['project_id'],
            'acceptance_plan_sha256': plan.fingerprint()}


def read_acceptance_plan(location):
    path = Path(location).expanduser()
    if not path.is_file():
        raise ValueError('请选择验收计划 JSON 文件。')
    with path.open('rb') as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError('验收计划超过 2 MiB。')
    try:
        data = json.loads(raw, object_pairs_hook=_unique_fields)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('验收计划须为有效 JSON。') from None
    if not isinstance(data, dict) or data.get('kind') != _KIND or type(data.get('schema_version')) is not int:
        raise ValueError('验收计划的字段或版本无效，请检查计划格式。')
    version = data['schema_version']
    persistent_fields = _FIELDS | {'baseline', 'required_passes', 'approved_selection'}
    expected_fields = {1: _FIELDS, 2: _FIELDS | {'baseline'}, 3: persistent_fields,
                       4: persistent_fields | {'protected_inputs'},
                       5: persistent_fields | {'protected_inputs', 'test_contract'}}.get(version)
    if data.keys() != expected_fields:
        raise ValueError('验收计划的字段或版本无效，请检查计划格式。')
    baseline = None
    if version == 2 or data.get('baseline') is not None:
        fields = data['baseline']
        if not isinstance(fields, dict) or fields.keys() != _BASELINE_FIELDS:
            raise ValueError('验收计划的基线锁定字段无效。')
        baseline = BaselinePin(**fields)

    def read_goals(field):
        tests = data.get(field, [])
        if not isinstance(tests, list):
            raise ValueError(field + ' 须为数组。')
        if data['runner'] == 'vitest':
            if any(not isinstance(item, dict) or item.keys() != {'file', 'case'} for item in tests):
                raise ValueError('Vitest 验收目标须包含 file 和 case。')
            tests = [(item['file'], item['case']) for item in tests]
        return tuple(tests)

    selection = data.get('approved_selection')
    if selection is not None and not isinstance(selection, list):
        raise ValueError('approved_selection 须为数组或 null。')
    selection = tuple(selection) if selection is not None else None
    protected = data.get('protected_inputs')
    if version in (4, 5) and protected is None:
        raise ValueError('该计划版本须包含固定失败样本。')
    contract = data.get('test_contract')
    if version == 5 and contract is None:
        raise ValueError('版本 5 的计划须包含测试契约。')
    return AcceptancePlan(data['runner'], data['policy'], read_goals('required_tests'), data['require_receipt'],
                          data['min_stability_rounds'], baseline, read_goals('required_passes'), selection, protected, contract)


def _plan_json(plan):
    content = json.dumps(plan.record(), ensure_ascii=False, indent=2).encode('utf-8')
    if len(content) > _MAX_BYTES:
        raise ValueError('验收计划超过 2 MiB。')
    return content


def save_acceptance_plan(plan, location):
    try:
        atomic_write(Path(location).expanduser(), _plan_json(plan), replace=False)
    except FileExistsError:
        raise ValueError('该文件已存在，请选择新的验收计划文件名。') from None


def promote_plan(location, plan_path, destination, *, input_control=None, receipt=None, stability=None, control=None):
    from .project_history import _checked_report, report_reference
    from .project_review_summary import review_summary
    from .replay_verification import open_replay_evidence

    plan = read_acceptance_plan(plan_path)
    destination = Path(destination).expanduser().absolute()
    protected = plan.protected_inputs
    with open_replay_evidence(location, control=control) as source:
        report = _checked_report(source, control=control)
        if destination.resolve().is_relative_to(Path(report['report_dir'])):
            raise ValueError('请将持续保护计划保存到来源报告之外。')
        settings = dict(plan=plan, receipt=receipt, stability=stability,
                        expected_report=report_reference(report), control=control)
        summary = review_summary(source, **settings)
        if summary['gate']['status'] == 'failed':
            return summary, 1
        if input_control is not None:
            from .project_input_control import inputs_for_promotion

            with open_replay_evidence(input_control, control=control) as evidence:
                checked = _checked_report(evidence, control=control)
                if destination.resolve().is_relative_to(Path(checked['report_dir'])):
                    raise ValueError('请将持续保护计划保存到对照报告之外。')
                promotable, proof, code = inputs_for_promotion(report, checked, plan)
            if code:
                return {**summary, 'input_promotion': proof}, code
            protected = promotable
            summary = review_summary(source, _reviewed_inputs=frozenset(protected['values']), **settings)
            summary['input_promotion'] = proof
    if summary['gate']['status'] != 'passed':
        return summary, summary['gate']['exit_code']
    promoted = replace(plan, targets=(), baseline=None, required_passes=(*plan.required_passes, *plan.targets),
                       approved_selection=tuple(report['selected_tests']), protected_inputs=protected)
    if control:
        control.commit()
    save_acceptance_plan(promoted, destination)
    return {'status': 'saved', 'output': str(destination), 'report_sha256': summary['report_sha256'],
            'source_plan_sha256': plan.fingerprint(), 'acceptance_plan_sha256': promoted.fingerprint(),
            'approved_count': len(promoted.approved_selection), 'required_passes': len(promoted.required_passes),
            **({'input_promotion': summary['input_promotion']} if 'input_promotion' in summary else {})}, 0


def export_ci_config(reference, plan, destination, *, control):
    """Export the checked report scope together with the chosen acceptance plan."""
    from .project_history import _checked_report, _match_reference

    target = Path(destination).expanduser().absolute()
    directory = Path(reference['report_dir']).resolve()
    if target.resolve().is_relative_to(directory):
        raise ValueError('请将 CI 配置保存到报告目录之外。')
    if target.exists() or target.is_symlink():
        raise ValueError('该文件已存在，请选择新的 CI 配置文件名。')
    report = _checked_report(directory, control=control)
    _match_reference(report, reference)
    require_plan_runner(plan, report.get('runner', 'pytest'))
    selection = json.dumps(report['selected_tests'], ensure_ascii=False, indent=2).encode('utf-8')
    if len(selection) > MAX_SELECTION_BYTES:
        raise ValueError('测试范围超过 8 MiB，无法导出 CI 配置。')

    data = BytesIO()
    with ZipFile(data, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('regression-tests.json', selection)
        archive.writestr('acceptance-plan.json', _plan_json(plan))
    control.commit()
    try:
        atomic_write(target, data.getvalue(), replace=False)
    except FileExistsError:
        raise ValueError('该文件已存在，请选择新的 CI 配置文件名。') from None
    return target
