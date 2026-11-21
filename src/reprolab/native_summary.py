"""Public-facing summary of a verified native evidence record."""
import hashlib


_LABELS = {
    'passed': 'Passed',
    'failed': 'Failed',
    'blocked': 'Blocked',
    'skipped': 'Skipped',
    'improved': 'Improved',
    'regression_detected': 'Confirmed new failure',
    'regression_passed': 'Passed on both sides',
    'baseline_failure': 'Failed on both sides',
    'native_only': 'Observations only',
    'missing_both': 'Missing on both sides',
    'only_before': 'Observed only before',
    'only_after': 'Observed only after',
    'same_snapshot_divergence': 'Pass/fail diverged on the same snapshot',
    'inconclusive': 'Inconclusive',
    'required_fixes_observed': 'Observed all required improvements',
    'historical_scope_unknown': 'Historical evidence lacks scope and input coverage',
    'excluded_tracked_changes': 'Tracked Git changes were excluded',
    'test_inputs_unknown': 'Test input coverage is unknown',
    'test_inputs_changed_or_missing': 'Declared test inputs changed or are missing',
    'comparison_blocked': 'The ordinary comparison is blocked',
    'baseline_failure_marker_unverified': 'Required baseline failure markers could not be confirmed',
    'baseline_failure_markers_unsupported': 'This runner cannot verify baseline failure markers',
    'missing': 'Missing',
    'required_improvement_missing': 'A required test did not improve',
    'review_plan_digest_mismatch': 'Receiver plan bytes differ from the plan recorded at execution',
    'pr_review_plan_unrecorded': 'Evidence does not record a PR review plan',
    'review_plan_scope_mismatch': 'Evidence scope differs from the receiver plan',
    'review_plan_test_files_changed_or_missing': 'Files in the receiver plan changed or are missing',
    'matched': 'Matched',
    'partial': 'Partial',
    'mismatch': 'Mismatch',
    'unavailable': 'Unavailable',
    'unrecorded': 'Unrecorded',
    'declared_commits_mismatch': 'Declared commits differ from the receiver plan',
    'git_objects_unavailable': 'Local Git could not supply the required source or diff',
    'git_snapshot_mismatch': 'Frozen snapshot differs from local Git',
    'git_omitted_changes_mismatch': 'Excluded change count differs from local Git',
    'git_omitted_changes_unrecorded': 'Historical evidence lacks an excluded change count',
    'git_snapshots_and_omissions_matched': 'Snapshots and excluded change count match local Git',
    'runner': 'runner',
    'commits': 'Git commit pair',
    'selected_tests': 'selected tests',
    'shared_test_sources': 'test definitions',
    'shared_test_inputs': 'test support inputs',
}
_COVERAGE = {
    'sender_attested': 'Sender attested',
    'changed_or_missing': 'Changed or missing',
    'unknown': 'Unknown',
}
_RUNNERS = {'cmake': 'CMake/CTest', 'cargo': 'Cargo'}
_PR_SETUP_ERRORS = {
    'pr_parent_missing': 'Local Git lacks the target or PR head commit.',
    'pr_history_shallow': 'Head comparison requires complete local Git history.',
    'pr_merge_base_missing': 'Local Git lacks the common baseline commit.',
    'pr_merge_missing': 'Local Git lacks the specified PR merge commit.',
    'pr_merge_parent_mismatch': 'The merge commit parents differ from the event target and head.',
}


def _label(value):
    return _LABELS.get(value, 'Unknown')


def _identity(value):
    return hashlib.sha256(value.encode('utf-8', errors='surrogatepass')).hexdigest()[:16]


def render_native_review_error(reason, *, cancelled):
    status = 'Cancelled' if cancelled else 'Blocked'
    message = _PR_SETUP_ERRORS.get(reason, 'Verification ended before producing a result.')
    return ('### ReproLab native evidence\n\n'
            f'- Review · {status} (exit 2)\n'
            '- Evidence integrity · Unverified\n'
            f'- {message} See the CLI error in the job log.\n')


def render_native_markdown(record):
    comparison = record['comparison']
    gate = comparison['gate']
    scope = record['snapshot_scope']
    selected = record['selected_tests']
    rules = 'Historical schema 1' if record['schema_version'] == 1 else 'Current schema 2'
    failure = ' · Confirmed new failure' if gate['status'] == 'failed' else ''
    lines = [
        '### ReproLab native evidence', '',
        '- Evidence integrity · Consistent',
        f'- Evidence rules · {rules}',
        f'- Ordinary comparison · {_label(gate["status"])} (exit {gate["exit_code"]}){failure}',
    ]
    if scope.get('excluded_changes_only'):
        lines.append('- Comparison blocked · All observed Git changes were excluded from the snapshots.')
    elif comparison['counts'].get('same_snapshot_divergence'):
        lines.append('- Comparison blocked · Pass/fail diverged on the same snapshot.')
    acceptance = record.get('review_plan_acceptance') or record.get('fix_acceptance')
    if acceptance:
        name = 'Receiver plan acceptance' if 'review_plan_acceptance' in record else 'Required fix acceptance'
        lines.append(f'- {name} · {_label(acceptance["status"])} (exit {acceptance["exit_code"]})'
                     f' · {_label(acceptance["reason"])}')
        if pr := acceptance.get('pr_context'):
            mode = 'Head comparison' if pr['mode'] == 'head' else 'Merge comparison'
            lines.append(f'- PR target · `{pr["base"]}` · {mode}')
            lines.append(f'- PR head · `{pr["head"]}`')
        if fields := acceptance.get('mismatched_fields'):
            lines.append('- Plan scope mismatch · ' + ', '.join(map(_label, fields)))
        if missing := acceptance.get('unmatched_test_files'):
            lines.append(f'- Plan files changed or missing · {len(missing)}')
        if checks := acceptance.get('baseline_marker_checks'):
            confirmed = sum(status == 'matched' for status in checks.values())
            lines.append(f'- Baseline failure markers · {confirmed}/{len(checks)} matched')
        if digest := acceptance.get('plan_sha256'):
            lines.append(f'- Receiver plan SHA-256 · `{digest}`')
    elif 'verified_result' in record:
        lines.append('- Exit policy · Ordinary comparison')
    else:
        lines.append('- Exit policy · Evidence integrity only (exit 0)')

    if git := record.get('git_verification'):
        lines.append(f'- Receiver-supplied local Git objects · {_label(git["status"])} · {_label(git["reason"])}')
        for side, row in git.get('snapshots', {}).items():
            label = 'Before' if side == 'before' else 'After'
            lines.append(f'  - {label} · Content {_label(row["content"])} / permissions {_label(row["mode"])}')
        if count := git.get('excluded_tracked_changes'):
            observed = count['observed'] if count['observed'] is not None else 'Unknown'
            lines.append(f'  - Excluded changes · {observed} observed locally · {_label(count["status"])}')

    omitted = scope['excluded_tracked_changes']
    coverage = comparison.get('test_input_coverage', 'unknown')
    selected_count = f'{len(selected)} explicit' if selected is not None else 'Discovered'
    support_count = (str(len(record['shared_test_inputs'])) if 'shared_test_inputs' in record else 'Unknown')
    lines += [
        '',
        f'- Runner · {_RUNNERS[record["runner"]]}',
        f'- Declared commits · `{record["commits"]["before"]}` → `{record["commits"]["after"]}`',
        f'- Test selection · {selected_count}',
        f'- Compared tests · {comparison["shared_count"]} shared / {len(comparison["entries"])} reported',
        f'- Declared files · {len(record["shared_test_files"])} test definitions / {support_count} support inputs',
        f'- Test input coverage · {_COVERAGE[coverage]}',
        f'- Excluded tracked Git changes · {"Unknown" if omitted is None else str(omitted) + " (sender reported)"}',
    ]
    if comparison['counts']:
        lines += ['', '| Observation | Cases |', '| --- | ---: |']
        lines.extend(f'| {_label(outcome)} | {count} |' for outcome, count in sorted(comparison['counts'].items()))
    if acceptance and (targets := acceptance.get('targets')):
        lines += ['', '| Required test fingerprint | Observation |', '| --- | --- |']
        lines.extend(f'| `{_identity(name)}` | {_label(outcome)} |' for name, outcome in list(targets.items())[:20])
        if len(targets) > 20:
            lines.append(f'\nShowing 20 of {len(targets)} required tests.')
    entries = comparison['entries']
    if entries:
        prioritized = sorted(entries, key=lambda row: (
            row['status'] != 'regression_detected', row['status'] in ('improved', 'regression_passed')))
        lines += ['', '| Test fingerprint | Before | After | Observation |', '| --- | --- | --- | --- |']
        for row in prioritized[:20]:
            observation = _label(row['status'])
            if row.get('test_configuration') in ('changed', 'unverified'):
                observation += f' (CTest configuration {row["test_configuration"]})'
            lines.append(f'| `{_identity(row["test_id"])}` | {_label(row["before"])} | '
                         f'{_label(row["after"])} | {observation} |')
        if len(entries) > 20:
            lines.append(f'\nShowing 20 of {len(entries)} observed tests.')
    return '\n'.join(lines) + '\n'
