"""Link an explicit issue or incident to reviewed long-term verification evidence."""
import hashlib
import json
from pathlib import Path

from .acceptance_plan import read_acceptance_plan
from .persistence import atomic_write
from .project_history import _checked_report
from .project_review_summary import review_summary
from .project_selections import validate_selection
from .replay_verification import open_replay_evidence

_KIND = 'reprolab_case_contract'


def _read(path):
    path = Path(path).expanduser()
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('请选择 2 MiB 以内的案例记录。')
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _case(location):
    value, digest = _read(location)
    if (not isinstance(value, dict) or value.keys() != {'schema_version', 'kind', 'id', 'expected', 'tests'}
            or value['schema_version'] != 1 or value['kind'] not in ('issue', 'incident')
            or not isinstance(value['id'], str) or not value['id'].strip() or len(value['id']) > 200
            or not isinstance(value['expected'], str) or not value['expected'].strip() or len(value['expected']) > 10000
            or validate_selection(value['tests']) != value['tests']):
        raise ValueError('案例须明确记录 issue/incident、ID、预期行为与完整测试 ID。')
    return value, digest


def _derive(case_path, report_path, plan_path, failure, *, control):
    case, digest = _case(case_path)
    plan = read_acceptance_plan(plan_path)
    if plan.runner != 'pytest' or not set(case['tests']) <= set(plan.required_passes):
        raise ValueError('每条案例测试都须纳入 pytest 计划的持续通过目标。')
    failure_digest = None
    if case['kind'] == 'incident':
        if not failure or not plan.protected_inputs or not set(case['tests']) <= plan.protected_inputs['values'].keys():
            raise ValueError('事故契约需要失败来源，以及覆盖全部案例测试的固定失败样本。')
        from .project_input_control import select_failure_inputs

        with open_replay_evidence(failure, control=control) as source:
            original = _checked_report(source, control=control)
            witness = select_failure_inputs(original, case['tests'])
        if (witness['source_report_sha256'] != plan.protected_inputs['source_report_sha256']
                or any(value != plan.protected_inputs['values'][target] for target, value in witness['values'].items())):
            raise ValueError('事故原始失败与方案中的样本不符。')
        failure_digest = witness['source_report_sha256']
    summary = review_summary(report_path, plan=plan, control=control)
    code = summary['gate']['exit_code']
    if code:
        return {'status': summary['gate']['status'], 'gate': summary['gate']}, code
    return {'kind': _KIND, 'schema_version': 1, 'case_kind': case['kind'], 'id': case['id'],
            'case_sha256': digest, 'plan_sha256': plan.fingerprint(),
            'report_sha256': summary['report_sha256'], 'failure_sha256': failure_digest,
            'test_count': len(case['tests']),
            'notice': '契约绑定外部案例、已通过的计划及实际证据。预期行为由维护者声明。'}, 0


def link_case_contract(case, report, plan, output, *, failure, control):
    result, code = _derive(case, report, plan, failure, control=control)
    if code:
        return result, code
    target = Path(output).expanduser().absolute()
    source = Path(report).expanduser().resolve()
    if source.is_dir() and target.resolve().is_relative_to(source):
        raise ValueError('请将案例契约保存到报告之外。')
    control.commit()
    atomic_write(target, json.dumps(result, ensure_ascii=False, indent=2), replace=False)
    return {**result, 'status': 'linked', 'output': str(target)}, 0


def check_case_contract(location, case, report, plan, *, failure, control):
    saved, _ = _read(location)
    result, code = _derive(case, report, plan, failure, control=control)
    if code or saved != result:
        raise ValueError('案例记录、原始报告、计划或事故失败与契约不一致。')
    return {**result, 'integrity': 'consistent'}
