"""Explicit v0.13 QA: real widgets/workers and controlled local write failures."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from reprolab.desktop import MainWindow
from reprolab.desktop_store import DesktopStore
from reprolab.evidence import check_report
from reprolab.evidence_bundle import pack_report, unpack_report
from reprolab import report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--python', default='')
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    output = {'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    window = None
    original_write = report.write_report

    def wait():
        deadline = time.monotonic() + 45
        while window.task and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(15)
            # Let the background Python worker acquire the GIL between Qt polls.
            time.sleep(.005)
        assert window.task is None, 'UI task timed out'
        app.processEvents()

    def case(name, test, target=None):
        folder = root / 'fixtures' / name
        folder.mkdir(parents=True)
        for label, body in [('before', 'raise ValueError("teaching bug")'), ('after', 'return 42')]:
            (folder / label).mkdir()
            (folder / label / 'sample.py').write_text('def compute():\n    ' + body + '\n')
        (folder / 'regression.py').write_text(test)
        path = folder / 'case.json'
        path.write_text(json.dumps({'title': name, 'before': 'before', 'after': 'after', 'test': 'regression.py',
                                   'test_target': target, 'expected_failure': {'type': 'ValueError', 'message_contains': 'teaching bug'}}))
        return path

    def run(path):
        window.add_case_path(path)
        wait()
        window.run_button.click()
        wait()
        return window.last_result

    try:
        store = DesktopStore(root / 'state')
        store.settings.update(cases=str(root / 'cases'), drafts=str(root / 'drafts'), reports=str(root / 'reports'),
                              imports=str(root / 'imports'), model_config='', python=args.python)
        window = MainWindow(store)
        window.show()
        wait()
        marker = root / 'must-not-execute'
        multiple = case('动态多测试被阻止', 'import pytest\nfrom pathlib import Path\n'
                        f'MARKER = Path({str(marker)!r})\n'
                        '@pytest.fixture(autouse=True)\ndef fixture(): MARKER.touch()\n'
                        "@pytest.mark.parametrize('value', [1, 2])\ndef test_one(value): MARKER.touch()\n",
                        'tests/test_selection.py')
        rejected = run(multiple)
        assert rejected['status'] == 'inconclusive'
        assert not marker.exists()
        for label in ('before', 'after'):
            assert rejected[label]['selection_guard']['status'] == 'rejected'
            assert rejected[label]['phase_events'] == []
            assert rejected[label]['diagnosis']['code'] == 'multiple_tests'
        output['checks'].append('multiple_selection_stops_before_fixture_and_body')
        window.grab().save(str(root / 'selection-rejected.png'))
        empty = run(case('零测试被阻止', '# no tests\n'))
        assert empty['diagnosis']['before']['code'] == 'no_tests'
        output['checks'].append('zero_selection_records_rejection')
        good = case('单测试完整交付', 'from sample import compute\ndef test_one(): assert compute() == 42\n')
        verified = run(good)
        assert verified['status'] == 'verified'
        directory = Path(verified['report_dir'])
        assert check_report(directory)['status'] == 'consistent'
        old_json = (directory / 'report.json').read_bytes()
        archive = root / 'evidence.zip'
        pack_report(directory, archive)
        imported = root / 'imports' / 'saved-case'
        unpack_report(archive, imported)
        replay = run(imported / 'evidence/case.json')
        assert replay['status'] == 'verified'
        output['checks'].append('complete_evidence_pack_import_replay')
        window.grab().save(str(root / 'verified.png'))
        before_count = len(list((root / 'reports').glob('*/report.json')))
        def fail(*args, **kwargs):
            raise OSError('teaching disk-full failure')
        report.write_report = fail
        failed = run(good)
        report.write_report = original_write
        assert failed is None and '无法完成' in window.verdict_label.text()
        assert 'saving' in window.status_label.text() and 'disk-full' in window.status_label.text()
        assert len(list((root / 'reports').glob('*/report.json'))) == before_count
        records = [json.loads(p.read_text()) for p in (root / 'reports').glob('*/operation.json')]
        assert any(r['status'] == 'incomplete' and len(r['completed']) == 2 for r in records)
        assert (directory / 'report.json').read_bytes() == old_json
        output['checks'].append('write_failure_no_success_keeps_old_report')
        window.grab().save(str(root / 'save-failure.png'))
        (directory / 'before.log').write_text('teaching changed log')
        assert check_report(directory)['status'] == 'inconsistent'
        output['checks'].append('changed_log_detected')
        window.refresh()
        wait()
        assert any('未完成' in w for w in store.catalog()['warnings'])
        output['checks'].append('incomplete_attempt_visible_after_refresh')
        output.update(ok=True, report_dir=replay['report_dir'])
        window.close()
    except Exception:
        output.update(ok=False, error=traceback.format_exc())
        if window and window.task:
            window.task.control.cancel()
            window.task.wait(15000)
    finally:
        report.write_report = original_write
    (root / 'reliability-result.json').write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
