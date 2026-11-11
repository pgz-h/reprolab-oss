"""Authored regressions for report-driven repairs. Pending authorized execution.

Synthetic records below test preparation contracts; the final tests explicitly
exercise real workers after the user opens runtime validation.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from reprolab import project_failure as flow
from reprolab.case import load_case
from reprolab.runner import RunCancelled, RunControl, evaluate, snapshot, source_fingerprint, source_mode_fingerprint

ID = 'tests/test_cart.py::test_money_display'
TEST = 'from cart import display\n\ndef test_money_display():\n    assert display(12) == "12.00"\n'


def observation():
    error = {'type': 'AssertionError', 'message': "assert '12.0' == '12.00'"}
    phases = [{'phase': phase, 'outcome': status, 'xfail': False,
               'exception': error if phase == 'call' else None}
              for phase, status in [('setup', 'passed'), ('call', 'failed'), ('teardown', 'passed')]]
    row = {'test_id': ID, 'status': 'regression_detected', 'before': {'status': 'passed'},
           'after': {'status': 'failed', 'exception': error, 'phases': phases}}
    report = {'status': 'regression_detected', 'after': {'status': 'completed', 'exit_code': 1,
        'worker_record_complete': True, 'timed_out': False, 'cancelled': False, 'collection_errors': []}}
    return row, report


@pytest.mark.parametrize('change, expected', [
    ('setup', 'fixture'), ('teardown', 'fixture'), ('dependency', '依赖'), ('skip', '跳过'),
    ('cancel', '取消'), ('timeout', '超时'), ('collection', '收集'), ('incomplete', '完整'), ('pass', '实际')])
def test_ineligible_results_have_actionable_reasons(change, expected):
    row, report = observation()
    if change in ('setup', 'teardown'):
        row['after']['phases'][0 if change == 'setup' else 2]['outcome'] = 'failed'
    elif change == 'dependency':
        row['after']['exception']['type'] = 'ModuleNotFoundError'
    elif change == 'skip':
        row['after']['status'] = 'skipped'
    elif change == 'cancel':
        report['status'] = 'cancelled'
    elif change == 'timeout':
        report['after']['timed_out'] = True
    elif change == 'collection':
        report['after']['collection_errors'] = ['missing module']
    elif change == 'incomplete':
        report['after']['worker_record_complete'] = False
    else:
        row['after']['status'] = 'passed'
    ready, reason = flow.repair_eligibility(row, report)
    assert not ready and expected in reason


def material():
    row, _ = observation()
    return {'reference': {'project_id': 'project', 'run_id': 'run', 'report_sha256': 'a' * 64},
        'test_id': ID, 'test_sha256': 'b' * 64, 'entry': row, 'created_at': '2026-11-04T00:00:00+00:00',
        'source_sha256': {'before': 'c' * 64, 'after': 'd' * 64}, 'diagnostics': ['先检查实际异常'],
        'path_roots': {'repository': '/private/project'}, 'blocks': {
            'test': {'title': '历史实际测试', 'text': TEST},
            'log:after': {'title': '历史日志', 'text': 'a' * (100 * 1024)},
            'diff:cart.py': {'title': '历史差异', 'text': '-old\n+new'}}}


def render(value, **options):
    return flow.render_repair_brief(value, problem='金额显示丢失末尾零', expected_behavior='固定两位小数',
                                   allowed_changes='cart.py 的显示逻辑，保持原断言', **options)


def test_long_material_omissions_edits_and_exports_are_exact(tmp_path):
    text = render(material(), selected_blocks=['test', 'log:after'])
    assert len(text.encode()) <= flow.MAX_CONTEXT_BYTES and '已截断' in text
    text = render(material(), selected_blocks=['test'])
    assert '明确省略：历史日志；历史差异' in text
    edited = text.replace(TEST, '[用户省略测试主体]\n') + '\n用户删减后的补充'
    saved = flow.export_repair_brief(tmp_path / 'repair.txt', edited)
    assert Path(saved['file']).read_bytes() == edited.encode('utf-8')
    assert saved['sha256'] == hashlib.sha256(edited.encode()).hexdigest()


def test_credentials_machine_roots_and_current_material_are_separate():
    value = material()
    value['blocks']['test']['text'] += '\napi_key = "sk-abcdefghijklmnop"\n{"password": "hidden-value"}\n/private/project/cart.py'
    text = render(value, current_material='当前业务函数已改为两位小数')
    assert 'abcdefghijklmnop' not in text and 'hidden-value' not in text and '/private/project' not in text
    assert '凭据' in text and '当前材料（用户另行提供，未经来源运行验证）' in text


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """Synthetic historical observation; preparation must never call a runtime."""
    folder, repo = tmp_path / 'source-report', tmp_path / 'candidate'
    old, failed = folder / 'evidence/project/inputs/before', folder / 'evidence/project/inputs/after'
    for directory, precision in ((old, 2), (failed, 1), (repo, 2)):
        (directory / 'tests').mkdir(parents=True)
        (directory / 'cart.py').write_text(f'def display(value):\n    return f"{{value:.{precision}f}}"\n')
        (directory / 'tests/test_cart.py').write_text(TEST)
    row, report = observation()
    report.update(kind='project_regression', schema_version=1, run_id='historical-run', project_id='project',
        report_dir=str(folder), _report_sha256='a' * 64, repository=str(repo), selected_tests=[ID], entries=[row],
        source_sha256={key: source_fingerprint(path)[1] for key, path in [('before', old), ('after', failed)]},
        source_mode_sha256={key: source_mode_fingerprint(path) for key, path in [('before', old), ('after', failed)]},
        shared_tests={'tests/test_cart.py': flow._digest(failed / 'tests/test_cart.py')},
        runtime={'executable': sys.executable}, created_at='2026-11-04T01:00:00+00:00')
    (folder / 'after.events.json').write_text(json.dumps({'collected_tests': [ID], 'started_tests': [ID]}))
    state = {'title': 'synthetic cart', 'repository': str(repo), 'created_at': '2026-11-04T00:00:00+00:00',
             'before_commit': 'b' * 40}
    monkeypatch.setattr(flow, '_checked_report', lambda directory, **kw: deepcopy(report))
    monkeypatch.setattr(flow, 'read_project', lambda directory: state)
    monkeypatch.setattr(flow, 'repository_root', lambda directory: Path(directory))
    def copy_current(source, destination, **kw):
        digest = snapshot(Path(source), destination, source_fingerprint(Path(source))[0], **kw)
        return {'sha256': digest, 'file_count': 2}
    monkeypatch.setattr(flow, 'copy_workspace', copy_current)
    return folder, repo, report


def prepare(prepared, tmp_path):
    folder, repo, report = prepared
    return flow.prepare_report_repair(folder, ID, tmp_path / 'preparations', expected_reference=flow.report_reference(report))


def save(plan, path, **options):
    return flow.save_report_repair(plan['directory'], path, problem='金额格式错误', expected_behavior='固定两位小数',
        failure_type='AssertionError', message_contains='12.00', confirmed=True,
        expected_preparation=plan['_preparation_sha256'], **options)


def test_preparation_uses_actual_failing_after_not_historical_passing_baseline(prepared, tmp_path):
    plan = prepare(prepared, tmp_path)
    assert plan['source_sha256']['before'] == prepared[2]['source_sha256']['after']
    assert plan['source_sha256']['before'] != prepared[2]['source_sha256']['before']
    assert plan['test_change']['status'] == 'same'
    result = save(plan, tmp_path / 'cases/repair.json')
    case = load_case(Path(result['case']))
    assert '.1f' in (case.before / 'cart.py').read_text() and '.2f' in (case.after / 'cart.py').read_text()
    assert case.test.read_text() == TEST
    assert case.failure_origin == {} and case.project_failure_origin['kind'] == 'project_report_failure'
    assert case.project_failure_origin['confirmation']['expected_behavior'] == '固定两位小数'


def test_weakened_same_test_is_visible_but_original_check_remains(prepared, tmp_path):
    folder, repo, _ = prepared
    (repo / 'cart.py').write_text('def display(value):\n    return f"{value:.1f}"\n')
    (repo / 'tests/test_cart.py').write_text(TEST.replace('== "12.00"', '.startswith("12")'))
    plan = prepare(prepared, tmp_path)
    assert plan['test_change']['status'] == 'changed' and 'startswith' in plan['test_change']['diff']
    case = load_case(Path(save(plan, tmp_path / 'cases/repair.json')['case']))
    assert case.test.read_text() == TEST and 'startswith' in (case.after / 'tests/test_cart.py').read_text()


def test_moved_source_and_save_failure_keep_preparation_retryable(prepared, tmp_path, monkeypatch):
    import reprolab.workspace as workspace
    plan = prepare(prepared, tmp_path)
    prepared[0].rename(tmp_path / 'moved-source')
    reopened = flow.read_repair_preparation(plan['directory'])
    case_path = tmp_path / 'cases/repair.json'
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('injected disk write failure')
        patch.setattr(workspace.json, 'dump', fail)
        with pytest.raises(OSError, match='injected'):
            save(reopened, case_path)
    assert not case_path.exists() and not case_path.with_suffix('.inputs').exists()
    assert flow.read_repair_preparation(plan['directory'])['_preparation_sha256'] == plan['_preparation_sha256']
    assert load_case(Path(save(reopened, case_path)['case'])).project_failure_origin['source_report']['run_id'] == 'historical-run'


def test_wrong_identity_evidence_changes_empty_confirmation_and_cancellation(prepared, tmp_path):
    folder, repo, report = prepared
    wrong = {**flow.report_reference(report), 'run_id': 'other-run'}
    with pytest.raises(ValueError, match='身份不符'):
        flow.prepare_report_repair(folder, ID, tmp_path / 'preparations', expected_reference=wrong)
    cancelled = RunControl()
    cancelled.cancel()
    with pytest.raises(RunCancelled):
        flow.prepare_report_repair(folder, ID, tmp_path / 'preparations', control=cancelled)
    assert not list((tmp_path / 'preparations').glob('repair-*'))
    plan = prepare(prepared, tmp_path)
    with pytest.raises(ValueError, match='稳定非空'):
        flow.save_report_repair(plan['directory'], tmp_path / 'case.json', problem='问题', expected_behavior='两位',
                                failure_type='AssertionError', message_contains=' ', confirmed=True)
    (Path(plan['directory']) / 'inputs/regression.py').write_text('assert True')
    with pytest.raises(ValueError, match='测试已变化'):
        flow.read_repair_preparation(plan['directory'])


def test_batch_history_does_not_override_current_not_reproduced():
    result = {'kind': 'passed', 'test_id': ID}
    assert evaluate(result, result, {'type': 'AssertionError', 'message_contains': '12.00'})[0] == 'not_reproduced'


@pytest.mark.parametrize('patch, expected', [('fix-money-display.patch', 'verified'),
                                          ('change-money-test.patch', 'still_failing')])
def test_actual_cart_repair_and_weakening_with_evidence_delivery(tmp_path, patch, expected):
    """Deferred: real project collection/execution, strict workers and replay."""
    import subprocess
    from reprolab.project_demo import create_project_demo
    from reprolab.project_sessions import collect_project, run_project
    from reprolab.runner import run_case
    from reprolab.evidence import check_report
    from reprolab.evidence_bundle import pack_report, unpack_report
    state = create_project_demo(tmp_path / 'sessions')
    collect_project(state['directory'])
    history = run_project(state['directory'], tmp_path / 'history', confirmed=True)
    from reprolab import __version__
    assert history['reprolab_version'] == __version__
    assert __version__ in (Path(history['report_dir']) / 'report.html').read_text()
    repository = Path(state['repository'])
    subprocess.run(['git', '-C', str(repository), 'apply', '--unidiff-zero',
                    str(repository / 'recheck-scenarios' / patch)], check=True, capture_output=True)
    plan = flow.prepare_report_repair(history['report_dir'], ID, tmp_path / 'preparations')
    assert plan['source_sha256']['before'] == history['source_sha256']['after']
    assert plan['test_change']['status'] == ('same' if expected == 'verified' else 'changed')
    saved = save(plan, tmp_path / 'cases/repair.json')
    shutil.move(history['report_dir'], tmp_path / 'moved-history')
    result = run_case(Path(saved['case']), tmp_path / 'strict')
    assert result['status'] == expected and result['before']['kind'] == 'failed'
    assert result['project_failure_origin']['source_report']['run_id'] == history['run_id']
    assert check_report(result['report_dir'])['status'] == 'consistent'
    bundle = tmp_path / 'delivery.zip'
    pack_report(result['report_dir'], bundle)
    imported = unpack_report(bundle, tmp_path / 'imported')
    replay_case = Path(imported['directory']) / 'evidence/case.json'
    assert load_case(replay_case).project_failure_origin == load_case(Path(saved['case'])).project_failure_origin
    replay = run_case(replay_case, tmp_path / 'replay')
    assert replay['status'] == expected


def test_actual_order_dependent_batch_failure_can_pass_in_isolation(tmp_path):
    """Deferred: batch observation must not be promoted to a live reproduction."""
    import subprocess
    from reprolab.project_sessions import prepare_project, collect_project, run_project
    from reprolab.workspace import WORKTREE
    from reprolab.runner import run_case
    repository = tmp_path / 'order-project'
    repository.mkdir()
    (repository / 'cart.py').write_text('broken = False\nflag = False\n')
    (repository / 'test_order.py').write_text('import cart\n\ndef test_first():\n    if cart.broken: cart.flag = True\n'
        '\ndef test_second():\n    assert not cart.flag, "batch state leaked"\n')
    for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=ReproLab test', '-c',
            'user.email=test@example.invalid', 'commit', '-qm', 'baseline']):
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-C',
                        str(repository), *args], check=True, capture_output=True)
    (repository / 'cart.py').write_text('broken = True\nflag = False\n')
    state = prepare_project(repository, 'HEAD', WORKTREE, tmp_path / 'sessions')
    collect_project(state['directory'])
    report = run_project(state['directory'], tmp_path / 'batch', confirmed=True)
    plan = flow.prepare_report_repair(report['report_dir'], 'test_order.py::test_second', tmp_path / 'preparations')
    saved = flow.save_report_repair(plan['directory'], tmp_path / 'cases/order.json', problem='批量状态泄漏',
        expected_behavior='不受其他测试顺序影响', failure_type='AssertionError',
        message_contains='batch state leaked', confirmed=True)
    result = run_case(Path(saved['case']), tmp_path / 'strict')
    assert result['status'] == 'not_reproduced'
    assert result['project_failure_origin']['observation']['after']['status'] == 'failed'
