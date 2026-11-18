"""Report observed first-attempt outcomes independently of terminal test verdicts."""
from collections import Counter
from pathlib import Path

from .project_history import _checked_report
from .project_sessions import _read
from .replay_verification import open_replay_evidence


def _pytest_first(events):
    if not events or events[0].get('phase') != 'setup':
        return 'unknown'
    end = next((index for index, event in enumerate(events) if event.get('phase') == 'teardown'), None)
    if end is None:
        return 'unknown'
    attempt = events[:end + 1]
    phases = [event.get('phase') for event in attempt]
    if phases not in (['setup', 'call', 'teardown'], ['setup', 'teardown']) or any(event.get('xfail') for event in attempt):
        return 'unknown'
    outcomes = [event.get('outcome') for event in attempt]
    if 'failed' in outcomes:
        return 'failed'
    if 'skipped' in outcomes:
        return 'skipped'
    return 'passed' if outcomes == ['passed', 'passed', 'passed'] else 'unknown'


def _vitest_first(outcome):
    if outcome.get('expected_failure') or outcome.get('repeat_count') != 0:
        return 'unknown'
    retries = outcome.get('retry_count')
    status = outcome.get('status')
    if type(retries) is not int:
        return 'unknown'
    if status in ('passed', 'failed'):
        return 'failed' if retries else status
    return 'skipped' if status == 'skipped' else 'unknown'


def _observations(report, side):
    runner = report.get('runner', 'pytest')
    entries = report['entries']
    if runner == 'vitest':
        rows = []
        for entry in entries:
            cases = entry.get('case_entries') or ()
            if not cases:
                rows.append({'file': entry['test_id'], 'test_id': None, 'first_attempt': 'unknown'})
            for case in cases:
                rows.append({'file': entry['test_id'], 'test_id': case['test_id'],
                             'first_attempt': _vitest_first(case[side]),
                             'terminal': case[side].get('status')})
        return rows
    if runner == 'pytest':
        raw = _read(Path(report['report_dir']) / f'{side}.events.json')
        events = raw.get('events') or {}
        return [{'test_id': entry['test_id'], 'terminal': entry[side]['status'],
                 'first_attempt': _pytest_first(events.get(entry['test_id']) or entry[side].get('phases', ()))}
                for entry in entries]
    return [{'test_id': entry['test_id'], 'terminal': entry[side]['status'],
             'first_attempt': entry[side]['status'] if entry[side]['status'] in ('passed', 'failed', 'skipped')
             and report[side]['status'] == 'completed' else 'unknown'} for entry in entries]


def first_attempts(locations, *, control):
    rows, seen = [], set()
    if not 1 <= len(locations) <= 100:
        raise ValueError('请提供 1–100 份不同的报告。')
    for location in locations:
        with open_replay_evidence(location, control=control) as folder:
            report = _checked_report(folder, control=control)
            fingerprint = report['_report_sha256']
            if fingerprint in seen:
                raise ValueError('首轮统计不能重复同一份报告。')
            seen.add(fingerprint)
            runs = {}
            for side in ('before', 'after'):
                attempts = _observations(report, side)
                counts = Counter(row['first_attempt'] for row in attempts)
                denominator = counts['failed'] + counts['passed']
                runs[side] = {'counts': {name: counts[name] for name in ('passed', 'failed', 'skipped', 'unknown')},
                              'observed_denominator': denominator,
                              'observed_failure_rate': counts['failed'] / denominator if denominator else None,
                              'tests': attempts}
            rows.append({'report_sha256': fingerprint, 'runner': report.get('runner', 'pytest'), 'runs': runs})
    return {'kind': 'project_first_attempts', 'schema_version': 1, 'reports': rows,
            'notice': '按报告分开统计。只用有通过或失败证据的首次尝试作为分母；跳过、无法识别及不同环境不会被当作成功。'}
