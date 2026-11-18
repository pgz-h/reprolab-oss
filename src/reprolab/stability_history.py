"""Find stability checks for one report and reconstruct them offline."""
from pathlib import Path
import re

from .project_history import _checked_report, _match_reference, report_reference
from .project_sessions import _read
from .replay_stability import (
    MAX_ROUNDS, MIN_ROUNDS, STABILITY_PREFIX, STABILITY_STATES, STABILITY_SUMMARY, stability_result,
)
from .replay_verification import compare_replay_reports, open_replay_evidence
from .runner import RunControl


_IDENTITY_FIELDS = ('project_id', 'run_id', 'report_sha256')
_ROUND_ID = re.compile(r'(?:project|go|vitest)-run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}')
_MAX_HISTORY = 50


def _saved_record(path):
    record = _read(path)
    if (record.get('kind') != 'project_replay_stability'
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1):
        raise ValueError('请选择 ReproLab 稳定性摘要。')
    source = record.get('source')
    rounds = record.get('rounds')
    requested = record.get('requested_rounds')
    if (not isinstance(source, dict) or any(not isinstance(source.get(key), str) for key in _IDENTITY_FIELDS)
            or type(requested) is not int or not MIN_ROUNDS <= requested <= MAX_ROUNDS
            or not isinstance(rounds, list) or len(rounds) > requested
            or record.get('status') not in STABILITY_STATES):
        raise ValueError('稳定性摘要缺少来源、轮数或状态。')
    return record


def _belongs(record, reference):
    return all(record['source'][key] == reference[key] for key in _IDENTITY_FIELDS)


def list_stability_history(source, output_root, *, expected_report=None, control=None):
    """List saved checks for the exact original report, without reading their runs."""
    control = control or RunControl()
    with open_replay_evidence(source, control=control) as directory:
        original = _checked_report(directory, control=control)
    reference = report_reference(original)
    if expected_report is not None:
        _match_reference(original, expected_report)
    output = Path(output_root).expanduser().resolve()
    history = []
    truncated = False
    skipped = 0
    for folder in sorted(output.glob(f'{STABILITY_PREFIX}*'), reverse=True):
        control.check()
        if folder.is_symlink() or not folder.is_dir():
            continue
        path = folder / STABILITY_SUMMARY
        try:
            record = _saved_record(path)
        except (OSError, ValueError):
            skipped += 1
            continue
        if _belongs(record, reference):
            if len(history) >= _MAX_HISTORY:
                truncated = True
                break
            history.append({'check_id': folder.name, 'summary_file': str(path), 'recorded_status': record['status'],
                            'requested_rounds': record['requested_rounds'], 'saved_rounds': len(record['rounds'])})
    return {'kind': 'project_stability_history', 'schema_version': 1, 'checks': history,
            'truncated': truncated, 'skipped': skipped}


def check_saved_stability(source, summary, *, expected_report=None, control=None):
    """Read sibling round evidence and recalculate, ignoring the recorded outcomes."""
    control = control or RunControl()
    path = Path(summary).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('请选择实际的稳定性摘要或检查目录。')
    if path.is_dir():
        path /= STABILITY_SUMMARY
    record = _saved_record(path)
    with open_replay_evidence(source, control=control) as directory:
        original = _checked_report(directory, control=control)
    reference = report_reference(original)
    if expected_report is not None:
        _match_reference(original, expected_report)
    if not _belongs(record, reference):
        raise ValueError('稳定性摘要与原始报告的运行 ID 或指纹不一致。')

    rounds = []
    issues = []
    used_runs = {original['run_id']}
    reports = path.parent / 'reports'
    for number, item in enumerate(record['rounds'], 1):
        control.check()
        try:
            if not isinstance(item, dict) or type(item.get('round')) is not int or item['round'] != number:
                raise ValueError('轮次顺序无效。')
            proof = item.get('verification')
            if not isinstance(proof, dict) or not isinstance(replay := proof.get('replay'), dict):
                raise ValueError('没有可核对的报告身份记录。')
            run_id = replay.get('run_id')
            if not isinstance(run_id, str) or not _ROUND_ID.fullmatch(run_id) or run_id in used_runs:
                raise ValueError('轮次报告的运行 ID 无效或重复。')
            folder = reports / run_id
            if reports.is_symlink() or folder.is_symlink() or not folder.is_dir():
                raise ValueError('请选择稳定性目录内保存的轮次报告。')
            report = _checked_report(folder, control=control)
            if report['run_id'] != run_id or report['_report_sha256'] != replay.get('report_sha256'):
                raise ValueError('轮次报告与保存的运行 ID 或指纹不一致。')
            verification = compare_replay_reports(original, report)
        except (OSError, ValueError) as exc:
            issues.append({'round': number, 'message': str(exc)})
            continue
        used_runs.add(run_id)
        rounds.append({'round': number, 'report_dir': report['report_dir'], 'verification': verification})

    recorded_status = record['status']
    status = 'cancelled' if recorded_status == 'cancelled' else 'complete'
    if status != 'cancelled' and (issues or recorded_status in ('running', 'incomplete')):
        status = 'incomplete'
    saved_source = {key: reference[key] for key in _IDENTITY_FIELDS}
    saved_source['path'] = str(Path(source).expanduser().resolve())
    notes = {'running': '该记录保存于运行过程中，请按已取得的轮次查看。',
             'incomplete': '该检查保存时尚未取得完整结论。'}
    issue = notes.get(recorded_status)
    result = stability_result(saved_source, path, record['requested_rounds'], rounds, status, issue=issue)
    return {**result, 'history_issues': issues}
