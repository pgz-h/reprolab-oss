"""Actual packaged variable configuration, execution and replay with synthetic local input."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest

from reprolab import __version__
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.desktop_runtime_environment import LocalValuesDialog
from reprolab.project_sessions import check_project_report, pack_project_report, unpack_project_report, replay_project_report
from reprolab.project_history import compare_project_reports
from reprolab.runner import RunControl


def main():
    root = Path(sys.argv[1]).expanduser().resolve(); root.mkdir(parents=True, exist_ok=False)
    app = QApplication.instance() or QApplication([])
    store = DesktopStore(root / 'state')
    store.settings.update(cases=str(root / 'cases'), drafts=str(root / 'drafts'), reports=str(root / 'reports'),
                          imports=str(root / 'imports'), model_config='', python='')
    window = MainWindow(store); window.show(); bench = window.project_workbench
    value = 'synthetic-local-runtime-value'
    result = {'version': __version__, 'frozen': bool(getattr(sys, 'frozen', False)), 'model_requests_sent': False, 'checks': []}
    def wait(condition=None):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            app.processEvents(); QTest.qWait(10)
            if (condition() if condition else not bench.busy and window.task is None):
                app.processEvents(); return
        raise TimeoutError(bench.status_label.text())
    def shot(name, widget=window):
        app.processEvents(); assert widget.grab().save(str(root / name))
    try:
        wait(); window.open_project_workbench()
        repo = root / 'project'; repo.mkdir()
        (repo / 'feature.py').write_text('def answer(): return 0\n')
        (repo / 'test_feature.py').write_text('import os, hashlib\nassert os.environ["APP_MODE"] == "testing"\n'
            + f'assert hashlib.sha256(os.environ["SERVICE_TOKEN"].encode()).hexdigest() == "{hashlib.sha256(value.encode()).hexdigest()}"\n'
            + 'from feature import answer\ndef test_answer():\n    assert answer() == 42\n')
        def git(*args):
            subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                '-c', 'user.name=ReproLab demo', '-c', 'user.email=demo@example.invalid', *args], check=True, capture_output=True)
        git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
        (repo / 'feature.py').write_text('def answer(): return 42\n')
        bench.repository_edit.setText(str(repo)); bench.prepare_discover_button.click(); wait()
        assert bench.collection['status'] == 'collection_failed' and bench.report is None
        old = bench.state['directory']; shot('01-missing-variables.png')
        result['checks'].append('missing_variables_block_discovery_without_passing_verdict')
        failures = []
        def configure():
            try:
                dialog = bench.environment_dialog; wait(lambda: dialog.task is None)
                panel = dialog.variables_panel; dialog.tabs.setCurrentWidget(panel)
                panel.add_variable('APP_MODE', 'testing', local=False)
                panel.add_variable('SERVICE_TOKEN', value, local=True)
                shot('02-variable-editor.png', dialog)
                previews = []
                def review(plan):
                    previews.append(plan.diff)
                    assert value not in plan.diff and 'SERVICE_TOKEN' in plan.diff
                    return True
                dialog._review_configuration = review
                panel.apply_button.click(); wait(lambda: dialog.task is None)
                assert previews and dialog.runtime_values == {'SERVICE_TOKEN': value}
                assert value not in (repo / 'pyproject.toml').read_text()
                shot('03-declarations-applied.png', dialog)
                assert panel.refresh_button.isEnabled()
                panel.refresh_button.click()
            except Exception as exc:
                failures.append(str(exc)); bench.environment_dialog.reject()
        QTimer.singleShot(0, configure)
        bench.problem_environment_button.click(); wait()
        assert not failures, failures
        wait(lambda: not bench.busy and bench.state['directory'] != old)
        assert store.local_runtime_values(repo) == {'SERVICE_TOKEN': value}
        assert value not in (store.directory / 'settings.json').read_text()
        result['checks'].append('reviewed_declarations_exclude_local_value_and_refresh_preserves_memory_input')
        bench.discover_button.click(); wait(); assert bench.collection['status'] == 'collected'
        bench.run_button.click(); wait(); report = bench.report
        assert report['status'] == 'regression_passed' and report['entries'][0]['status'] == 'improved'
        assert report['before']['runtime_environment'] == report['after']['runtime_environment']
        assert check_project_report(report['report_dir'])['status'] == 'consistent'
        for path in Path(report['report_dir']).rglob('*'):
            if path.is_file(): assert value.encode() not in path.read_bytes(), str(path)
        shot('04-comparison.png'); result['checks'].append('both_versions_share_inputs_and_tool_records_do_not_serialize_local_value')
        bundle = root / 'evidence.zip'; pack_project_report(report['report_dir'], bundle)
        imported = unpack_project_report(bundle, root / 'imported')
        preview = replay_project_report(imported['report_dir'], root / 'replays')
        assert preview['required_local_variables'] == ['SERVICE_TOKEN']
        try:
            replay_project_report(imported['report_dir'], root / 'missing-replay', confirmed=True)
        except ValueError as exc:
            assert 'SERVICE_TOKEN' in str(exc) and value not in str(exc)
        else:
            raise AssertionError('missing value must stop replay')
        replay = replay_project_report(imported['report_dir'], root / 'replays', confirmed=True,
                                      control=RunControl(runtime_values={'SERVICE_TOKEN': value}))
        assert replay['status'] == 'regression_passed' and replay['run_id'] != report['run_id']
        assert check_project_report(replay['report_dir'])['status'] == 'consistent'
        compared = compare_project_reports(report['report_dir'], replay['report_dir'])
        assert compared['scope']['same_runtime_variables'] is None
        result['checks'].append('export_import_replay_requires_values_again_and_history_keeps_local_equality_unknown')
        source = Path(bench.state['directory']) / 'inputs'
        case = root / 'strict.json'
        case.write_text(json.dumps({'title': 'runtime variables strict check', 'before': str(source / 'before'),
            'after': str(source / 'after'), 'test': str(source / 'after/test_feature.py'),
            'test_target': 'test_feature.py::test_answer', 'expected_failure': {'type': 'AssertionError', 'message_contains': '42'}}))
        original_exec = LocalValuesDialog.exec
        def provide(dialog):
            dialog.edits['SERVICE_TOKEN'].setText(value); dialog.accept(); return dialog.result()
        try:
            LocalValuesDialog.exec = provide
            window.start_verification(case); wait()
        finally:
            LocalValuesDialog.exec = original_exec
        assert window.last_result['status'] == 'verified', window.last_result.get('error') or window.last_result.get('explanation')
        assert value not in json.dumps(window.last_result)
        assert value not in (store.directory / 'activity.json').read_text()
        result['checks'].append('strict_snapshot_case_uses_explicit_memory_input_without_persisting_it')
        assert DesktopStore(root / 'state').local_runtime_values(repo) == {}
        result['checks'].append('restart_does_not_restore_local_values')
        result.update(status='passed', report=report['report_dir'], replay=replay['report_dir'])
    except Exception as exc:
        result.update(status='failed', error=str(exc)); shot('failure.png'); raise
    finally:
        (root / 'runtime-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        window.close(); app.processEvents()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
