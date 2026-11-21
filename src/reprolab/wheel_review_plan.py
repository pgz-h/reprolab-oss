"""Apply receiver-owned release requirements to saved wheel evidence."""
import hashlib
import json
from pathlib import Path
import re
import tempfile

from .acceptance_plan import _unique_fields
from .git_source import excluded_source_path, export_revision, resolve_commit
from .project_selections import MAX_SELECTION_BYTES
from .project_wheels import (
    MODULE_NAME, WheelEvidenceMismatch, _read_checked, _review_checked, _snapshot_record,
    wheel_artifact_mismatches,
)
from .runtime import PYTHON_ENVIRONMENT_FIELDS, valid_python_environment
from .wheel_archive import open_wheel_evidence
from .wheel_install import INSTALL_CHECK_KIND, wheelhouse_manifest_sha256
from .wheel_results import recorded_test_context, wheel_requirements

_KIND = 'reprolab_wheel_review_plan'
_FIELDS = {'kind', 'schema_version', 'wheel_artifacts', 'downstream_commit', 'module', 'selected_tests', 'gate'}
_OPTIONAL_FIELDS = {'required_improvements', 'required_install_environment', 'required_test_environment',
                    'required_install_wheelhouse_sha256', 'required_test_wheelhouse_sha256'}
_INSTALL_CHECK_FIELDS = {'kind', 'schema_version', 'wheel', 'environment', 'wheelhouse', 'steps',
                         'status', 'exit_code', 'reason'}
_MAX_INSTALL_CHECK = 256 * 1024
_SIDES = ('before', 'after')
CASE_GATES = ('regression', 'no-new-failures')


def read_wheel_review_plan(path):
    if not path.is_file() or path.stat().st_size > MAX_SELECTION_BYTES:
        raise ValueError('wheel 验收计划须为不超过 8 MiB 的 UTF-8 JSON 文件。')
    with path.open('rb') as stream:
        raw = stream.read(MAX_SELECTION_BYTES + 1)
    if len(raw) > MAX_SELECTION_BYTES:
        raise ValueError('wheel 验收计划超过 8 MiB。')
    plan = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_fields)
    if (not isinstance(plan, dict) or plan.keys() - _OPTIONAL_FIELDS != _FIELDS
            or plan['kind'] != _KIND or type(plan['schema_version']) is not int or plan['schema_version'] != 1):
        raise ValueError('wheel 验收计划需要受支持的 kind、schema_version 和规定字段。')
    if not isinstance(plan['downstream_commit'], str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', plan['downstream_commit']):
        raise ValueError('wheel 验收计划的 downstream_commit 须为完整 Git 提交。')
    if not isinstance(plan['module'], str) or not MODULE_NAME.fullmatch(plan['module']):
        raise ValueError('wheel 验收计划的 module 须为正常 Python 模块名。')
    if plan['gate'] not in CASE_GATES:
        raise ValueError('wheel 验收计划的 gate 须为 regression 或 no-new-failures。')
    wheels = plan['wheel_artifacts']
    if not isinstance(wheels, dict) or wheels.keys() != set(_SIDES):
        raise ValueError('wheel 验收计划的 wheel_artifacts 须包含 before 和 after。')
    for side in _SIDES:
        wheel = wheels[side]
        if (not isinstance(wheel, dict) or wheel.keys() != {'filename', 'sha256'}
                or not isinstance(wheel['filename'], str)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.+!-]*\.whl', wheel['filename'])
                or not isinstance(wheel['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', wheel['sha256'])):
            raise ValueError(f'wheel_artifacts.{side} 须提供 wheel 文件名和小写十六进制 SHA-256。')
    for field in ('selected_tests', 'required_improvements'):
        values = plan.get(field, [])
        if (not isinstance(values, list) or (field == 'selected_tests' and not values)
                or any(not isinstance(value, str) or not value.partition('::')[2]
                       or re.search(r'[\ud800-\udfff]', value) for value in values)):
            raise ValueError(f'wheel 验收计划的 {field} 须为完整 pytest 选择器列表。')
        try:
            wheel_requirements(values)
        except ValueError:
            raise ValueError(f'wheel 验收计划的 {field} 包含无效或重复的选择器。') from None
    for field in ('required_install_environment', 'required_test_environment'):
        if field in plan and not valid_python_environment(plan[field]):
            raise ValueError(f'{field} 须指定实际 Python 完整版本、实现、平台和架构。')
    if 'required_install_wheelhouse_sha256' in plan:
        digest = plan['required_install_wheelhouse_sha256']
        if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or 'required_install_environment' not in plan):
            raise ValueError('required_install_wheelhouse_sha256 须为小写 SHA-256，并与 required_install_environment 同用。')
    if 'required_test_wheelhouse_sha256' in plan:
        digest = plan['required_test_wheelhouse_sha256']
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('required_test_wheelhouse_sha256 须为小写 SHA-256。')
    return plan, hashlib.sha256(raw).hexdigest()


def _scope_mismatches(record, plan):
    fields = [key for key in ('downstream_commit', 'module', 'selected_tests') if record.get(key) != plan[key]]
    return fields + wheel_artifact_mismatches(record['wheel_artifacts'], plan['wheel_artifacts'])


def _verify_git(snapshot, recorded, repository, commit, *, control):
    control.check()
    try:
        if resolve_commit(repository, commit, local_only=True) != commit:
            raise ValueError('Expected an exact Git commit.')
    except (OSError, ValueError):
        return 'git_commit_unavailable', ['downstream_commit']
    with tempfile.TemporaryDirectory(prefix='reprolab-wheel-git-') as temporary:
        expected = Path(temporary) / 'downstream'
        try:
            export_revision(repository, commit, expected, local_only=True)
            exported = _snapshot_record(expected)
        except (OSError, ValueError):
            return 'git_snapshot_unavailable', ['downstream']
    control.check()
    fields = [f'downstream.{key}' for key, value in exported.items() if recorded[key] != value]
    if any(excluded_source_path(path.relative_to(snapshot)) for path in snapshot.rglob('*')):
        fields.append('downstream.entries')
    return ('git_snapshot_mismatch', fields) if fields else ('matched', [])


def _verify_install_check(path, evidence, *, wheel, environment, wheelhouse_sha256):
    if path is None:
        return 'install_check_missing', ['install_check'], None
    try:
        path = path.expanduser().resolve()
        if path.is_relative_to(evidence):
            return 'install_check_inside_evidence', ['install_check'], None
        if not path.is_file() or path.stat().st_size > _MAX_INSTALL_CHECK:
            raise ValueError('Invalid installation report file.')
        with path.open('rb') as stream:
            raw = stream.read(_MAX_INSTALL_CHECK + 1)
        if len(raw) > _MAX_INSTALL_CHECK:
            raise ValueError('Installation report too large.')
        report = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_fields)
        if (not isinstance(report, dict) or report.keys() != _INSTALL_CHECK_FIELDS
                or report['kind'] != INSTALL_CHECK_KIND or type(report['schema_version']) is not int
                or report['schema_version'] != 1 or not isinstance(report['wheel'], dict)
                or report['wheel'].keys() != wheel.keys() or not isinstance(report['environment'], dict)
                or not isinstance(report['steps'], dict) or not isinstance(report['wheelhouse'], list)):
            raise ValueError('Invalid installation report format.')
    except (OSError, ValueError, RuntimeError):
        return 'install_check_unavailable', ['install_check'], None
    if fields := [f'install_check.wheel.{key}' for key in ('filename', 'sha256') if report['wheel'][key] != wheel[key]]:
        return 'install_check_artifact_mismatch', fields, None

    def step_passed(key):
        step = report['steps'].get(key)
        return (isinstance(step, dict) and step.get('status') == 'completed'
                and type(step.get('exit_code')) is int and step['exit_code'] == 0)

    actual_environment = report['environment']
    if (report['status'] != 'passed' or type(report['exit_code']) is not int or report['exit_code'] != 0
            or report['reason'] is not None or actual_environment.get('isolated_venv') is not True
            or actual_environment.get('initial_distributions') != []
            or not all(step_passed(key) for key in ('install', 'pip-check'))):
        return 'install_check_not_passed', ['install_check'], None
    if fields := [f'install_check.environment.{key}' for key in PYTHON_ENVIRONMENT_FIELDS
                  if actual_environment.get(key) != environment[key]]:
        return 'install_check_environment_mismatch', fields, None
    installation = {
        'status': 'matched_passed', 'environment': environment, 'report_sha256': hashlib.sha256(raw).hexdigest(),
    }
    if wheelhouse_sha256 is not None:
        try:
            observed = wheelhouse_manifest_sha256(report['wheelhouse'])
        except ValueError:
            return 'install_check_wheelhouse_invalid', ['install_check.wheelhouse'], None
        if observed != wheelhouse_sha256:
            return 'install_check_wheelhouse_mismatch', ['install_check.wheelhouse'], None
        installation['wheelhouse_sha256'] = observed
    return None, [], installation


def _verify_test_wheelhouse(record, observations, expected_sha256):
    fields = []
    observed_sha256 = None
    reason = 'test_wheelhouse_mismatch'
    if 'test_wheelhouse' not in record:
        fields.append('test_wheelhouse')
        reason = 'test_installation_unavailable'
    else:
        try:
            observed_sha256 = wheelhouse_manifest_sha256(record['test_wheelhouse'])
        except ValueError:
            return 'test_wheelhouse_invalid', ['test_wheelhouse']
        if observed_sha256 != expected_sha256:
            fields.append('test_wheelhouse')
    for side, proof in observations.items():
        if proof is None or proof['mode'] != 'isolated_venv':
            fields.append(f'test_installation.{side}')
            reason = 'test_installation_unavailable'
        elif observed_sha256 is not None and proof['wheelhouse_sha256'] != observed_sha256:
            fields.append(f'test_installation.{side}.wheelhouse_sha256')
    return reason, fields


def _review_test_scope(record, phases, plan, receipt):
    contexts = {side: recorded_test_context(phases[side], record['runs'][side]) for side in _SIDES}
    expected_environment = plan.get('required_test_environment')
    observed = {side: context.get('environment') for side, context in contexts.items()}
    if expected_environment is not None or any(observed.values()):
        test_receipt = receipt.setdefault('test_environment', {})
        test_receipt['observed'] = observed
    if expected_environment is not None:
        test_receipt['required'] = expected_environment
        fields = []
        for side, environment in observed.items():
            if environment is None:
                fields.append(f'test_environment.{side}')
            else:
                fields.extend(f'test_environment.{side}.{key}' for key in PYTHON_ENVIRONMENT_FIELDS
                              if environment[key] != expected_environment[key])
        if fields:
            reason = 'test_environment_unavailable' if None in observed.values() else 'test_environment_mismatch'
            return reason, fields
        test_receipt['status'] = 'matched'

    expected_wheelhouse = plan.get('required_test_wheelhouse_sha256')
    observed_installs = {side: context.get('installation') for side, context in contexts.items()}
    if expected_wheelhouse is not None or any(observed_installs.values()):
        test_installation = {'observed': observed_installs}
        receipt['test_installation'] = test_installation
    if expected_wheelhouse is not None:
        test_installation['required_wheelhouse_sha256'] = expected_wheelhouse
        reason, fields = _verify_test_wheelhouse(record, observed_installs, expected_wheelhouse)
        if fields:
            return reason, fields
        test_installation['status'] = 'matched'
    return None, []


def review_wheel_functional_execution(directory, plan, digest, *, include_ids=False, control):
    """Apply the original sender plan to the staged comparison and actual test phases."""
    record, snapshot, documents, phases = _read_checked(directory, control)
    result = _review_checked(record, snapshot, documents, phases, policy=plan['gate'],
                             required=plan.get('required_improvements', ()), include_ids=include_ids)
    receipt = {**result['comparison']['gate']}
    if 'required_install_environment' in plan:
        receipt['receiver_install_check_required'] = True
    reason, fields = _review_test_scope(record, phases, plan, receipt)
    if fields:
        receipt.update(status='blocked', exit_code=2, reason=reason, mismatched_fields=fields)
    result['functional_review_plan_sha256'] = digest
    result['functional_plan_execution'] = receipt
    return result, receipt['exit_code']


def review_wheel_release(location, plan_file, repository, *, install_check=None, include_ids=False, control):
    evidence = location.expanduser().resolve()
    plan_path = plan_file.expanduser().resolve()
    repo = repository.expanduser().resolve()
    if plan_path.is_relative_to(evidence) or repo.is_relative_to(evidence):
        raise ValueError('wheel 验收计划和本地 Git 仓库须由接收方在证据之外提供。')
    plan, digest = read_wheel_review_plan(plan_path)
    acceptance = {'plan_schema_version': plan['schema_version'], 'plan_sha256': digest, 'policy': plan['gate']}
    install_environment = plan.get('required_install_environment')
    test_environment = plan.get('required_test_environment')
    if install_check is not None and install_environment is None:
        raise ValueError('计划须包含 required_install_environment 才能接收安装检查报告。')
    if install_environment is not None:
        acceptance['required_install_environment'] = install_environment
    if test_environment is not None:
        acceptance['test_environment'] = {'required': test_environment}

    def blocked(reason, fields):
        return {'review_plan_acceptance': {**acceptance, 'status': 'blocked', 'exit_code': 2,
                                           'reason': reason, 'mismatched_fields': fields}}, 2

    with open_wheel_evidence(location, control=control) as directory:
        try:
            record, snapshot, documents, phases = _read_checked(directory, control)
        except WheelEvidenceMismatch as exc:
            return blocked('evidence_scope_inconsistent', exc.fields)
        if fields := _scope_mismatches(record, plan):
            return blocked('review_plan_scope_mismatch', fields)
        git_status, fields = _verify_git(snapshot, record['downstream'], repo, plan['downstream_commit'],
                                         control=control)
        if fields:
            return blocked(git_status, fields)
        acceptance.update(scope='matched', git_verification='matched_local_snapshot')
        reason, fields = _review_test_scope(record, phases, plan, acceptance)
        if fields:
            return blocked(reason, fields)
        if install_environment is not None:
            reason, fields, installation = _verify_install_check(
                install_check, evidence, wheel=plan['wheel_artifacts']['after'], environment=install_environment,
                wheelhouse_sha256=plan.get('required_install_wheelhouse_sha256'),
            )
            if fields:
                return blocked(reason, fields)
            acceptance['installation'] = installation
        result = _review_checked(record, snapshot, documents, phases, policy=plan['gate'],
                                 required=plan.get('required_improvements', ()), include_ids=include_ids)
    decision = result['comparison']['gate']
    result['review_plan_acceptance'] = {**acceptance, **decision}
    return result, decision['exit_code']
