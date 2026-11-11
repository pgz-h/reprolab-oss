"""Real Qt flow for recommended tests, observed failures, adoption, and verification."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from reprolab.desktop import MainWindow
from reprolab.desktop_creation import CaseWizard
from reprolab.desktop_store import DesktopStore
from reprolab.evidence import check_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--python', default='')
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=False)
    app = QApplication(sys.argv[:1])
    result = {'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    window = wizard = None

    def wait(predicate):
        end = time.monotonic() + 60
        while not predicate() and time.monotonic() < end:
            app.processEvents()
            QTest.qWait(5)
            time.sleep(.005)
        assert predicate(), 'probe UI timed out'
        app.processEvents()

    try:
        repo = root / 'project'
        repo.mkdir()
        def git(*arguments):
            return subprocess.check_output(['git', '-C', str(repo), *arguments], text=True).strip()
        git('init', '-q')
        (repo / 'sample.py').write_text('def compute(): raise ValueError("teaching bug")\n')
        (repo / 'tests').mkdir()
        (repo / 'tests/test_sample.py').write_text('from sample import compute\ndef test_fix(): assert compute() == 42\n')
        (repo / 'tests/test_unrelated.py').write_text('def test_other(): pass\n')
        git('add', '.')
        git('-c', 'user.name=ReproLab QA', '-c', 'user.email=qa@example.invalid', 'commit', '-qm', 'before repair')
        after_marker = root / 'after-executed'
        (repo / 'sample.py').write_text(f'from pathlib import Path\ndef compute():\n    Path({str(after_marker)!r}).touch()\n    return 42\n')
        git_state = git('status', '--porcelain')
        store = DesktopStore(root / 'state')
        store.settings.update(cases=str(root / 'cases'), drafts=str(root / 'drafts'), reports=str(root / 'reports'),
                              imports=str(root / 'imports'), python=args.python, model_config='')
        window = MainWindow(store)
        window.show()
        wait(lambda: window.task is None)
        wizard = CaseWizard(store.settings, window)
        wizard.show()
        wizard.workspace_check.setChecked(True)
        wizard.repository_edit.setText(str(repo))
        wizard.refresh_button.click()
        wait(lambda: wizard.task is None and wizard.pending is None)
        assert wizard.tests_list.item(0).text() == 'tests/test_sample.py::test_fix'
        assert wizard.tests_list.item(0).toolTip()
        result['checks'].append('related_test_ranked_and_selected_with_reason')
        wizard.probe_button.click()
        wait(lambda: wizard.task is None)
        assert wizard._candidate_probe['status'] == 'failure_observed', wizard.status_label.text()
        assert not after_marker.exists()
        assert wizard.adopt_button.isEnabled()
        assert git('status', '--porcelain') == git_state
        result['checks'].append('before_only_observation_preserves_repository')
        wizard.grab().save(str(root / 'observed-failure.png'))
        wizard.adopt_button.click()
        assert wizard.failure_type_edit.text() == 'ValueError'
        assert wizard.failure_message_edit.text() == 'teaching bug'
        (repo / 'sample.py').write_text('def compute(): return -100\n')
        wizard.save_case(root / 'cases/confirmed.json')
        wait(lambda: wizard.task is None)
        assert wizard.result_data, wizard.status_label.text()
        result['checks'].append('explicit_adoption_saves_the_observed_frozen_inputs')
        window.add_case_path(wizard.result_data['case'])
        wait(lambda: window.task is None)
        window.run_button.click()
        wait(lambda: window.task is None)
        accepted = window.last_result
        assert accepted and accepted['status'] == 'verified', window.status_label.text()
        assert after_marker.exists() and accepted['failure_origin']['confirmed']
        assert check_report(accepted['report_dir'])['status'] == 'consistent'
        result['checks'].append('formal_two_version_verification_and_origin_evidence')
        window.grab().save(str(root / 'verified.png'))
        window.close()
        result.update(ok=True, report_dir=accepted['report_dir'], case=wizard.result_data['case'])
    except Exception:
        result.update(ok=False, error=traceback.format_exc())
        for widget in (window, wizard):
            if widget and widget.task:
                widget.task.control.cancel()
                widget.task.wait(15000)
    (root / 'probe-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
