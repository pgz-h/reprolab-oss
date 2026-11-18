"""Confirmed replay must retain the report identity shown at preview."""
import json

import pytest

from reprolab import go_project, project_history, project_sessions, vitest_project
from reprolab.runner import RunCancelled, RunControl


@pytest.mark.parametrize(('runner', 'module'), [('go', go_project), ('vitest', vitest_project)])
def test_replay_rechecks_reference_and_propagates_control(tmp_path, monkeypatch, runner, module):
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'report.json'
    report = {'kind': 'project_regression', 'schema_version': 1, 'runner': runner,
              'run_id': 'original-run', 'project_id': 'original-project',
              'title': 'original title', 'entries': []}
    path.write_text(json.dumps(report), encoding='utf-8')
    reference = project_history.report_reference(project_history.read_report_snapshot(source))
    output = tmp_path / 'reports'
    controls, dispatches = [], []

    def check(directory, *, control):
        controls.append(control)
        return {'status': 'consistent', 'report_dir': str(source), 'issues': []}

    def replay(folder, report, output, *, confirmed, control, **options):
        dispatches.append((confirmed, control))
        return {'status': 'ready'}

    monkeypatch.setattr(project_history, 'check_project_report', check)
    monkeypatch.setattr(module, f'replay_{runner}_project', replay)
    preview_control = RunControl()
    assert project_sessions.replay_project_report(
        source, output, expected_report=reference, control=preview_control,
    )['status'] == 'ready'
    expected_controls = [preview_control]
    if runner == 'vitest':
        cache_control = RunControl()
        with pytest.raises(ValueError, match='npm 缓存须位于来源报告目录之外'):
            project_sessions.replay_project_report(
                source, output, expected_report=reference, control=cache_control, confirmed=True,
                npm_cache=source / 'npm-cache',
            )
        expected_controls.append(cache_control)
    report['title'] = 'changed title'
    path.write_text(json.dumps(report), encoding='utf-8')
    run_control = RunControl()
    with pytest.raises(ValueError, match='来源身份不符'):
        project_sessions.replay_project_report(
            source, output, expected_report=reference, control=run_control, confirmed=True,
        )
    cancelled = RunControl()
    cancelled.cancel()
    with pytest.raises(RunCancelled):
        project_sessions.replay_project_report(
            source, output, expected_report=reference, control=cancelled, confirmed=True,
        )
    assert controls == [*expected_controls, run_control]
    assert dispatches == [(False, preview_control)]
    assert not output.exists()
