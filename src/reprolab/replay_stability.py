"""Measure recorded test outcomes across independent replays of one report."""
from collections import Counter
import json
from pathlib import Path

from .persistence import save_json
from .project_history import _checked_report, _match_reference, report_reference
from .project_sessions import replay_project_report
from .repair_suite import _new_id
from .replay_verification import open_replay_evidence
from .runner import RunCancelled, RunControl


MIN_ROUNDS = 2
MAX_ROUNDS = 10
DEFAULT_ROUNDS = 3
STABILITY_PREFIX = 'replay-stability-'
STABILITY_SUMMARY = 'replay-stability.json'
STABILITY_STATES = ('running', 'verified', 'different', 'incomplete', 'cancelled')
OUTCOME_LABELS = {'matched': '每轮复现', 'consistently_different': '持续偏离', 'variable': '轮间变化',
                  'incomplete': '记录不足'}


def _observations(rounds, requested):
    by_round = [{} for _ in range(requested)]
    identities = {}
    for item in rounds:
        verification = item['verification']
        rows = {}
        for entry in verification.get('entries', ()):
            key = (entry.get('file'), entry.get('project'), entry['test_id'])
            if key not in identities or identities[key] is None:
                identities[key] = entry['source']
            identity_checks = verification.get('recorded_failure_identity')
            same_conditions = not identity_checks or False not in verification['conditions'].values()
            if (entry['status'] in ('matched', 'different') and all(verification['inputs'].values()) and same_conditions):
                change = ((entry.get('failure_fingerprint_changes') or entry.get('failure_changes'))
                          if identity_checks else None)
                rows[key] = (entry['replay'], change)
        by_round[item['round'] - 1] = rows

    entries = []
    for (file, project, name), source in identities.items():
        pairs = [rows.get((file, project, name)) for rows in by_round]
        observed = [item[0] if item is not None else None for item in pairs]
        changes = [item[1] if item is not None else None for item in pairs]
        distinct = {(pair['before'], pair['after'], json.dumps(change, sort_keys=True))
                    for pair, change in zip(observed, changes) if pair is not None}
        if len(distinct) > 1:
            status = 'variable'
        elif None in observed:
            status = 'incomplete'
        else:
            status = 'matched' if observed[0] == source and not any(changes) else 'consistently_different'
        identity = {'test_id': name}
        if file is not None:
            identity.update(file=file, project=project)
        entries.append({**identity, 'source': source, 'rounds': observed, 'status': status,
                        'observed_rounds': sum(pair is not None for pair in observed),
                        **({'recorded_failure_changes': changes} if any(changes) else {})})
    return entries


def stability_result(reference, summary_file, requested, rounds, status, *, issue=None):
    entries = _observations(rounds, requested)
    counts = Counter(entry['status'] for entry in entries)
    if status == 'complete':
        incomplete_round = any(item['verification']['status'] not in ('verified', 'different') for item in rounds)
        if len(rounds) != requested or incomplete_round or not entries or counts['incomplete'] or any(
            entry['observed_rounds'] < requested for entry in entries
        ):
            status = 'incomplete'
        elif counts['variable'] or counts['consistently_different']:
            status = 'different'
        else:
            status = 'verified'
    result = {'kind': 'project_replay_stability', 'schema_version': 1, 'status': status,
              'source': reference, 'requested_rounds': requested, 'completed_rounds': len(rounds),
              'rounds': rounds, 'counts': {key: counts[key] for key in OUTCOME_LABELS},
              'entries': entries, 'summary_file': str(summary_file)}
    if issue:
        result['issue'] = issue
    return result


def replay_stability(source, output_root, *, rounds=DEFAULT_ROUNDS, confirmed=False,
                     expected_report=None, control=None, **options):
    """Run the exact saved input sequentially and checkpoint each verified round."""
    if confirmed is not True:
        raise ValueError('请审阅可信证据后明确开始稳定性检查。')
    if type(rounds) is not int or not MIN_ROUNDS <= rounds <= MAX_ROUNDS:
        raise ValueError(f'稳定性检查须选择 {MIN_ROUNDS}–{MAX_ROUNDS} 轮。')
    control = control or RunControl()
    location = Path(source).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    with open_replay_evidence(location, control=control) as folder:
        original = _checked_report(folder, control=control)
        reference = report_reference(original)
        if expected_report is not None:
            _match_reference(original, expected_report)
        if output.is_relative_to(Path(original['report_dir'])) or output.is_relative_to(location):
            raise ValueError('稳定性检查的输出须位于来源证据之外。')
        source_identity = {key: reference[key] for key in ('project_id', 'run_id', 'report_sha256')}
        source_identity['path'] = str(location)
        directory = output / _new_id(STABILITY_PREFIX.rstrip('-'))
        control.check()
        directory.mkdir(parents=True, exist_ok=False)
        summary_file = directory / STABILITY_SUMMARY
        saved = []

        def checkpoint(status, issue=None):
            result = stability_result(source_identity, summary_file, rounds, saved, status, issue=issue)
            save_json(summary_file, result)
            return result

        checkpoint('running')
        status, issue = 'complete', None
        try:
            for number in range(1, rounds + 1):
                control.notify('stability_round', {'round': number, 'total': rounds})
                control.check()
                replay = replay_project_report(folder, directory / 'reports', confirmed=True,
                                               verify_result=True, expected_report=reference,
                                               control=control.child(progress=control.progress), **options)
                proof = replay['replay_verification']
                saved.append({'round': number, 'report_dir': replay['report_dir'], 'verification': proof})
                checkpoint('running')
                if replay['status'] == 'cancelled':
                    raise RunCancelled('稳定性检查已取消。')
                if proof['status'] in ('unavailable', 'inputs_changed', 'same_run'):
                    status, issue = 'incomplete', proof.get('issue') or proof['summary']
                    break
            control.commit()
        except (RunCancelled, KeyboardInterrupt):
            status, issue = 'cancelled', '稳定性检查已取消，已保存的报告可继续查看。'
        except (OSError, ValueError) as exc:
            status, issue = 'incomplete', str(exc)
        return checkpoint(status, issue)


def stability_exit_code(result):
    return {'verified': 0, 'different': 1}.get(result['status'], 2)


def stability_summary(result):
    state = {'verified': '每轮均复现', 'different': '观察到结果差异',
             'incomplete': '稳定性核对未完成', 'cancelled': '稳定性检查已取消'}[result['status']]
    counts = ' · '.join(f'{label} {result["counts"][key]}' for key, label in OUTCOME_LABELS.items())
    return f'{state} · {result["completed_rounds"]}/{result["requested_rounds"]} 轮有报告 · {counts}'
