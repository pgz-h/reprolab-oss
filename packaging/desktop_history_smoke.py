"""Packaged multi-round review, notes and relocated-source acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog
from reprolab import __version__
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.project_sessions import check_project_report

IDS = {name: f'tests/test_{name}.py::test_value' for name in ('price', 'tax', 'shipping')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    root = parser.parse_args().directory.absolute()
    root.mkdir(parents=True, exist_ok=False)
    app = QApplication(sys.argv[:1])
    result = {'version': __version__, 'frozen': bool(getattr(sys, 'frozen', False)),
              'model_requests_sent': False, 'checks': []}
    window = None

    def wait(predicate, seconds=45):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            app.processEvents(); QTest.qWait(10); time.sleep(.003)
        assert predicate(), '等待桌面操作超时'
        app.processEvents()

    try:
        repo = root / 'project'; repo.mkdir(); (repo / 'tests').mkdir()
        def values(price, tax, shipping):
            (repo / 'component.py').write_text(f'VALUES = {{"price": {price}, "tax": {tax}, "shipping": {shipping}}}\n')
        values(1, 1, 1)
        for name in IDS:
            (repo / 'tests' / f'test_{name}.py').write_text(f'from component import VALUES\ndef test_value(): assert VALUES["{name}"] == 1, "{name} needs attention"\n')
        def git(*args):
            subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                            '-c', 'user.name=ReproLab QA', '-c', 'user.email=qa@example.invalid', *args], check=True, capture_output=True)
        git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
        values(0, 0, 0)
        store = DesktopStore(root / 'state')
        store.settings.update(cases=str(root / 'cases'), reports=str(root / 'reports'), drafts=str(root / 'drafts'),
                              imports=str(root / 'imports'), python='', model_config='')
        window = MainWindow(store); window.resize(1280, 840); window.show()
        bench = window.project_workbench
        wait(lambda: window.task is None and not bench.busy)
        window.navigation_buttons[bench].click()
        bench.repository_edit.setText(str(repo)); bench.before_combo.setEditText('HEAD')
        bench.workspace_check.setChecked(True); bench.title_edit.setText('多轮修复与未复查项示例')
        bench.prepare_button.click(); wait(lambda: not bench.busy)
        bench.discover_button.click(); wait(lambda: not bench.busy)
        bench._check_ids(list(IDS.values())); bench.run_button.click(); wait(lambda: not bench.busy)
        first = dict(bench.report)
        assert first['counts'] == {'regression_detected': 3}
        values(1, 0, 0)
        group = next(group for group in bench._diagnostic_summary['groups'] if group['test_ids'] == [IDS['price']] and group['side'] == 'after')
        bench.diagnostic_selector.setCurrentIndex(bench.diagnostic_selector.findData(group['id']))
        bench.recheck_diagnostic_button.click(); wait(lambda: not bench.busy)
        second = dict(bench.report)
        assert second['selected_tests'] == [IDS['price']] and second['status'] == 'regression_passed'
        assert second['review_source']['run_id'] == first['run_id']
        result['checks'].extend(['initial_three_failures_recorded', 'group_recheck_preserves_one_test_scope'])
        values(1, 1, 0)
        bench._check_ids([IDS['price'], IDS['tax']])
        bench.refresh_workspace_button.click(); wait(lambda: not bench.busy)
        assert bench.state['review_source']['run_id'] == second['run_id']
        assert bench.collection is None
        bench.discover_button.click(); wait(lambda: not bench.busy)
        bench.run_button.click(); wait(lambda: not bench.busy)
        third = dict(bench.report)
        assert third['status'] == 'regression_passed' and len(third['selected_tests']) == 2
        assert third['review_source']['selection_mode'] == 'workspace'
        result['checks'].append('workspace_refresh_links_displayed_report_and_current_selection')
        original_bytes = {report['run_id']: (Path(report['report_dir']) / 'report.json').read_bytes() for report in (first, second, third)}
        bench.results_scroll.ensureWidgetVisible(bench.processing_history_button)
        bench.processing_history_button.click()
        wait(lambda: bench.task is None and bench.processing_dialog is not None)
        dialog = bench.processing_dialog
        assert dialog.data['summary']['rounds'] == 3 and dialog.data['summary']['not_rechecked'] == 1
        shipping = next(row for row in dialog.data['tests'] if row['test_id'] == IDS['shipping'])
        assert not shipping['selected_now'] and shipping['latest_observation']['round'] == 1
        dialog.tabs.setCurrentIndex(1)
        dialog.filter.setCurrentIndex(dialog.filter.findData('omitted'))
        dialog.grab().save(str(root / 'unrechecked-tests.png'))
        result['checks'].append('test_omitted_for_two_rounds_remains_visible')
        dialog.tabs.setCurrentIndex(0)
        note = '本轮修复税费相关检查；运费项仍未复查。'
        dialog.note_edit.setPlainText(note); dialog.save_note_button.click(); wait(lambda: dialog.task is None)
        dialog.copy_button.click(); assert note in QApplication.clipboard().text()
        destination = root / 'processing-history.json'
        original_picker = QFileDialog.getSaveFileName
        try:
            QFileDialog.getSaveFileName = lambda *args, **kwargs: (str(destination), '')
            dialog.export_button.click()
        finally:
            QFileDialog.getSaveFileName = original_picker
        assert json.loads(destination.read_text())['rounds'][-1]['note']['text'] == note
        dialog.grab().save(str(root / 'rounds-and-note.png'))
        dialog.compare_button.click(); wait(lambda: dialog.task is None and dialog.comparison_dialog is not None)
        dialog.comparison_dialog.grab().save(str(root / 'adjacent-comparison.png'))
        dialog.comparison_dialog.reject(); app.processEvents()
        result['checks'].append('saved_note_is_copied_exported_and_adjacent_comparison_opens')
        dialog.reject(); app.processEvents()
        bench.processing_history_button.click(); wait(lambda: bench.task is None and bench.processing_dialog is not None)
        dialog = bench.processing_dialog
        assert dialog.note_edit.toPlainText() == note
        result['checks'].append('local_note_restores_on_reopen')
        moved = root / 'relocated-second-report'
        Path(second['report_dir']).rename(moved)
        dialog.reload_button.click(); wait(lambda: dialog.task is None)
        assert dialog.data['status'] == 'partial' and dialog.data['summary']['rounds'] == 1
        dialog.grab().save(str(root / 'source-gap.png'))
        original_directory_picker = QFileDialog.getExistingDirectory
        try:
            QFileDialog.getExistingDirectory = lambda *args, **kwargs: str(moved)
            dialog.relocate_button.click(); wait(lambda: dialog.task is None)
        finally:
            QFileDialog.getExistingDirectory = original_directory_picker
        assert dialog.data['status'] == 'complete' and dialog.data['summary']['rounds'] == 3
        assert store.settings['project_report_locations'][third['review_source']['report_sha256']] == str(moved)
        assert dialog.note_edit.toPlainText() == note
        result['checks'].append('missing_source_is_explicit_and_verified_relocation_rejoins_chain')
        dialog.resize(960, 680); app.processEvents()
        dialog.tabs.setCurrentIndex(1)
        dialog.filter.setCurrentIndex(dialog.filter.findData('omitted'))
        dialog.grab().save(str(root / 'compact-history.png'))
        result['checks'].append('compact_history_keeps_unrechecked_test_accessible')
        final_export = root / 'processing-history-relocated.json'
        try:
            QFileDialog.getSaveFileName = lambda *args, **kwargs: (str(final_export), '')
            dialog.export_button.click()
        finally:
            QFileDialog.getSaveFileName = original_picker
        second['report_dir'] = str(moved)
        for report in (first, second, third):
            assert check_project_report(report['report_dir'])['status'] == 'consistent'
            assert (Path(report['report_dir']) / 'report.json').read_bytes() == original_bytes[report['run_id']]
        result['checks'].append('all_original_report_bytes_and_evidence_remain_consistent')
        result.update(status='passed', reports=[report['report_dir'] for report in (first, second, third)],
                      current_report=third['report_dir'], notes_root=str(bench.project_root.parent / 'run-notes'),
                      history_file=str(final_export), source_locations=dict(store.settings['project_report_locations']))
        dialog.reject(); app.processEvents()
    except Exception:
        result.update(status='failed', error=traceback.format_exc())
    finally:
        if window:
            bench = window.project_workbench
            if bench.processing_dialog is not None:
                dialog = bench.processing_dialog
                dialog._confirm_discard = lambda: True
                dialog.reject()
                wait(lambda: bench.processing_dialog is None)
            if bench.task:
                bench.task.control.cancel(); wait(lambda: bench.task is None)
            window.close(); app.processEvents()
        (root / 'history-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
