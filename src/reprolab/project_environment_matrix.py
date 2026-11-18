"""Review the same frozen comparison across distinct recorded Python environments."""
from .environment import compare_environments
from .project_history import _checked_report
from .project_review_summary import _review_summary
from .replay_verification import open_replay_evidence


def check_environment_matrix(locations, *, policy, control):
    if not 2 <= len(locations) <= 20:
        raise ValueError('环境矩阵需要 2–20 份比较报告。')
    fixed = ('source_sha256', 'source_mode_sha256', 'selected_tests', 'shared_tests', 'order_control',
             'fixture_control', 'input_control', 'test_source_control')
    root = None
    outputs, issues, fingerprints = [], [], set()
    for location in locations:
        with open_replay_evidence(location, control=control) as source:
            report = _checked_report(source, control=control)
            if report.get('runner', 'pytest') != 'pytest':
                raise ValueError('当前环境矩阵接受 pytest 比较报告。')
            if root is None:
                root = report
            digest = report['_report_sha256']
            if (any(report.get(key) != root.get(key) for key in fixed) or any(report.get(key) is None
                    for key in ('source_sha256', 'source_mode_sha256', 'shared_tests'))):
                issues.append({'report_sha256': digest, 'reason': 'frozen_inputs_changed'})
                continue
            if any(report.get('collection', {}).get(key, '') != root.get('collection', {}).get(key, '')
                   for key in ('collection_path',)):
                issues.append({'report_sha256': digest, 'reason': 'collection_scope_changed'})
                continue
            current = report.get('environment') or {}
            changes = compare_environments(root.get('environment'), current)
            fingerprint = current.get('fingerprint')
            if current.get('status') != 'recorded' or not fingerprint or changes['status'] == 'unknown':
                issues.append({'report_sha256': digest, 'reason': 'environment_unknown'})
                continue
            if fingerprint in fingerprints:
                issues.append({'report_sha256': digest, 'reason': 'duplicate_environment'})
                continue
            fingerprints.add(fingerprint)
            summary = _review_summary(report, False, policy, None)
            outputs.append({'report_sha256': digest, 'environment_sha256': fingerprint, 'gate': summary['gate'],
                            'runtime': {key: report['runtime'].get(key) for key in ('python_version', 'pytest_version')},
                            'distributions_changed_from_first': {key: changes[key] for key in ('added', 'removed', 'changed')},
                            'entries': [{'test_id': entry['test_id'],
                                         **{side: entry[side]['status'] for side in ('before', 'after')}}
                                        for entry in report['entries']]})
    statuses = {cell['gate']['status'] for cell in outputs}
    status = 'failed' if 'failed' in statuses else 'blocked' if issues or 'blocked' in statuses else 'passed'
    code = {'failed': 1, 'blocked': 2, 'passed': 0}[status]
    return {'kind': 'project_environment_matrix', 'schema_version': 1, 'status': status, 'exit_code': code,
            'source_sha256': root['source_sha256'], 'selected_tests': root['selected_tests'],
            'policy': policy, 'environments': outputs, 'issues': issues,
            'notice': '每个环境保存各自的两版结果。包清单记录版本，运行条件仍须结合实际部署核对。'}, code
