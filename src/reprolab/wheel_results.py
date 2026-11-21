"""Compare the pytest observations recorded for two installed wheel builds."""
from collections import Counter, defaultdict
import hashlib
import html
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .acceptance_plan import _unique_fields
from .case import parse_test_target
from .project_selections import validate_selection
from .runtime import valid_python_environment

MAX_RECORD_BYTES = 16 * 1024 * 1024
PHASE_FILE = 'pytest-phases.json'
OUTCOMES = {
    'improved': 'Improved',
    'regression_passed': 'Passed on both',
    'regression_detected': 'New failure',
    'baseline_failure': 'Failed on both',
    'same_wheel_divergence': 'Pass/fail diverged on the same wheel',
    'inconclusive': 'Uncertain',
}
EXIT_CODES = {'passed': 0, 'failed': 1, 'blocked': 2}

# Runs inside the selected interpreter and records no failure messages or parameter values
# other than the opaque node IDs already present in pytest's result records.
PHASE_PROBE = """
import json
class ReproLabPhases:
    def __init__(self, environment, pytest_version, test_installation):
        self.environment = environment
        self.pytest_version = pytest_version
        self.test_installation = test_installation
        self.events = []
        self.truncated = False
    def pytest_runtest_logreport(self, report):
        if len(self.events) >= 30000 or len(report.nodeid) > 4000:
            self.truncated = True
        else:
            self.events.append((report.nodeid, report.when, report.outcome, hasattr(report, 'wasxfail')))
    def pytest_sessionfinish(self, session, exitstatus):
        truncated = self.truncated or len(session.items) > 10000
        data = {'schema_version': 1, 'exit_code': int(exitstatus), 'truncated': truncated,
                'selected': [item.nodeid for item in session.items[:10000] if len(item.nodeid) <= 4000],
                'events': self.events, 'environment': self.environment, 'pytest_version': self.pytest_version,
                'test_installation': self.test_installation}
        raw = json.dumps(data).encode('utf-8')
        if len(raw) > 16 * 1024 * 1024:
            raw = b'{"schema_version":1,"truncated":true}'
        pathlib.Path(sys.argv[3]).write_bytes(raw)
"""


def junit_document(path, *, payload=None):
    if payload is None:
        if not path.is_file():
            return None
        with path.open('rb') as stream:
            payload = stream.read(MAX_RECORD_BYTES + 1)
    if len(payload) > MAX_RECORD_BYTES:
        return None
    try:
        # Pytest emits UTF-8 XML and has no need for document types or entities.
        text = payload.decode('utf-8-sig')
        if '<!DOCTYPE' in text or '<!ENTITY' in text:
            return None
        return ET.fromstring(text)
    except (UnicodeError, ET.ParseError, ValueError):
        return None


def junit_totals(document):
    if document is None:
        return None
    cases = list(document.iter('testcase'))
    flags = [{child.tag for child in row} for row in cases]
    return {'tests': len(cases), 'passed': sum(not fields & {'failure', 'error', 'skipped'} for fields in flags),
            **{key: sum(tag in fields for fields in flags)
               for key, tag in (('failed', 'failure'), ('errors', 'error'), ('skipped', 'skipped'))}}


def _selected_by(identity, selections):
    # Parameters are one opaque suffix, never split or normalized.
    exact, _, _ = identity.partition('[')
    return [target for target in selections if (target == identity or
            ('[' not in target and (target == exact or exact.startswith(target + '::'))))]


def _junit_key(identity):
    address, bracket, params = identity.partition('[')
    file, *names = address.split('::')
    return '.'.join([file.removesuffix('.py').replace('/', '.'), *names[:-1]]), names[-1] + bracket + params


def _legacy_id(row, files):
    classname, literal = row.get('classname', ''), row.get('name', '')
    name, bracket, params = literal.partition('[')
    if not name.isidentifier() or bracket and not params.endswith(']'):
        return None
    parts = classname.split('.')
    candidates = []
    for index in range(1, len(parts) + 1):
        classes = parts[index:]
        if not all(part.isidentifier() for part in classes):
            continue
        for file in files.get('.'.join(parts[:index]), ()):
            if row.get('file', file) == file:
                candidates.append('::'.join([file, *classes, literal]))
    return candidates[0] if len(candidates) == 1 else None


def _junit_cases(document):
    if document is None:
        return None
    if document.tag == 'testsuite':
        suites = [document]
    elif document.tag == 'testsuites' and all(child.tag == 'testsuite' for child in document):
        suites = list(document)
    else:
        return None
    if len(suites) != 1:
        return None
    suite = suites[0]
    if any(row.tag not in ('properties', 'testcase', 'system-out', 'system-err') for row in suite):
        return None
    rows = suite.findall('testcase')
    if not rows or len(rows) > 20000:
        return None
    fields = ['tests', 'failures', 'errors', 'skipped']
    try:
        declared = [int(suite.attrib[key]) for key in fields]
    except (KeyError, ValueError):
        return None
    if any(value < 0 for value in declared):
        return None
    return rows, declared


def _xml_outcome(row):
    findings = [child for child in row if child.tag not in ('properties', 'system-out', 'system-err')]
    if not findings:
        return 'passed'
    if (len(findings) == 1 and findings[0].tag == 'failure'
            and not findings[0].get('message', '').lstrip().startswith('[XPASS')):
        # In pytest JUnit a failure is a call report. Setup, teardown and
        # collection errors have separate tags. Strict XPASS is not a failing call.
        return 'failed'
    return 'uncertain'


_FAILURE_SITE = re.compile(r'^(.+):([1-9][0-9]*): ([A-Za-z_][A-Za-z0-9_.]*)$')


def _failure_identity(row, node):
    """Require pytest's location and exception type plus the same complete failure evidence."""
    failure = row.find('failure')
    text = failure.text or ''
    if not text.strip() or not failure.get('message'):
        return None
    site = _FAILURE_SITE.fullmatch(text.rstrip().splitlines()[-1])
    if site is None or site[1].replace('\\', '/') != parse_test_target(node):
        return None
    # Raw text can include private values, absolute paths, and volatile addresses.
    # Compare exact evidence in memory, with no attempt to erase differences.
    proof = [site[2], site[3], failure.get('message'), failure.get('type'), text]
    return hashlib.sha256(json.dumps(proof, ensure_ascii=True).encode()).digest()


def _phase_index(payload, exit_code, files):
    try:
        data = json.loads(payload)
        if (type(data['schema_version']) is not int or data['schema_version'] != 1
                or type(data['exit_code']) is not int or data['exit_code'] != exit_code
                or data['truncated'] is not False or not isinstance(data['selected'], list)
                or not isinstance(data['events'], list) or len(data['events']) > 30000
                or not 1 <= len(data['selected']) <= 10000):
            return None
        selected = data['selected']
        if any(not isinstance(node, str) or len(node) > 4000 or '::' not in node
               or parse_test_target(node) not in files for node in selected):
            return None
        counts = Counter(selected)
        events = defaultdict(list)
        for node, phase, outcome, xfail in data['events']:
            if (node not in counts or phase not in ('setup', 'call', 'teardown')
                    or outcome not in ('passed', 'failed', 'skipped') or type(xfail) is not bool):
                return None
            events[node].append((phase, outcome, xfail))
        return counts, events
    except (KeyError, TypeError, ValueError, RecursionError):
        return None


def recorded_test_context(payload, run):
    """Read execution details bound to a completed, hash-checked phase record."""
    exit_code = run.get('exit_code')
    if (not payload or run.get('status') != 'completed' or type(exit_code) is not int
            or exit_code not in (0, 1)):
        return {}
    try:
        data = json.loads(payload, object_pairs_hook=_unique_fields)
        if (type(data['schema_version']) is not int or data['schema_version'] != 1
                or data['truncated'] is not False
                or type(data['exit_code']) is not int or data['exit_code'] != exit_code):
            return {}
        environment = data.get('environment')
        installation = data.get('test_installation')
        context = {'environment': environment} if valid_python_environment(environment) else {}
        if installation == {'mode': 'existing_environment'}:
            context['installation'] = installation
        elif (isinstance(installation, dict) and installation.keys() == {'mode', 'wheelhouse_sha256'}
              and installation['mode'] == 'isolated_venv' and isinstance(installation['wheelhouse_sha256'], str)
              and re.fullmatch(r'[0-9a-f]{64}', installation['wheelhouse_sha256'])):
            context['installation'] = installation
        return context
    except (KeyError, TypeError, ValueError, RecursionError):
        return {}


def _phase_outcome(events):
    if ([row[0] for row in events] != ['setup', 'call', 'teardown'] or any(row[2] for row in events)
            or events[0][1] != 'passed' or events[2][1] != 'passed'):
        return 'uncertain'
    return events[1][1] if events[1][1] in ('passed', 'failed') else 'uncertain'


def _observations(run, document, phase_payload, selections, files):
    issues = Counter()
    if run.get('install_status') != 'completed' or run.get('install_exit_code') != 0:
        return {}, {'installation_incomplete': 1}
    exit_code = run.get('exit_code')
    if run.get('status') == 'completed' and exit_code == 74 and not phase_payload:
        return {}, {'module_origin_unverified': 1}
    if run.get('status') != 'completed' or type(exit_code) is not int or exit_code not in (0, 1):
        return {}, {'pytest_incomplete': 1}
    parsed = _junit_cases(document)
    if parsed is None:
        return {}, {'junit_unavailable_or_invalid': 1}
    rows, declared = parsed
    facts = junit_totals(document)
    if (declared[1:] != [facts['failed'], facts['errors'], facts['skipped']]
            or declared[0] > len(rows)
            or (exit_code == 0) != (facts['failed'] == facts['errors'] == 0)):
        return {}, {'junit_totals_or_exit_mismatch': 1}

    indexed = {}
    phases = None
    if phase_payload is not None:
        phases = _phase_index(phase_payload, exit_code, set(file for values in files.values() for file in values))
        if phases is None:
            return {}, {'phase_record_incomplete': 1}
        counts, events = phases
        for node in counts:
            indexed.setdefault(_junit_key(node), []).append(node)
    seen = defaultdict(list)
    matched = set()
    for row in rows:
        if phases is None:
            node = _legacy_id(row, files)
        else:
            options = indexed.get((row.get('classname'), row.get('name')), ())
            node = options[0] if len(options) == 1 else None
        if node is None:
            issues['unmapped_junit_case'] += 1
            continue
        scope = _selected_by(node, selections)
        if not scope:
            issues['outside_selection'] += 1
            continue
        matched.update(scope)
        outcome = _xml_outcome(row)
        seen[node].append((outcome, _failure_identity(row, node) if outcome == 'failed' else None))

    for target in set(selections) - matched:
        issues['selection_without_junit_case'] += 1
        # Only exact selectors can be rendered as a testcase with no observation.
        if '[' in target:
            seen[target].append(('uncertain', None))

    if phases is not None:
        for node in counts:
            if node not in seen:
                issues['collected_id_without_junit_case'] += 1
                if _selected_by(node, selections):
                    seen[node].append(('uncertain', None))
    # Pytest subtracts one suite test when it emits a second testcase for teardown
    # after a call failure. Other disagreements cannot certify suite completeness.
    repeats = sum(len(values) - 1 for values in seen.values())
    if len(rows) - declared[0] not in (0, repeats):
        return {}, {'junit_totals_or_exit_mismatch': 1}

    outcomes = {}
    for node, observations in seen.items():
        outcome, proof = observations[0]
        if len(observations) != 1 or phases is not None and counts[node] != 1:
            issues['duplicate_identity'] += 1
            outcome = 'uncertain'
        if phases is not None and outcome != _phase_outcome(events[node]):
            issues['phase_or_junit_incomplete'] += 1
            outcome = 'uncertain'
        outcomes[node] = (outcome, proof if outcome == 'failed' else None)
    return outcomes, dict(issues)


def _visible(node, include_ids):
    if include_ids or '[' not in node:
        return node
    address, _, _ = node.partition('[')
    return address + '[sha256:' + hashlib.sha256(node.encode('utf-8')).hexdigest() + ']'


def wheel_requirements(required):
    requested = list(required)
    targets = validate_selection(requested, allow_empty=True)
    if len(requested) != len(targets):
        raise ValueError('必验改善目标含重复测试 ID。')
    return targets


def compare_wheel_results(record, snapshot, documents, phases, *, policy, required=(), include_ids=False):
    selections = validate_selection(record['selected_tests'])
    targets = wheel_requirements(required)
    files = defaultdict(list)
    for file in snapshot.rglob('*.py'):
        name = file.relative_to(snapshot).as_posix()
        files[name.removesuffix('.py').replace('/', '.')].append(name)
    outcomes, issues = {}, {}
    for side in ('before', 'after'):
        outcomes[side], issues[side] = _observations(record['runs'][side], documents[side], phases[side], selections, files)

    labels = {('failed', 'passed'): 'improved', ('passed', 'passed'): 'regression_passed',
              ('passed', 'failed'): 'regression_detected', ('failed', 'failed'): 'baseline_failure'}
    left_wheel, right_wheel = (record['wheel_artifacts'][side] for side in ('before', 'after'))
    same_wheel = (left_wheel['sha256'] == right_wheel['sha256']
                  and Path(left_wheel['file']).name == Path(right_wheel['file']).name)
    uncertain = {'inconclusive', 'same_wheel_divergence'}
    entries = []
    verdicts = {}
    existing = Counter()
    nodes = outcomes['before'].keys() | outcomes['after'].keys()
    for node in sorted(nodes):
        left, right = (outcomes[side].get(node, ('unobserved', None)) for side in ('before', 'after'))
        before, after = left[0], right[0]
        verdict = labels.get((before, after), 'inconclusive')
        if same_wheel and verdict in ('improved', 'regression_detected'):
            verdict = 'same_wheel_divergence'
        verdicts[node] = verdict
        entry = {'test_id': _visible(node, include_ids), 'before': before, 'after': after, 'status': verdict}
        if verdict == 'baseline_failure':
            identity = ('unverified' if left[1] is None or right[1] is None else
                        'matched' if left[1] == right[1] else 'changed')
            entry['existing_failure'] = identity
            existing[identity] += 1
        entries.append(entry)
    counts = dict(Counter(verdicts.values()))
    required_rows = []
    for node in targets:
        outcome = verdicts.get(node, 'inconclusive')
        status = ('passed' if outcome == 'improved' else 'blocked' if outcome in uncertain else 'failed')
        required_rows.append({'test_id': _visible(node, include_ids), 'status': status, 'outcome': outcome})
    if counts.get('regression_detected') or any(row['status'] == 'failed' for row in required_rows):
        decision = 'failed'
    elif (any(issues.values()) or not nodes or uncertain.intersection(counts)
          or any(row['status'] == 'blocked' for row in required_rows)
          or existing['changed'] or existing['unverified']
          or (policy == 'regression' and counts.get('baseline_failure'))):
        decision = 'blocked'
    else:
        decision = 'passed'
    return {
        'counts': counts, 'entries': entries, 'issues': issues, 'existing_failures': dict(existing),
        'required_improvements': required_rows,
        'phase_basis': {side: 'explicit_pytest_phases' if phases[side] is not None else 'legacy_pytest_junit'
                        for side in ('before', 'after')},
        'gate': {'policy': policy, 'status': decision, 'exit_code': EXIT_CODES[decision]},
    }


def _markdown_identity(value):
    return (html.escape(value, quote=False).replace('|', '&#124;').replace('[', '&#91;').replace(']', '&#93;')
            .replace('`', '&#96;').replace('\n', ' ').replace('\r', ' '))


def _environment_description(environment):
    if environment is None:
        return 'unknown'
    return (f'{environment["implementation"]} {environment["python_version"]} · '
            f'{environment["platform"]}/{environment["machine"]}')


def render_wheel_error_markdown(*, exit_policy, cancelled):
    status = 'cancelled' if cancelled else 'blocked'
    return ('### ReproLab wheel check\n\n'
            f'- Exit policy · {exit_policy} · {status} · 2\n'
            '- Business gate · unavailable\n'
            '- Diagnostic details are on standard error.\n')


def render_wheel_markdown(result, *, exit_policy, exit_code):
    acceptance = result.get('review_plan_acceptance')
    sender_plan = result.get('functional_review_plan_sha256')
    execution = result.get('functional_plan_execution')
    if acceptance:
        title = 'ReproLab wheel release acceptance'
    elif sender_plan:
        title = 'ReproLab planned wheel functional check'
    else:
        title = 'ReproLab downstream wheel comparison'
    exit_status = 'ready' if result.get('preflight_only') else {0: 'passed', 1: 'failed', 2: 'blocked'}[exit_code]
    lines = [f'### {title}', '', f'- Exit policy · {exit_policy} · {exit_status} · {exit_code}']
    comparison = result.get('comparison')
    if comparison is not None:
        gate = comparison['gate']
        lines.append(f'- Business gate ({gate["policy"]}) · {gate["status"]} · {gate["exit_code"]}')
    else:
        lines.append('- Business gate · pending execution' if result.get('preflight_only')
                     else '- Business gate · unavailable')
    if sender_plan:
        lines.append(f'- Sender plan · `{sender_plan}`')
        if result.get('preflight_only'):
            return '\n'.join(lines) + '\n'
        if execution.get('receiver_install_check_required'):
            lines.append('- Required clean installation · supply the report at receiver verification')
    if acceptance:
        lines.append(f'- Receiver plan · `{acceptance["plan_sha256"]}`')
        if acceptance.get('scope') == 'matched':
            lines.append('- Receiver scope and local Git snapshot · matched')
    scope = acceptance or execution
    if scope:
        test = scope.get('test_environment', {})
        observed = test.get('observed', {})
        lines.append('- Functional test environment · before ' + _environment_description(observed.get('before'))
                     + ' · after ' + _environment_description(observed.get('after')))
        if test.get('required'):
            lines.append('- Required functional test environment · ' + _environment_description(test['required']))
        if test_install := scope.get('test_installation'):
            sources = test_install.get('observed', {})
            names = {'isolated_venv': 'isolated venv', 'existing_environment': 'existing environment'}
            lines.append('- Functional dependency source · before '
                         + names.get((sources.get('before') or {}).get('mode'), 'unknown') + ' · after '
                         + names.get((sources.get('after') or {}).get('mode'), 'unknown'))
            if fingerprint := test_install.get('required_wheelhouse_sha256'):
                lines.append(f'- Required functional wheelhouse inputs · `{fingerprint}`')
        if installation := scope.get('installation'):
            lines.append('- Clean installation · passed · ' + _environment_description(installation['environment']))
            if wheelhouse := installation.get('wheelhouse_sha256'):
                lines.append(f'- Approved clean-install wheelhouse inputs · `{wheelhouse}`')
        elif scope.get('required_install_environment'):
            lines.append('- Required clean installation environment · '
                         + _environment_description(scope['required_install_environment']))
        if 'mismatched_fields' in scope:
            lines.append(f'- Reason · `{scope["reason"]}`')
            lines.extend(f'- Scope check blocked · `{field}`' for field in scope['mismatched_fields'])
            return '\n'.join(lines) + '\n'
        lines.append('')
    else:
        lines += ['- Comparison uses inputs declared by the evidence.']

    if 'integrity' in result:
        lines.append(f'- Evidence integrity · {result["integrity"]}')
    lines += [
        f'- Legacy aggregate · {result["status"]}',
        '',
        '| Outcome | Cases |',
        '| --- | ---: |',
    ]
    lines.extend(f'| {label} | {comparison["counts"].get(key, 0)} |' for key, label in OUTCOMES.items())
    lines.append('')
    for side in ('before', 'after'):
        run = result['runs'][side]
        tests = run.get('tests') or {}
        basis = 'pytest phase records and JUnit' if comparison['phase_basis'][side] == 'explicit_pytest_phases' else 'legacy pytest JUnit'
        lines.append(f'- {side.title()} · {tests.get("passed", "unknown")} passed / '
                     f'{tests.get("tests", "unknown")} recorded · {basis}')
        for reason, count in comparison['issues'][side].items():
            lines.append(f'  - {reason.replace("_", " ")} · {count}')
    limit = 20
    goals = comparison['required_improvements']
    if goals:
        lines.extend(['', 'Required improvements', ''])
        priority = {'failed': 0, 'blocked': 1, 'passed': 2}
        for row in sorted(goals, key=lambda item: priority[item['status']])[:limit]:
            lines.append(f'- {_markdown_identity(row["test_id"])} · {row["status"]}')
        if len(goals) > limit:
            lines.append(f'- {len(goals) - limit} additional requirements in the JSON comparison.')

    entries = comparison['entries']
    priority = {'regression_detected': 0, 'same_wheel_divergence': 1, 'inconclusive': 1, 'improved': 2, 'baseline_failure': 3,
                'regression_passed': 4}
    detail = entries if len(entries) <= limit else [row for row in entries if row['status'] != 'regression_passed']
    visible = sorted(detail, key=lambda row: priority[row['status']])[:limit]
    if visible:
        lines.extend(['', '| Test identity | Before | After | Result |', '| --- | --- | --- | --- |'])
        for row in visible:
            cell = _markdown_identity(row['test_id'])
            outcome = OUTCOMES[row['status']]
            if failure := row.get('existing_failure'):
                outcome += f' ({failure} failure evidence)'
            lines.append(f'| {cell} | {row["before"]} | {row["after"]} | {outcome} |')
    if len(visible) < len(entries):
        lines.extend(['', f'Showing {len(visible)} of {len(entries)} individual results. '
                      'Use `--format json` for the full list.'])
    return '\n'.join(lines) + '\n'
