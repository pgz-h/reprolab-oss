"""Explicit pytest order controls for comparisons over saved evidence."""
import hashlib
import json
from pathlib import Path
import re


def validate_order_control(order, selected):
    if order is None:
        return
    if (not isinstance(order, dict) or set(order) != {'mode', 'source_report_sha256', 'test_ids'}
            or order['mode'] not in ('baseline', 'requested')
            or not isinstance(order['source_report_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', order['source_report_sha256'])):
        raise ValueError('pytest 顺序对照记录无效。')
    tests = order['test_ids']
    if (not isinstance(tests, list) or len(tests) != len(selected)
            or not all(isinstance(test, str) for test in tests) or set(tests) != set(selected)):
        raise ValueError('顺序对照需要涵盖原报告全部所选测试的完整顺序。')


def order_digest(test_ids):
    return hashlib.sha256(json.dumps(test_ids, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def order_control_completed(report):
    order = report['order_control']['test_ids']
    return (report['status'] != 'cancelled' and not report['counts'].get('inconclusive')
            and all(report[side]['status'] == 'completed' and report[side].get('execution_order') == order
                    for side in ('before', 'after')))


def _legacy_started_tests(report, side):
    from .project_sessions import _read

    name = f'{side}.events.json'
    try:
        digest = report['persistence']['sha256'][name]
        if not isinstance(digest, str):
            return None
        raw = _read(Path(report['report_dir']) / name, expected_sha256=digest)
        if raw.get('mode') == 'run' and raw.get('requested_tests') == report['selected_tests']:
            return raw.get('started_tests')
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return None


def observed_pytest_orders(report):
    """Read start order from a checked report while its evidence directory is available."""
    if report.get('runner', 'pytest') != 'pytest':
        return {side: {'status': 'not_applicable', 'source': None, 'started_tests': None}
                for side in ('before', 'after')}
    orders = {}
    selected = set(report['selected_tests'])
    for side in ('before', 'after'):
        run = report[side]
        if run['status'] == 'not_run':
            orders[side] = {'status': 'not_run', 'source': None, 'started_tests': []}
            continue
        progress = run.get('progress')
        recorded = progress is not None and progress.get('status') == 'recorded'
        started = run.get('execution_order') if recorded else None
        source = 'progress_journal'
        if started is None and (progress is None or recorded) and run.get('worker_record_complete') is True:
            started, source = _legacy_started_tests(report, side), 'worker_events'
        if (not isinstance(started, list) or not all(isinstance(test, str) for test in started)
                or len(set(started)) != len(started) or not set(started) <= selected):
            orders[side] = {'status': 'unknown', 'source': None, 'started_tests': None}
            continue
        complete = (run['status'] == 'completed' and run.get('worker_record_complete') is True
                    and set(started) == selected
                    and (progress is None or (progress.get('session_finished') is True
                                              and progress.get('tail_ignored') is False
                                              and progress.get('completed') == len(selected))))
        orders[side] = {'status': 'complete' if complete else 'partial', 'source': source, 'started_tests': started}
    return orders
