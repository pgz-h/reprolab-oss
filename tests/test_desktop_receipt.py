"""Exercise the desktop replay handoff with real saved pytest evidence."""
from pathlib import Path
import shutil
import sys
import time

from test_desktop_ui import wait_idle, window
from test_pytest_profile import make_project
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QTableWidget, QTabWidget,
)

from reprolab.desktop_project import QDesktopServices
from reprolab.project_sessions import pack_project_report, prepare_project, run_project
from reprolab.replay_receipt import check_replay_receipt
from reprolab.workspace import WORKTREE


def test_desktop_replay_saves_and_checks_returned_receipts(window, tmp_path, monkeypatch):
    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    original = run_project(state['directory'], tmp_path / 'origin', confirmed=True)
    source_zip = tmp_path / 'source.zip'
    pack_project_report(original['report_dir'], source_zip)
    repo.rename(tmp_path / 'sender-moved')

    bench = window.project_workbench
    wait_idle(bench)
    window.tabs.setCurrentWidget(window.home_page)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *_args: (str(source_zip), ''))
    window.home_page.import_evidence_button.click()
    wait_idle(bench)
    wait_idle(window)
    assert window.tabs.currentWidget() is bench
    assert bench.preview_replay_button.isVisible() and bench.preview_replay_button.isEnabled()
    assert '尚未重放' in bench.evidence_message.text()
    assert not list((tmp_path / 'reports').glob('*/report.json'))
    imported = Path(bench.report['report_dir'])
    assert imported.parent == tmp_path / 'imported'
    window.tabs.setCurrentWidget(window.reports_page)
    assert Path(window.selected_report()['directory']) == imported
    assert window.open_project_evidence_button.isVisible()
    window.open_project_evidence_button.click()
    wait_idle(bench)
    assert window.tabs.currentWidget() is bench
    assert Path(bench.report['report_dir']) == imported
    assert bench.preview_replay_button.isVisible()
    receipt = tmp_path / 'receipt'
    rebuilt = tmp_path / 'rebuilt'
    opened, results = [], []
    previews = []

    def respond(dialog):
        if dialog.windowTitle() == '重放项目报告':
            scope = dialog.findChild(QPlainTextEdit, 'replaySelectedTests')
            assert scope.isReadOnly() and scope.toPlainText().splitlines() == original['selected_tests']
            previews.append(scope.toPlainText())
            if len(previews) == 1:
                dialog.reject()
                return dialog.result()
            dialog.findChild(QLineEdit, 'replayPythonPath').setText(sys.executable)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        elif dialog.objectName() == 'replayReceiptPaths':
            values = {'source': source_zip, 'receipt': receipt}
            if dialog.windowTitle() == '制作回执':
                values['replay'] = Path(bench.report['report_dir']) / 'report.json'
                values['destination'] = rebuilt
            elif dialog.windowTitle() == '保存回执':
                values['destination'] = receipt
                assert dialog.suggested_name == bench.report['run_id'] + '-receipt'
            assert not dialog.submit.isEnabled()
            for field, edit in dialog.edits.items():
                assert edit.accessibleName()
                edit.setText(str(values[field]))
            assert dialog.submit.isEnabled()
            dialog.submit.click()
        elif dialog.objectName() == 'replayReceiptResult':
            save = dialog.findChild(QPushButton, 'receiptSave')
            if save:
                save.click()
            else:
                headline = dialog.findChild(QLabel, 'receiptResultHeadline').text()
                details = dialog.findChild(QPlainTextEdit, 'receiptResultDetails').toPlainText()
                copy = dialog.findChild(QPushButton, 'receiptCopyMarkdown')
                if copy:
                    copy.click()
                if open_folder := dialog.findChild(QPushButton, 'receiptOpenFolder'):
                    open_folder.click()
                results.append((headline, details, bool(copy)))
                dialog.reject()
        else:
            raise AssertionError(f'Unexpected dialog: {dialog.windowTitle()}')
        return dialog.result()

    monkeypatch.setattr(QDialog, 'exec', respond)
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(Path(url.toLocalFile())) or True)
    bench.preview_replay_button.click()
    assert bench.task is None and not list((tmp_path / 'reports').glob('*/report.json'))
    assert bench.preview_replay_button.isVisible()
    bench.preview_replay_button.click()
    wait_idle(bench)
    assert bench.replay_verification_button.isVisible()
    assert bench.save_replay_receipt_button.isVisible() and not bench.preview_replay_button.isVisible()
    replayed = bench.report
    assert replayed['run_id'] != original['run_id']
    bench.tabs.setCurrentWidget(bench.go_page)
    assert not bench.evidence_actions.isVisible()
    bench.tabs.setCurrentWidget(bench.results_scroll)
    assert bench.save_replay_receipt_button.isVisible()
    bench.save_replay_receipt_button.click()
    wait_idle(bench)
    assert len(results) == 1
    assert '已保存回执' in results[0][0] and opened == [receipt]
    assert bench.open_replay_receipt_button.isVisible() and not bench.save_replay_receipt_button.isVisible()
    assert check_replay_receipt(source_zip, receipt)['status'] == 'consistent'
    assert QApplication.clipboard().text() == (receipt / 'replay-receipt.md').read_text(encoding='utf-8')

    def action_named(title):
        return next(action for action in bench.project_menu.actions() if action.text() == title)

    action_named('复核回传回执…').trigger()
    wait_idle(bench)
    assert len(results) == 2
    assert '回传材料一致' in results[1][0] and results[1][2]
    assert bench.open_replay_receipt_button.isVisible()
    assert bench.report is replayed

    with (receipt / 'replay-receipt.md').open('a', encoding='utf-8') as markdown:
        markdown.write('\nEdited\n')
    action_named('复核回传回执…').trigger()
    wait_idle(bench)
    assert len(results) == 3
    assert '回传材料需要核对' in results[2][0]
    assert 'replay-receipt.md' in results[2][1] and not results[2][2]
    assert bench.report is replayed

    action_named('从报告制作回执…').trigger()
    wait_idle(bench)
    assert len(results) == 4
    assert '已保存回执' in results[3][0] and opened[-1] == rebuilt
    assert check_replay_receipt(source_zip, rebuilt)['status'] == 'consistent'
    assert bench.report is replayed


def test_desktop_multiple_replays_show_stability_and_saved_reports(window, tmp_path, monkeypatch):
    from reprolab.desktop import MainWindow
    from reprolab.desktop_store import DesktopStore

    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    original = run_project(state['directory'], tmp_path / 'origin', confirmed=True)
    bench = window.project_workbench
    wait_idle(bench)
    window.tabs.setCurrentWidget(bench)
    assert bench.open_project_report(Path(original['report_dir']) / 'report.json')
    wait_idle(bench)
    report = bench.report
    seen, opened, histories = [], [], []

    def respond(dialog):
        if dialog.objectName() == 'replayStabilityResult':
            assert '2/2 轮有报告' in dialog.findChild(QLabel, 'stabilityHeadline').text()
            outcomes = dialog.findChild(QTableWidget, 'stabilityOutcomes')
            assert outcomes.rowCount() == 1
            assert [outcomes.item(0, column).text() for column in range(1, 5)] == [
                '每轮复现', '失败 → 通过', '失败 → 通过', '失败 → 通过']
            dialog.findChild(QTabWidget).setCurrentIndex(1)
            rounds = dialog.findChild(QTableWidget, 'stabilityRounds')
            rounds.selectRow(1)
            dialog.findChild(QPushButton, 'stabilityOpenReport').click()
            seen.append(rounds.item(1, 0).toolTip())
            dialog.reject()
        elif dialog.objectName() == 'replayStabilityHistory':
            checks = dialog.findChild(QTableWidget, 'stabilityHistoryChecks')
            histories.append(checks.rowCount())
            if checks.rowCount():
                assert checks.rowCount() == 1 and checks.item(0, 1).text() == '2/2'
                assert checks.item(0, 2).text() == '每轮均复现'
                assert checks.item(0, 0).toolTip().startswith('replay-stability-')
                dialog.findChild(QPushButton, 'stabilityHistoryOpen').click()
            else:
                dialog.findChild(QPushButton, 'stabilityHistoryBrowse').click()
        else:
            rounds = dialog.findChild(QSpinBox, 'replayRounds')
            assert rounds.value() == 1
            rounds.setValue(2)
            start = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
            assert start.text() == '重放 2 轮'
            start.click()
        return dialog.result()

    monkeypatch.setattr(QDialog, 'exec', respond)
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(Path(url.toLocalFile())) or True)
    bench.preview_replay_button.click()
    wait_idle(bench)
    assert len(seen) == 1 and opened[0] == Path(seen[0]) / 'report.html'
    assert opened[0].is_file()
    assert bench.report is report
    assert bench.stability_result_button.isVisible()
    window.store.save()
    store = DesktopStore(window.store.directory)
    other = MainWindow(store)
    other.show()
    try:
        reopened = other.project_workbench
        wait_idle(other)
        wait_idle(reopened)
        other.tabs.setCurrentWidget(reopened)
        assert reopened.open_project_report(Path(original['report_dir']) / 'report.json')
        wait_idle(reopened)
        assert reopened.stability_result_button.isVisible()
        def reopen_history(expected):
            reopened.stability_result_button.click()
            deadline = time.monotonic() + 12
            while len(seen) < expected and time.monotonic() < deadline:
                wait_idle(reopened)
                QTest.qWait(10)
            assert len(seen) == expected

        reopen_history(2)
        assert histories == [1] and seen[0] == seen[1]
        assert reopened.report['run_id'] == original['run_id']
        check = Path(seen[0]).parent.parent
        moved = tmp_path / 'moved'
        shutil.move(check, moved)
        monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                            lambda *_args: (str(moved / 'replay-stability.json'), ''))
        reopen_history(3)
        assert histories == [1, 0]
        assert Path(seen[2]).parent == moved / 'reports'
    finally:
        other.close()
