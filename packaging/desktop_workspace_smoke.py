"""Exercise the whole current-workspace + parameter selection workflow."""
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
        deadline = time.monotonic() + 60
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(5)
            time.sleep(.005)
        assert predicate(), 'UI workflow timed out'
        app.processEvents()

    try:
        repo = root / 'project'
        repo.mkdir()
        def git(*arguments):
            return subprocess.check_output(['git', '-C', str(repo), *arguments], text=True).strip()
        git('init', '-q')
        (repo / 'sample.py').write_text('def compute(value): raise ValueError("teaching bug")\n')
        (repo / 'pytest.ini').write_text('[pytest]\naddopts = --this-should-be-disabled\n')
        git('add', '.')
        git('-c', 'user.name=ReproLab QA', '-c', 'user.email=qa@example.invalid', 'commit', '-qm', 'before fix')
        (repo / 'sample.py').write_text('def compute(value): return value * 2\n')
        (repo / 'tests').mkdir()
        marker = root / 'fixture-started'
        (repo / 'tests/conftest.py').write_text('import pytest\nfrom pathlib import Path\n@pytest.fixture(autouse=True)\n'
                                              f'def configured(): Path({str(marker)!r}).touch()\n')
        (repo / 'tests/test_fix.py').write_text('import pytest\nfrom sample import compute\n'
                                              '@pytest.mark.parametrize("value", [1,2], ids=["one","two"])\n'
                                              'def test_fix(value): assert compute(value) == value * 2\n')
        original_status = git('status', '--porcelain')
        store = DesktopStore(root / 'state')
        store.settings.update(cases=str(root / 'cases'), drafts=str(root / 'drafts'), reports=str(root / 'reports'),
                              imports=str(root / 'imports'), python=args.python, model_config='')
        wizard = CaseWizard(store.settings)
        wizard.show()
        wizard.workspace_check.setChecked(True)
        wizard.repository_edit.setText(str(repo))
        wizard.refresh_button.click()
        wait(lambda: wizard.task is None and wizard.pending is None)
        wizard.target_edit.setText('tests/test_fix.py::test_fix')
        wizard.collect_button.click()
        wait(lambda: wizard.task is None)
        targets = [wizard.tests_list.item(i).text() for i in range(wizard.tests_list.count())]
        assert targets == ['tests/test_fix.py::test_fix[one]', 'tests/test_fix.py::test_fix[two]'], wizard.status_label.text()
        assert not marker.exists(), 'collection ran fixture'
        result['checks'].append('collect_real_parameters_without_fixture_execution')
        wizard.tests_list.setCurrentRow(1)
        wizard.failure_type_edit.setText('ValueError')
        wizard.failure_message_edit.setText('teaching bug')
        wizard.grab().save(str(root / 'parameter-selection.png'))
        wizard.save_case(root / 'cases/current.json')
        wait(lambda: wizard.task is None)
        assert wizard.result_data, wizard.status_label.text()
        assert git('status', '--porcelain') == original_status
        result['checks'].append('freeze_uncommitted_code_and_new_test_without_git_mutation')
        (repo / 'sample.py').write_text('def compute(value): return -100\n')
        window = MainWindow(store)
        window.show()
        wait(lambda: window.task is None)
        window.add_case_path(wizard.result_data['case'])
        wait(lambda: window.task is None)
        window.run_button.click()
        wait(lambda: window.task is None)
        accepted = window.last_result
        assert accepted and accepted['status'] == 'verified', window.status_label.text()
        assert accepted['test_target'].endswith('[two]')
        assert accepted['workspace_origin'] and check_report(accepted['report_dir'])['status'] == 'consistent'
        assert marker.exists()
        result['checks'].extend(['frozen_case_ignores_later_repository_edits', 'selected_parameter_verified_with_complete_evidence'])
        window.grab().save(str(root / 'verified-workspace.png'))
        window.close()
        result.update(ok=True, report_dir=accepted['report_dir'], case=wizard.result_data['case'])
    except Exception:
        result.update(ok=False, error=traceback.format_exc())
        for widget in (window, wizard):
            if widget and widget.task:
                widget.task.control.cancel()
                widget.task.wait(15000)
    (root / 'workspace-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
