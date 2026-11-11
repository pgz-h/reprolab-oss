import json
from pathlib import Path

import pytest

from reprolab import runner, report
from reprolab.evidence import check_report
from test_runner import make_case


def test_html_failure_cannot_leave_a_verified_json(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('disk full while saving HTML')
    monkeypatch.setattr(report, 'write_report', fail)
    with pytest.raises((OSError, ValueError), match='disk full'):
        runner.run_case(make_case(tmp_path), tmp_path / 'reports')
    assert not list((tmp_path / 'reports').rglob('report.json'))
    records = list((tmp_path / 'reports').glob('*/operation.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['status'] == 'incomplete'
    assert record['phase'] == 'saving'
    assert set(record['completed']) == {'before', 'after'}


def test_run_case_returns_only_after_all_report_files_are_saved(tmp_path):
    result = runner.run_case(make_case(tmp_path), tmp_path / 'reports')
    assert check_report(result['report_dir'])['status'] == 'consistent'
    assert result['persistence']['status'] == 'complete'


@pytest.mark.parametrize('name', ['before.log', 'after.events.json', 'report.html'])
def test_new_reports_detect_changed_execution_evidence(tmp_path, name):
    result = runner.run_case(make_case(tmp_path), tmp_path / 'reports')
    report.write_report(result)
    path = Path(result['report_dir']) / name
    path.write_text('changed after publication')
    checked = check_report(result['report_dir'])
    assert checked['status'] == 'inconsistent'
    assert any(name in issue for issue in checked['issues'])


def test_evidence_changed_between_runs_cannot_be_published(tmp_path):
    case = make_case(tmp_path)
    def progress(phase, result):
        if phase == 'after_done':
            saved_test = next((tmp_path / 'reports').glob('*/evidence/regression.py'))
            saved_test.write_text('def test_different(): pass\n')
    with pytest.raises((OSError, ValueError), match='证据|变化'):
        runner.run_case(case, tmp_path / 'reports', control=runner.RunControl(progress))
    assert not list((tmp_path / 'reports').rglob('report.json'))


def test_missing_log_before_commit_is_not_verified(tmp_path):
    def progress(phase, result):
        if phase == 'after_done':
            next((tmp_path / 'reports').glob('*/before.log')).unlink()
    with pytest.raises((OSError, ValueError)):
        runner.run_case(make_case(tmp_path), tmp_path / 'reports', control=runner.RunControl(progress))
    assert not list((tmp_path / 'reports').rglob('report.json'))


def test_final_json_replace_failure_keeps_only_incomplete_materials(tmp_path, monkeypatch):
    from reprolab import persistence
    real_replace = persistence.os.replace
    def fail(source, destination):
        if Path(destination).name == 'report.json':
            raise OSError('cannot commit final report')
        return real_replace(source, destination)
    monkeypatch.setattr(persistence.os, 'replace', fail)
    with pytest.raises(ValueError, match='cannot commit final report'):
        runner.run_case(make_case(tmp_path), tmp_path / 'reports')
    assert not list((tmp_path / 'reports').glob('*/report.json'))
    assert not list((tmp_path / 'reports').glob('*/report.html'))
    record = json.loads(next((tmp_path / 'reports').glob('*/operation.json')).read_text())
    assert record['status'] == 'incomplete'
    assert len(record['completed']) == 2


def test_desktop_reports_incomplete_attempts_after_restart(tmp_path):
    from reprolab.desktop_store import DesktopStore
    store = DesktopStore(tmp_path / 'state')
    root = tmp_path / 'reports'
    root.mkdir()
    partial = root / 'partial'
    partial.mkdir()
    (partial / 'operation.json').write_text(json.dumps({'status': 'incomplete', 'phase': 'saving'}))
    store.settings['reports'] = str(root)
    catalog = store.catalog()
    assert not catalog['reports']
    assert any('未完成' in warning and '1' in warning for warning in catalog['warnings'])


@pytest.mark.parametrize('name', ['before.events.json', 'evidence/case.json'])
def test_changed_execution_or_replay_record_before_commit_is_rejected(tmp_path, name):
    def progress(phase, result):
        if phase == 'after_done':
            path = next((tmp_path / 'reports').glob('*/' + name))
            path.write_text('{}')
    with pytest.raises(ValueError, match='证据|变化'):
        runner.run_case(make_case(tmp_path), tmp_path / 'reports', control=runner.RunControl(progress))
    assert not list((tmp_path / 'reports').glob('*/report.json'))


def test_batch_html_failure_does_not_publish_a_verified_summary(tmp_path, monkeypatch):
    import time
    from reprolab import batch
    summary = {'kind':'suite', 'schema_version':1, 'batch_id':'teaching', 'summary_dir':str(tmp_path),
               'total':1, 'entries':[{'status':'verified', 'check_mode':'repair'}]}
    def failed_html(*args, **kwargs):
        raise OSError('teaching summary save failure')
    monkeypatch.setattr(batch, 'write_batch_report', failed_html)
    with pytest.raises(OSError, match='summary save failure'):
        batch._save(summary, time.monotonic())
    assert not (tmp_path / 'summary.json').exists()
