"""Check a complete declared pytest selection across independently saved reports."""
from collections import Counter

from .project_history import _checked_report
from .project_review_summary import _review_summary
from .project_selections import read_selection_values, validate_selection
from .replay_verification import open_replay_evidence, _conditions


def check_project_shards(selection, locations, *, policy, control):
    requested = read_selection_values(selection)
    if not 1 <= len(requested) <= 10000 or not 1 <= len(locations) <= 100:
        raise ValueError('分片核对支持 1–10000 项期望测试和 1–100 份报告。')
    targets = [identity for start in range(0, len(requested), 2000)
               for identity in validate_selection(requested[start:start + 2000])]
    if targets != requested or len(set(targets)) != len(targets):
        raise ValueError('期望测试清单须包含唯一完整测试 ID。')
    wanted = set(targets)
    root = None
    observed, parts, issues = Counter(), [], []
    failed = blocked = False
    fixed_fields = ('source_sha256', 'source_mode_sha256', 'before_commit', 'after_commit')
    for location in locations:
        with open_replay_evidence(location, control=control) as source:
            report = _checked_report(source, control=control)
            if report.get('runner', 'pytest') != 'pytest':
                raise ValueError('分片核对接受 pytest 项目报告。')
            reference = report['_report_sha256']
            if root is None:
                root = report
            comparable = all(report.get(key) is not None and report.get(key) == root.get(key) for key in fixed_fields)
            if not comparable:
                issues.append({'report_sha256': reference, 'reason': 'source_changed'})
                continue
            conditions, _ = _conditions(root, report, 'pytest')
            if any(value is False for value in conditions.values()):
                issues.append({'report_sha256': reference, 'reason': 'recorded_conditions_changed'})
                continue
            selected = report['selected_tests']
            observed.update(selected)
            summary = _review_summary(report, False, policy, None)
            gate = summary['gate']
            failed |= gate['status'] == 'failed'
            blocked |= gate['status'] == 'blocked'
            parts.append({'report_sha256': reference, 'selected_count': len(selected), 'gate': gate,
                          'conditions': conditions})
    missing = [identity for identity in targets if identity not in observed]
    duplicate = sorted(identity for identity, count in observed.items() if count > 1)
    extra = sorted(observed.keys() - wanted)
    blocked |= bool(issues or missing or duplicate or extra)
    state = 'failed' if failed else 'blocked' if blocked else 'passed'
    code = {'failed': 1, 'blocked': 2, 'passed': 0}[state]
    return {'kind': 'project_shard_check', 'schema_version': 1, 'status': state, 'exit_code': code,
            'source_sha256': root['source_sha256'], 'policy': policy, 'expected_count': len(targets),
            'matched_count': len(observed.keys() & wanted), 'missing_tests': missing,
            'duplicate_tests': duplicate, 'unexpected_tests': extra, 'shards': parts, 'issues': issues,
            'notice': '核对冻结输入、记录条件和显式范围。分片各自运行，跨分片顺序和共享状态仍需另行验证。'}, code
