"""Explicit pytest order controls for comparisons over saved evidence."""
import hashlib
import json
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
