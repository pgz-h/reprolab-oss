"""Check the workbench acceptance flow against saved execution evidence."""
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from test_desktop_project_replay import saved_report, start_workbench
from test_desktop_ui import wait_idle, window
from test_pytest_profile import make_project
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QTreeWidget,
)

from reprolab import desktop_project, project_review_summary
from reprolab.acceptance_plan import AcceptancePlan, BaselinePin, read_acceptance_plan, save_acceptance_plan
from reprolab.project_history import read_report_snapshot
from reprolab.project_sessions import pack_project_report, prepare_project, replay_project_report, run_project
from reprolab.replay_receipt import save_replay_receipt
from reprolab.replay_stability import replay_stability
from reprolab.runner import RunControl
from reprolab.workspace import WORKTREE


def test_imported_pytest_zip_requires_review_when_double_red_assertion_changes(window, tmp_path, monkeypatch):
    repo = make_project(tmp_path / 'project')
    (repo / 'tests/test_calc.py').write_text(
        'from calc import answer\ndef test_answer(expected):\n'
        '    assert answer() == expected\n    assert answer() < expected\n', encoding='utf-8')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    original = run_project(state['directory'], tmp_path / 'original', confirmed=True)
    evidence = tmp_path / 'evidence.zip'
    pack_project_report(original['report_dir'], evidence)

    bench = start_workbench(window)
    assert bench.import_bundle(evidence)
    wait_idle(bench)
    assert '记录的断言位置不同' in bench.result_summary.text()
    seen = []

    def respond(dialog):
        if dialog.objectName() == 'projectAcceptanceOptions':
            policy = dialog.findChild(QComboBox, 'acceptancePolicy')
            policy.setCurrentIndex(policy.findData('no-new-failures'))
            dialog.findChild(QPushButton, 'acceptanceStart').click()
        elif dialog.objectName() == 'projectAcceptanceResult':
            headline = dialog.findChild(QLabel, 'acceptanceHeadline').text()
            row = dialog.findChild(QTreeWidget, 'acceptanceChecks').topLevelItem(0)
            seen.append((headline, row.text(0), row.text(1), row.text(2)))
            dialog.reject()
        return dialog.result()

    monkeypatch.setattr(QDialog, 'exec', respond)
    bench.acceptance_button.click()
    wait_idle(bench)
    [(headline, title, status, details)] = seen
    assert (headline, title, status) == ('验收受阻', '回归门槛 · 允许原有失败', '受阻')
    assert '断言位置变化，需复核 1 项' in details
    assert bench.report['counts'] == {'baseline_failure': 1}


def test_acceptance_preserves_hidden_targets_and_rechecks_saved_materials(window, tmp_path, monkeypatch):
    repo = make_project(tmp_path / 'project')
    with (repo / 'tests/test_calc.py').open('a') as tests:
        tests.write('\ndef test_always_passes(): pass\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    original = run_project(state['directory'], tmp_path / 'original', confirmed=True)
    source = Path(original['report_dir'])
    replay = replay_project_report(source, tmp_path / 'replay', confirmed=True, verify_result=True)
    receipt = tmp_path / 'receipt'
    save_replay_receipt(replay, receipt, control=RunControl())
    stability = replay_stability(source, tmp_path / 'stability', rounds=2, confirmed=True)
    bench = start_workbench(window)
    assert not bench.acceptance_button.isVisible()
    assert bench.open_project_report(source / 'report.json')
    wait_idle(bench)
    assert bench.acceptance_button.isVisible() and bench.acceptance_button.isEnabled()
    report = bench.report
    evidence = (source / 'report.json').read_bytes()
    dialogs = []
    expected = ['projectAcceptanceOptions', 'projectAcceptanceResult'] * 3
    plan_file = tmp_path / 'saved-plan.json'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *_args, **_kw: (str(plan_file), ''))

    def respond(dialog):
        dialogs.append(dialog.objectName())
        assert dialogs == expected[:len(dialogs)]
        attempt = (len(dialogs) - 1) // 2
        if dialog.objectName() == 'projectAcceptanceOptions':
            tree = dialog.findChild(QTreeWidget, 'acceptanceTargets')
            search = dialog.findChild(QLineEdit, 'acceptanceSearch')
            count = dialog.findChild(QLabel, 'acceptanceSelection')
            receipt_edit = dialog.findChild(QLineEdit, 'acceptanceReceipt')
            stability_edit = dialog.findChild(QLineEdit, 'acceptanceStability')
            rounds = dialog.findChild(QSpinBox, 'acceptanceMinRounds')
            fixed = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                         if tree.topLevelItem(i).text(0) == 'test_answer')
            if attempt == 0:
                assert not rounds.isEnabled()
                fixed.setCheckState(0, Qt.CheckState.Checked)
                search.setText('test_always_passes')
                assert fixed.isHidden() and '1 项被搜索隐藏' in count.text()
                dialog.findChild(QCheckBox, 'acceptanceRequireReceipt').click()
                dialog.findChild(QCheckBox, 'acceptanceRequireStability').click()
                receipt_edit.setText(str(receipt))
                stability_edit.setText(stability['summary_file'])
                assert rounds.isEnabled()
                rounds.setValue(3)
            else:
                assert fixed.checkState(0) == Qt.CheckState.Checked
                assert receipt_edit.text() == str(receipt)
                assert stability_edit.text() == stability['summary_file']
                assert rounds.value() == (3 if attempt == 1 else 2)
                rounds.setValue(2)
            if attempt == 1:
                dialog.findChild(QPushButton, 'acceptanceSavePlan').click()
                plan = read_acceptance_plan(plan_file)
                assert plan == AcceptancePlan('pytest', 'strict', ('tests/test_calc.py::test_answer',), True, 2)
                assert str(tmp_path) not in plan_file.read_text(encoding='utf-8')
            dialog.findChild(QPushButton, 'acceptanceStart').click()
        else:
            headline = dialog.findChild(QLabel, 'acceptanceHeadline')
            copy = dialog.findChild(QPushButton, 'acceptanceCopy')
            if attempt == 2:
                assert headline.text() == '无法完成验收' and copy is None
                assert '来源身份不符' in dialog.findChild(QPlainTextEdit, 'acceptanceError').toPlainText()
                dialog.reject()
                return dialog.result()
            rows = dialog.findChild(QTreeWidget, 'acceptanceChecks')
            assert rows.topLevelItemCount() == 4
            assert [rows.topLevelItem(i).text(1) for i in range(4)] == [
                '通过', '通过', '受阻' if attempt == 0 else '通过', '通过']
            assert '2/2 轮已核对' in rows.topLevelItem(2).text(2)
            if attempt == 0:
                assert headline.text() == '验收受阻'
                assert rows.currentItem().text(0) == '多轮稳定性'
                status_filter = dialog.findChild(QComboBox, 'acceptanceResultFilter')
                search = dialog.findChild(QLineEdit, 'acceptanceResultSearch')
                count = dialog.findChild(QLabel, 'acceptanceResultCount')
                detail = dialog.findChild(QLabel, 'acceptanceDetail')
                status_filter.setCurrentIndex(status_filter.findData('attention'))
                assert count.text() == '1/4 项' and rows.currentItem().text(0) == '多轮稳定性'
                search.setText('test_calc.py::test_answer')
                assert count.text() == '无匹配项' and not detail.text()
                status_filter.setCurrentIndex(status_filter.findData('passed'))
                assert count.text() == '1/4 项' and rows.currentItem().text(0) == 'test_answer'
                assert 'tests/test_calc.py::test_answer' in detail.text()
                copy.click()
                assert 'Required replay stability · Blocked' in QApplication.clipboard().text()
                dialog.findChild(QPushButton, 'acceptanceAdjust').click()
            else:
                assert headline.text() == '验收通过'
                copy.click()
                markdown = QApplication.clipboard().text()
                assert 'Required improvement' in markdown and 'test_answer' in markdown
                assert 'Required replay receipt · Passed' in markdown
                assert 'Required replay stability · Passed' in markdown
                assert str(tmp_path) not in markdown
                dialog.reject()
        return dialog.result()

    monkeypatch.setattr(QDialog, 'exec', respond)
    bench.acceptance_button.click()
    wait_idle(bench)
    wait_idle(bench)
    assert dialogs == expected[:4]
    assert (source / 'report.json').read_bytes() == evidence
    assert bench.report is report and bench.acceptance_button.isEnabled()
    assert bench.status_label.text() == '验收通过'

    # Changing the saved report after it was opened invalidates the desktop selection's report identity.
    (source / 'report.json').write_bytes(evidence + b'\n')
    assert bench.open_project_acceptance(options={
        'plan': read_acceptance_plan(plan_file),
        'receipt': receipt, 'stability': stability['summary_file'],
    })
    wait_idle(bench)
    assert dialogs == expected
    assert bench.report is report


def test_acceptance_exports_checked_report_scope_and_plan_to_ci(window, tmp_path, monkeypatch, capsys):
    from reprolab.project_cli import main
    from reprolab.project_selections import read_selection

    repo = make_project(tmp_path / 'project')
    with (repo / 'tests/test_calc.py').open('a') as tests:
        tests.write('\ndef test_always_passes(): pass\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    original = run_project(state['directory'], tmp_path / 'original', confirmed=True)
    report_path = Path(original['report_dir']) / 'report.json'
    bench = start_workbench(window)
    assert bench.open_project_report(report_path)
    wait_idle(bench)
    evidence = report_path.read_bytes()
    config = tmp_path / 'ci-config.zip'
    collision = tmp_path / 'occupied.zip'
    collision.write_bytes(b'leave this file alone')
    destinations = iter([collision, config.with_suffix('')])
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *_a, **_kw: (str(next(destinations)), ''))

    target = 'tests/test_calc.py::test_answer'
    plan = AcceptancePlan('pytest', 'strict', (target,))
    attempts = []

    def respond(dialog):
        assert dialog.objectName() == 'projectAcceptanceResult'
        assert '实际选择 2 项' in dialog.findChild(QLabel, 'acceptanceScope').text()
        counts = dialog.findChild(QLabel, 'acceptanceCheckCounts').text()
        assert '通过 2' in counts and '失败 0' in counts and '受阻 0' in counts
        feedback = dialog.findChild(QLabel, 'acceptanceExportError')
        if not attempts:
            assert feedback is None
        else:
            assert '文件已存在' in feedback.text()
        attempts.append(dialog)
        dialog.findChild(QPushButton, 'acceptanceExportCI').click()
        return dialog.result()

    monkeypatch.setattr(QDialog, 'exec', respond)
    bench._show_project_acceptance(
        desktop_project.report_reference(bench.report), {'plan': plan},
        project_review_summary.review_summary(original['report_dir'], plan=plan), None,
    )
    wait_idle(bench)
    wait_idle(bench)
    assert len(attempts) == 2
    assert collision.read_bytes() == b'leave this file alone'
    assert report_path.read_bytes() == evidence
    assert str(config) in bench.status_label.text()
    directory = tmp_path / 'ci'
    with ZipFile(config) as archive:
        assert set(archive.namelist()) == {'regression-tests.json', 'acceptance-plan.json'}
        archive.extractall(directory)
    selection_file = directory / 'regression-tests.json'
    plan_file = directory / 'acceptance-plan.json'
    assert read_selection(selection_file) == original['selected_tests']
    assert read_acceptance_plan(plan_file) == plan
    assert main(list(map(str, (
        'run', state['directory'], '--selection-file', selection_file, '--acceptance-plan', plan_file,
        '--verify-plan', '--output', tmp_path / 'ci-report',
    )))) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated['selected_tests'] == original['selected_tests']
    assert repeated['acceptance']['acceptance_plan_sha256'] == plan.fingerprint()


@pytest.mark.parametrize('runner', ['go', 'vitest'])
def test_acceptance_binds_runner_and_uses_exact_test_identity(window, tmp_path, monkeypatch, runner):
    source = tmp_path / 'saved'
    report_fixture = saved_report(runner, source)
    report_fixture['source_sha256'] = {'before': 'a' * 64, 'after': 'b' * 64}
    (source / 'report.json').write_text(json.dumps(report_fixture), encoding='utf-8')

    def checked(directory, *, control):
        return read_report_snapshot(directory)

    monkeypatch.setattr(desktop_project, '_checked_report', checked)
    monkeypatch.setattr(project_review_summary, '_checked_report', checked)
    bench = start_workbench(window)
    assert bench.open_project_report(source / 'report.json')
    wait_idle(bench)
    report = getattr(bench, runner + '_report')
    seen = []

    def respond(dialog):
        if dialog.objectName() == 'projectAcceptanceOptions':
            tree = dialog.findChild(QTreeWidget, 'acceptanceTargets')
            rows = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
            target = rows[0]
            target.setCheckState(0, Qt.CheckState.Checked)
            entry = report['entries'][0]
            expected = entry['test_id']
            if runner == 'vitest':
                expected += ' · ' + entry['case_entries'][0]['test_id']
            assert target.toolTip(0) == expected
            policy = dialog.findChild(QComboBox, 'acceptancePolicy')
            policy.setCurrentIndex(policy.findData('no-new-failures'))
            dialog.findChild(QPushButton, 'acceptanceStart').click()
        else:
            rows = dialog.findChild(QTreeWidget, 'acceptanceChecks')
            seen.append((rows.topLevelItem(0).text(0), rows.topLevelItem(1).toolTip(0)))
            dialog.reject()
        return dialog.result()

    monkeypatch.setattr(QDialog, 'exec', respond)
    assert bench.acceptance_button.isVisible()
    bench.acceptance_button.click()
    wait_idle(bench)
    entry = report['entries'][0]
    target = entry['test_id']
    if runner == 'vitest':
        target += ' · ' + entry['case_entries'][0]['test_id']
    assert seen == [('回归门槛 · 允许原有失败', target)]
    assert getattr(bench, runner + '_report') is report


def test_opened_plan_retains_missing_goal_and_required_materials(window, tmp_path, monkeypatch):
    from reprolab.desktop_acceptance import AcceptanceOptionsDialog
    from reprolab.project_cli import main

    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    result = run_project(state['directory'], tmp_path / 'report', confirmed=True)
    report = read_report_snapshot(result['report_dir'])
    path = tmp_path / 'portable.json'
    missing = 'tests/test_calc.py::test_renamed'
    original_baseline = BaselinePin('a' * 64, report['source_mode_sha256']['before'])
    plan = AcceptancePlan('pytest', 'strict', (missing, report['selected_tests'][0]), True, 3, original_baseline)
    save_acceptance_plan(plan, path)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *_args: (str(path), ''))
    dialog = AcceptanceOptionsDialog(report, window)
    try:
        dialog.findChild(QPushButton, 'acceptanceOpenPlan').click()
        tree = dialog.findChild(QTreeWidget, 'acceptanceTargets')
        assert tree.topLevelItem(0).toolTip(0) == missing
        assert tree.topLevelItem(0).checkState(0) == Qt.CheckState.Checked
        assert '1 项缺少报告结果' in dialog.findChild(QLabel, 'acceptanceSelection').text()
        assert dialog.findChild(QCheckBox, 'acceptanceRequireReceipt').isChecked()
        assert dialog.findChild(QSpinBox, 'acceptanceMinRounds').value() == 3
        assert dialog.findChild(QCheckBox, 'acceptancePinBaseline').isChecked()
        assert '基线与计划不同' in dialog.findChild(QLabel, 'acceptanceBaselineNotice').text()
        options = dialog.options()
        assert options == {'plan': plan, 'receipt': None, 'stability': None}
        summary = project_review_summary.review_summary(report['report_dir'], **options)
        assert summary['required_improvements'][0]['status'] == 'blocked'
        assert summary['baseline_check']['expected'] == original_baseline.record()
        assert summary['baseline_check']['reason'] == 'different'
        assert summary['receipt']['receipt_status'] == 'missing'
        assert summary['stability']['verification_status'] == 'missing'
        assert summary['gate']['exit_code'] == 2
        assert main(['summary', report['report_dir'], '--acceptance-plan', str(path)]) == 2
    finally:
        dialog.close()


@pytest.mark.parametrize('runner', ['go', 'vitest'])
def test_desktop_runs_loaded_plan_then_shows_acceptance(window, tmp_path, monkeypatch, runner):
    source = tmp_path / 'saved'
    fixture = saved_report(runner, source)
    fixture['source_sha256'] = {'before': 'a' * 64, 'after': 'b' * 64}
    (source / 'report.json').write_text(json.dumps(fixture), encoding='utf-8')
    bench = start_workbench(window)
    bench.tabs.setCurrentWidget(getattr(bench, runner + '_page'))
    getattr(bench, runner + '_repository_edit').setText(str(tmp_path / 'repo'))
    entry = fixture['entries'][0]
    identity = entry['test_id'] if runner == 'go' else (entry['test_id'], entry['case_entries'][0]['test_id'])
    plan = AcceptancePlan(runner, 'strict', (identity,))
    plan_file = tmp_path / 'acceptance.json'
    save_acceptance_plan(plan, plan_file)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *_a, **_kw: (str(plan_file), ''))
    monkeypatch.setattr(project_review_summary, '_checked_report', lambda path, *, control: read_report_snapshot(path))
    seen = []

    def run(*args, acceptance_plan, control, **options):
        assert acceptance_plan == plan
        if runner == 'go':
            assert options['selected'] is None
        else:
            assert args[3] is None
        seen.append('ran')
        return fixture

    def confirm(dialog):
        if dialog.objectName() == 'projectAcceptanceResult':
            assert dialog.findChild(QLabel, 'acceptanceHeadline').text() == '验收失败'
            rows = dialog.findChild(QTreeWidget, 'acceptanceChecks')
            assert rows.topLevelItem(1).text(1) == '通过'
            assert dialog.findChild(QPushButton, 'acceptanceCopy').isEnabled()
            dialog.reject()
            seen.append('verified')
            return dialog.result()
        raise AssertionError(dialog.windowTitle())

    monkeypatch.setattr(desktop_project, f'run_{runner}_project', run)
    monkeypatch.setattr(QDialog, 'exec', confirm)
    monkeypatch.setattr(bench, '_confirm_choice', lambda *_: False)
    button = getattr(bench, runner + '_plan_button')
    button.click()
    assert not seen and bench.task is None
    monkeypatch.setattr(bench, '_confirm_choice', lambda *_: True)
    button.click()
    wait_idle(bench)
    assert seen == ['ran', 'verified']
    assert getattr(bench, runner + '_report')['report_dir'] == str(source)
