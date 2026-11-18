"""Summarize paired, independently run pytest call durations with an observed noise floor."""
import math
import statistics

from .project_history import _checked_report
from .project_review_summary import _review_summary
from .replay_verification import _conditions, open_replay_evidence


def _call_seconds(outcome):
    if outcome.get('status') != 'passed':
        return None
    calls = [event['duration_seconds'] for event in outcome.get('phases', ())
             if event.get('phase') == 'call' and event.get('outcome') == 'passed']
    if len(calls) != 1 or not math.isfinite(calls[0]) or calls[0] <= 0:
        return None
    return calls[0]


def _statistics(pairs, ratio_floor, delta_floor):
    ratios = sorted(after / before for before, after in pairs)
    delta = sorted((after - before) * 1000 for before, after in pairs)
    middle, ratio = statistics.median(delta), statistics.median(ratios)
    low_index = (len(pairs) - 1) // 4
    low_delta, low_ratio = delta[low_index], ratios[low_index]
    if low_delta > delta_floor and low_ratio > ratio_floor:
        verdict = 'failed'
    elif middle > delta_floor and ratio > ratio_floor:
        verdict = 'blocked'
    else:
        verdict = 'passed'
    return {'status': verdict, 'median_ratio': ratio, 'median_delta_ms': middle,
            'lower_quartile_ratio': low_ratio, 'lower_quartile_delta_ms': low_delta,
            'mad_delta_ms': statistics.median(abs(value - middle) for value in delta)}


def check_performance(locations, *, selected, warmup, max_ratio, min_delta_ms, control):
    if not 0 <= warmup <= 10 or not 5 + warmup <= len(locations) <= 100:
        raise ValueError('至少需要 5 轮测量，最多 100 份；预热轮数可设为 0–10。')
    if not math.isfinite(max_ratio) or not math.isfinite(min_delta_ms) or max_ratio < 1 or min_delta_ms < 0:
        raise ValueError('性能阈值须为有限值，倍率不低于 1，耗时门槛不低于零。')
    anchor = None
    refs, pairs, drift = [], {}, []
    success = True
    regression = False
    seen = set()
    for index, location in enumerate(locations):
        with open_replay_evidence(location, control=control) as source:
            report = _checked_report(source, control=control)
            if report.get('runner', 'pytest') != 'pytest':
                raise ValueError('配对性能门槛当前接受 pytest 项目报告。')
            digest = report['_report_sha256']
            if digest in seen:
                raise ValueError('同一份报告不能作为两轮测量。')
            seen.add(digest)
            if anchor is None:
                anchor = report
                scope = set(report['selected_tests'])
                targets = list(dict.fromkeys(selected or report['selected_tests']))
                if not set(targets) <= scope:
                    raise ValueError('性能目标须包含在原始选择中。')
                pairs = {name: [] for name in targets}
            refs.append({'report_sha256': digest, 'warmup': index < warmup})
            fixed = ('source_sha256', 'source_mode_sha256', 'shared_tests', 'selected_tests', 'clock_control',
                     'input_control', 'order_control', 'test_source_control', 'fixture_control', 'version_order')
            same = all(report.get(key) == anchor.get(key) for key in fixed)
            conditions, _ = _conditions(anchor, report, 'pytest')
            if not same or not all(value is True for value in conditions.values()):
                drift.append({'report_sha256': digest, 'reason': 'different_or_unrecorded_conditions'})
                success = False
                continue
            gate = _review_summary(report, False, 'strict', None)['gate']
            if gate['status'] != 'passed':
                regression |= gate['status'] == 'failed'
                success = False
                continue
            if index < warmup:
                continue
            for entry in report['entries']:
                if entry['test_id'] not in pairs:
                    continue
                measure = tuple(_call_seconds(entry[side]) for side in ('before', 'after'))
                if None not in measure:
                    pairs[entry['test_id']].append(measure)
    measurements = []
    for identity, values in pairs.items():
        if len(values) != len(locations) - warmup:
            check = {'status': 'blocked', 'reason': 'incomplete_measurement'}
        else:
            check = _statistics(values, max_ratio, min_delta_ms)
        measurements.append({'test_id': identity, 'paired_rounds': len(values), **check})
    states = {row['status'] for row in measurements}
    status = ('failed' if regression or 'failed' in states and success else 'blocked'
              if not success or 'blocked' in states else 'passed')
    code = {'passed': 0, 'failed': 1, 'blocked': 2}[status]
    return {'kind': 'project_performance_check', 'schema_version': 1, 'status': status,
            'reports': refs, 'issues': drift, 'warmup_rounds': warmup, 'max_ratio': max_ratio,
            'min_delta_ms': min_delta_ms, 'measurements': measurements,
            'notice': '同条件配对的 pytest call 时间。噪声大的结果保留受阻。未控制的系统负载及执行顺序仍可影响计时。'}, code
