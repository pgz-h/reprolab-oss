"""Per-test wheel gates keep evidence consistency and execution decisions separate."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from reprolab.project_cli import main
from reprolab.project_wheels import _classification
from reprolab.runner import source_fingerprint, source_mode_fingerprint
from reprolab.wheel_results import PHASE_FILE, junit_totals


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _comparison(root, *, before, after, selectors, phases=None, install_failure=False, same_wheel=False):
    snapshot = root / 'downstream'
    (snapshot / 'tests').mkdir(parents=True)
    (snapshot / 'tests/test_case.py').write_text('# synthetic pytest JUnit evidence\n')
    runs, wheels = {}, {}
    for side, cases in (('before', before), ('after', after)):
        folder = root / side
        folder.mkdir()
        wheel = folder / 'fixture-0.1-py3-none-any.whl'
        wheel.write_bytes(b'both' if same_wheel else side.encode())
        wheels[side] = {'file': f'{side}/{wheel.name}', 'sha256': _digest(wheel)}
        install = folder / 'install.log'
        install.write_text('/private/install-root forbidden-info')
        files = [install]
        if side == 'before' and install_failure:
            row = {'install_status': 'completed', 'install_exit_code': 1, 'tests': None}
        else:
            log, xml = folder / 'pytest.log', folder / 'pytest.xml'
            log.write_text('/private/raw-log forbidden-info')
            document = ET.Element('testsuites')
            suite = ET.SubElement(document, 'testsuite', name='pytest', hostname='private-host',
                                  tests=str(len(cases)), failures=str(sum(tag == 'failure' for _, tag in cases)),
                                  errors=str(sum(tag == 'error' for _, tag in cases)),
                                  skipped=str(sum(tag == 'skipped' for _, tag in cases)))
            for name, flag in cases:
                node = ET.SubElement(suite, 'testcase', classname='tests.test_case', name=name)
                if flag:
                    ET.SubElement(node, flag, message='sensitive-exception').text = '/private/traceback'
            ET.ElementTree(document).write(xml, encoding='utf-8')
            files.extend((log, xml))
            code = int(any(tag in ('error', 'failure') for _, tag in cases))
            row = {'install_status': 'completed', 'install_exit_code': 0, 'status': 'completed',
                   'exit_code': code, 'tests': junit_totals(document)}
            if phases and side in phases:
                path = folder / PHASE_FILE
                path.write_text(json.dumps({'schema_version': 1, 'exit_code': code, **phases[side]}))
                files.append(path)
        row['evidence_sha256'] = {file.name: _digest(file) for file in files}
        runs[side] = row
    value = {'kind': 'reprolab_downstream_wheels', 'schema_version': 1, 'status': _classification(runs),
             'wheel_artifacts': wheels, 'downstream_commit': '0' * 40,
             'downstream': {'sha256': source_fingerprint(snapshot)[1], 'mode_sha256': source_mode_fingerprint(snapshot)},
             'module': 'fixture', 'selected_tests': selectors, 'runs': runs}
    (root / 'wheel-comparison.json').write_text(json.dumps(value))
    return value


@pytest.fixture
def release_scope(tmp_path):
    evidence = tmp_path / 'release evidence'
    selectors = ['tests/test_case.py::test_case[private-input]', 'tests/test_case.py::test_other']
    cases = [('test_case[private-input]', None), ('test_other', None)]
    record = _comparison(evidence, before=cases, after=cases, selectors=selectors)
    repo = tmp_path / 'receiver.git'
    snapshot = evidence / 'downstream'
    git = ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
           '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={os.devnull}']
    subprocess.run([*git, 'init', '--bare', '-q', str(repo)], check=True, capture_output=True)
    git.extend((f'--git-dir={repo}', f'--work-tree={snapshot}'))
    subprocess.run([*git, 'add', '.'], cwd=snapshot, check=True, capture_output=True)
    subprocess.run([*git, 'commit', '-qm', 'downstream'], cwd=snapshot, check=True, capture_output=True)
    record['downstream_commit'] = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
    manifest = evidence / 'wheel-comparison.json'
    manifest.write_text(json.dumps(record))
    plan = {'kind': 'reprolab_wheel_review_plan', 'schema_version': 1, 'gate': 'regression',
            **{key: record[key] for key in ('downstream_commit', 'module', 'selected_tests')},
            'wheel_artifacts': {side: {'filename': Path(wheel['file']).name, 'sha256': wheel['sha256']}
                                for side, wheel in record['wheel_artifacts'].items()}}
    plan_file = tmp_path / 'maintainer.json'
    plan_file.write_text(json.dumps(plan))
    options = ['--review-plan', str(plan_file), '--verify-git-repository', str(repo)]
    return evidence, manifest, record, plan_file, plan, options


def test_release_plan_accepts_zip_and_redacts_markdown(release_scope, tmp_path, capsys):
    evidence, _, _, plan_file, _, options = release_scope
    archive = tmp_path / 'received.zip'
    assert main(['pack-wheels', str(evidence), '--output', str(archive)]) == 0
    capsys.readouterr()
    assert main(['check-wheels', str(archive), *options]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['review_plan_acceptance'] == {
        'status': 'passed', 'exit_code': 0, 'policy': 'regression', 'scope': 'matched',
        'plan_schema_version': 1, 'plan_sha256': _digest(plan_file), 'git_verification': 'matched_local_snapshot',
    }
    assert main(['check-wheels', str(archive), *options, '--format', 'markdown']) == 0
    markdown = capsys.readouterr().out
    assert markdown.startswith('### ReproLab wheel release acceptance\n')
    assert 'Receiver scope and local Git snapshot · matched' in markdown
    for value in (str(tmp_path), 'private-input', 'forbidden-info', 'private-host'):
        assert value not in markdown
    for extra in (['--gate', 'regression'], ['--require-improvement', 'tests/test_case.py::test_other']):
        assert main(['check-wheels', str(archive), *options, *extra]) == 2
    assert main(['check-wheels', str(archive), *options[:2]]) == 2
    assert main(['check-wheels', str(archive), *options[2:]]) == 2
    nested_plan = evidence / 'plan.json'
    nested_plan.write_bytes(plan_file.read_bytes())
    assert main(['check-wheels', str(evidence), options[0], str(nested_plan), *options[2:]]) == 2


@pytest.mark.parametrize(('change', 'field'), [
    ('stale', 'wheel_artifacts.after.sha256'),
    ('renamed', 'wheel_artifacts.after.filename'),
    ('narrowed', 'selected_tests'),
    ('reordered', 'selected_tests'),
    ('commit', 'downstream_commit'),
    ('module', 'module'),
    ('content', 'downstream.sha256'),
    ('mode', 'downstream.mode_sha256'),
    ('excluded', 'downstream.entries'),
    ('damaged', 'wheel_artifacts.after.sha256'),
    ('empty_git', 'downstream_commit'),
])
def test_release_plan_blocks_scope_mismatch(release_scope, tmp_path, capsys, change, field):
    evidence, manifest, record, _, _, options = release_scope
    if change in ('stale', 'renamed', 'damaged'):
        item = record['wheel_artifacts']['after']
        wheel = evidence / item['file']
        if change == 'renamed':
            item['file'] = wheel.rename(wheel.with_name('other-0.1-py3-none-any.whl')).relative_to(evidence).as_posix()
        else:
            wheel.write_bytes(b'different wheel with the same filename')
            if change == 'stale':
                item['sha256'] = _digest(wheel)
    elif change == 'narrowed':
        record['selected_tests'].pop()
    elif change == 'reordered':
        record['selected_tests'].reverse()
    elif change == 'commit':
        record['downstream_commit'] = '1' * 40
    elif change == 'module':
        record['module'] = 'different_import'
    elif change in ('content', 'mode', 'excluded'):
        snapshot = evidence / 'downstream'
        target = snapshot / 'tests/test_case.py'
        if change == 'content':
            target.write_text('different downstream source\n')
        elif change == 'mode':
            target.chmod(0o755)
            if source_mode_fingerprint(snapshot) == record['downstream']['mode_sha256']:
                pytest.skip('filesystem cannot represent the executable bit')
        else:
            (snapshot / '.env-unexpected').write_text('excluded file')
        record['downstream'] = {'sha256': source_fingerprint(snapshot)[1],
                                'mode_sha256': source_mode_fingerprint(snapshot)}
    elif change == 'empty_git':
        missing = tmp_path / 'missing.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(missing)], check=True, capture_output=True)
        options[-1] = str(missing)
    manifest.write_text(json.dumps(record))
    assert main(['check-wheels', str(evidence), *options]) == 2
    result = json.loads(capsys.readouterr().out)['review_plan_acceptance']
    assert result['mismatched_fields'] == [field]
    assert result['exit_code'] == 2


def test_release_plan_applies_gate_and_required_improvements(release_scope, capsys):
    evidence, manifest, record, plan_file, plan, options = release_scope
    plan['gate'] = 'no-new-failures'
    plan['required_improvements'] = plan['selected_tests'][:1]
    plan_file.write_text(json.dumps(plan))
    assert main(['check-wheels', str(evidence), *options]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['review_plan_acceptance']['scope'] == 'matched'
    assert result['comparison']['gate'] == {'policy': 'no-new-failures', 'status': 'failed', 'exit_code': 1}
    assert result['comparison']['required_improvements'][0]['status'] == 'failed'

    xml = evidence / 'after/pytest.xml'
    tree = ET.parse(xml)
    tree.find('.//testsuite').set('failures', '1')
    ET.SubElement(tree.find('.//testcase[@name="test_other"]'), 'failure')
    tree.write(xml, encoding='utf-8')
    run = record['runs']['after']
    run.update(exit_code=1, tests=junit_totals(tree.getroot()))
    run['evidence_sha256'][xml.name] = _digest(xml)
    record['status'] = _classification(record['runs'])
    manifest.write_text(json.dumps(record))
    assert main(['check-wheels', str(evidence)]) == 0
    capsys.readouterr()
    assert main(['check-wheels', str(evidence), *options]) == 1
    assert json.loads(capsys.readouterr().out)['comparison']['counts']['regression_detected'] == 1


@pytest.mark.parametrize('change', ['duplicate_key', 'unknown_key', 'duplicate_selector', 'wrong_version', 'legacy_gate'])
def test_release_plan_rejects_invalid_json_contract(release_scope, capsys, change):
    evidence, _, _, plan_file, plan, options = release_scope
    if change == 'duplicate_key':
        plan_file.write_text('{"schema_version": 1,' + plan_file.read_text()[1:])
    else:
        if change == 'unknown_key':
            plan['wheel_artifacts']['after']['any_name'] = True
        elif change == 'duplicate_selector':
            plan['selected_tests'].append(plan['selected_tests'][0])
        elif change == 'wrong_version':
            plan['schema_version'] = True
        else:
            plan['gate'] = 'legacy'
        plan_file.write_text(json.dumps(plan))
    assert main(['check-wheels', str(evidence), *options]) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'


def test_release_plan_can_require_install_check_for_exact_wheel_and_environment(release_scope, tmp_path, capsys):
    evidence, manifest, record, plan_file, plan, options = release_scope
    wheel = evidence / 'after/wheel_fixture-2.0-py3-none-any.whl'
    _write_wheel(wheel, 2)
    artifact = {'filename': wheel.name, 'sha256': _digest(wheel)}
    record['wheel_artifacts']['after'] = {'file': 'after/' + wheel.name, 'sha256': artifact['sha256']}
    manifest.write_text(json.dumps(record))
    plan['wheel_artifacts']['after'] = artifact
    plan_file.write_text(json.dumps(plan))
    empty = tmp_path / 'empty-wheelhouse'
    empty.mkdir()
    install_output = tmp_path / 'installation'
    assert main(['verify-wheel-install', str(wheel), '--wheelhouse', str(empty), '--output', str(install_output),
                 '--confirm-run']) == 0
    report = json.loads(capsys.readouterr().out)
    receipt = install_output / 'wheel-install-check.json'
    verify = ['check-wheels', str(evidence), *options]
    include = ['--install-check', str(receipt)]
    assert main(verify) == 0
    assert 'installation' not in json.loads(capsys.readouterr().out)['review_plan_acceptance']
    assert main([*verify, *include]) == 2
    assert main(['check-wheels', str(evidence), *include]) == 2
    capsys.readouterr()

    fields = ('python_version', 'implementation', 'platform', 'machine')
    environment = {key: report['environment'][key] for key in fields}
    plan['required_install_environment'] = environment
    plan_file.write_text(json.dumps(plan))
    assert main([*verify, *include]) == 0
    acceptance = json.loads(capsys.readouterr().out)['review_plan_acceptance']
    assert acceptance['installation'] == {'status': 'matched_passed', 'report_sha256': _digest(receipt),
                                          'environment': environment}
    assert main([*verify, *include, '--format', 'markdown']) == 0
    markdown = capsys.readouterr().out
    assert 'Clean installation · passed' in markdown and str(receipt) not in markdown
    assert main(verify) == 2
    assert json.loads(capsys.readouterr().out)['review_plan_acceptance']['reason'] == 'install_check_missing'
    assert main([*verify, '--install-check', str(evidence / receipt.name)]) == 2
    assert json.loads(capsys.readouterr().out)['review_plan_acceptance']['reason'] == 'install_check_inside_evidence'

    edited = tmp_path / 'edited.json'
    for section, key, value, reason in [
        ('wheel', 'sha256', '1' * 64, 'install_check_artifact_mismatch'),
        ('wheel', 'filename', 'renamed-2.0-py3-none-any.whl', 'install_check_artifact_mismatch'),
        ('environment', 'python_version', '3.10.123', 'install_check_environment_mismatch'),
        ('environment', 'platform', 'unknown-os', 'install_check_environment_mismatch'),
        ('environment', 'machine', 'unknown-architecture', 'install_check_environment_mismatch'),
        ('environment', 'implementation', 'UnknownVM', 'install_check_environment_mismatch'),
        ('steps', 'pip-check', {'status': 'completed', 'exit_code': 1}, 'install_check_not_passed'),
        ('steps', 'install', {'status': 'completed', 'exit_code': False}, 'install_check_not_passed'),
    ]:
        edited.write_text(json.dumps({**report, section: {**report[section], key: value}}))
        assert main([*verify, '--install-check', str(edited)]) == 2
        acceptance = json.loads(capsys.readouterr().out)['review_plan_acceptance']
        assert acceptance['reason'] == reason
        if section != 'steps':
            assert acceptance['mismatched_fields'] == [f'install_check.{section}.{key}']
    edited.write_text(receipt.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
    assert main([*verify, '--install-check', str(edited)]) == 2
    assert json.loads(capsys.readouterr().out)['review_plan_acceptance']['reason'] == 'install_check_unavailable'


def test_release_plan_fingerprints_install_wheelhouse_inputs(release_scope, tmp_path, capsys):
    evidence, manifest, record, plan_file, plan, options = release_scope
    wheel = evidence / 'after/wheel_fixture-2.0-py3-none-any.whl'
    _write_wheel(wheel, 2)
    artifact = {'filename': wheel.name, 'sha256': _digest(wheel)}
    record['wheel_artifacts']['after'] = {'file': 'after/' + wheel.name, 'sha256': artifact['sha256']}
    manifest.write_text(json.dumps(record))
    plan['wheel_artifacts']['after'] = artifact
    house = tmp_path / 'trusted-wheelhouse'
    house.mkdir()
    for version in (4, 3):
        _write_wheel(house / f'wheel_fixture-{version}.0-py3-none-any.whl', version)
    assert main(['fingerprint-wheelhouse', str(house)]) == 0
    expected = json.loads(capsys.readouterr().out)['wheelhouse_sha256']
    plan['required_install_wheelhouse_sha256'] = expected
    plan_file.write_text(json.dumps(plan))
    verify = ['check-wheels', str(evidence), *options]
    assert main(verify) == 2
    assert 'required_install_environment' in capsys.readouterr().err

    report_dir = tmp_path / 'installation'
    assert main(['verify-wheel-install', str(wheel), '--wheelhouse', str(house), '--output', str(report_dir),
                 '--confirm-run']) == 0
    report = json.loads(capsys.readouterr().out)
    fields = ('python_version', 'implementation', 'platform', 'machine')
    plan['required_install_environment'] = {key: report['environment'][key] for key in fields}
    plan_file.write_text(json.dumps(plan))
    report_file = report_dir / 'wheel-install-check.json'
    assert main([*verify, '--install-check', str(report_file)]) == 0
    receipt = json.loads(capsys.readouterr().out)['review_plan_acceptance']['installation']
    assert receipt['wheelhouse_sha256'] == expected

    edited = tmp_path / 'reported-house.json'
    entries = report['wheelhouse']
    for wheelhouse, code, reason in [
        (entries[::-1], 0, None),
        ([], 2, 'install_check_wheelhouse_mismatch'),
        ([entries[0], entries[0]], 2, 'install_check_wheelhouse_invalid'),
        ([entries[0], {**entries[0], 'filename': entries[0]['filename'].capitalize()}],
         2, 'install_check_wheelhouse_invalid'),
        ([{**entries[0], 'sha256': 'x' * 64}], 2, 'install_check_wheelhouse_invalid'),
        ([{**entries[0], 'filename': 'subfolder/' + entries[0]['filename']}], 2, 'install_check_wheelhouse_invalid'),
    ]:
        edited.write_text(json.dumps({**report, 'wheelhouse': wheelhouse}))
        assert main([*verify, '--install-check', str(edited)]) == code
        acceptance = json.loads(capsys.readouterr().out)['review_plan_acceptance']
        if reason is None:
            assert acceptance['installation']['wheelhouse_sha256'] == expected
        else:
            assert acceptance['reason'] == reason
            assert acceptance['mismatched_fields'] == ['install_check.wheelhouse']


def test_release_plan_matches_both_hash_bound_test_environments(release_scope, capsys):
    evidence, manifest, record, plan_file, plan, options = release_scope
    environment = {'python_version': '3.11.18', 'implementation': 'CPython',
                   'platform': 'darwin', 'machine': 'arm64'}
    plan['required_test_environment'] = environment
    plan_file.write_text(json.dumps(plan))
    verify = ['check-wheels', str(evidence), *options]
    assert main(verify) == 2
    acceptance = json.loads(capsys.readouterr().out)['review_plan_acceptance']
    assert acceptance['reason'] == 'test_environment_unavailable'
    assert acceptance['mismatched_fields'] == ['test_environment.before', 'test_environment.after']

    def phase_record(side, facts):
        path = evidence / side / PHASE_FILE
        path.write_text(json.dumps(facts))
        record['runs'][side]['evidence_sha256'][PHASE_FILE] = _digest(path)
        manifest.write_text(json.dumps(record))
        return path

    selectors = record['selected_tests']
    facts = {'schema_version': 1, 'exit_code': 0, 'truncated': False, 'selected': selectors,
             'events': [[node, phase, 'passed', False]
                        for node in selectors for phase in ('setup', 'call', 'teardown')]}
    for side in ('before', 'after'):
        phase_record(side, facts)
    assert main(['check-wheels', str(evidence), '--gate', 'regression']) == 0
    capsys.readouterr()
    assert main(verify) == 2
    assert json.loads(capsys.readouterr().out)['review_plan_acceptance']['reason'] == 'test_environment_unavailable'

    phase_record('before', {**facts, 'environment': environment})
    other = {**environment, 'python_version': '3.15.2'}
    phase_record('after', {**facts, 'environment': other})
    assert main(verify) == 2
    acceptance = json.loads(capsys.readouterr().out)['review_plan_acceptance']
    assert acceptance['reason'] == 'test_environment_mismatch'
    assert acceptance['mismatched_fields'] == ['test_environment.after.python_version']
    path = phase_record('after', {**facts, 'environment': environment})
    assert main(verify) == 0
    assert json.loads(capsys.readouterr().out)['review_plan_acceptance']['test_environment'] == {
        'required': environment, 'observed': {'before': environment, 'after': environment}, 'status': 'matched',
    }
    path.write_text(json.dumps({**facts, 'environment': other}))
    assert main(verify) == 2
    assert '指纹不符' in capsys.readouterr().err


def test_same_aggregate_hides_improvement_and_regression(tmp_path, capsys):
    root = tmp_path / 'older directory'
    record = _comparison(root,
                         before=[('test_case[private-one]', 'failure'), ('test_case[private-two]', None)],
                         after=[('test_case[private-one]', None), ('test_case[private-two]', 'failure')],
                         selectors=['tests/test_case.py::test_case'])
    original = (root / 'wheel-comparison.json').read_bytes()
    assert main(['check-wheels', str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == {**record, 'integrity': 'consistent'}
    assert main(['check-wheels', str(root), '--gate', 'no-new-failures']) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result['status'] == 'blocked' and result['integrity'] == 'consistent'
    assert result['comparison']['counts'] == {'improved': 1, 'regression_detected': 1}
    assert len({row['test_id'] for row in result['comparison']['entries']}) == 2
    for secret in ('private-one', 'private-two', 'private-host', 'sensitive-exception', '/private/', 'forbidden-info'):
        assert secret not in output
    assert (root / 'wheel-comparison.json').read_bytes() == original
    assert main(['check-wheels', str(root), '--format', 'markdown', '--include-test-ids']) == 0
    assert 'test_case&#91;private-two&#93;' in capsys.readouterr().out


def test_wheel_markdown_separates_command_exit_from_comparison(tmp_path, capsys):
    evidence = tmp_path / 'functional'
    _comparison(evidence, before=[('test_case', None)], after=[('test_case', 'failure')],
                selectors=['tests/test_case.py::test_case'])
    archive = tmp_path / 'functional.zip'
    assert main(['pack-wheels', str(evidence), '--output', str(archive)]) == 0
    capsys.readouterr()
    command = ['check-wheels', str(archive), '--format', 'markdown']
    assert main(command) == 0
    assert 'Exit policy · Evidence integrity · passed · 0' in (markdown := capsys.readouterr().out)
    assert 'Business gate (legacy) · failed · 1' in markdown
    assert main([*command, '--gate', 'regression']) == 1
    assert 'Exit policy · Comparison gate · failed · 1' in (markdown := capsys.readouterr().out)
    assert 'Business gate (regression) · failed · 1' in markdown


@pytest.mark.parametrize('case', ['missing_zip', 'bad_zip', 'bad_plan', 'sender_plan'])
def test_wheel_markdown_always_has_safe_post_parse_failure(tmp_path, capsys, case):
    archive = tmp_path / 'private-received.zip'
    plan = tmp_path / 'private-maintainer.json'
    plan.write_text('private malformed plan')
    if case == 'sender_plan':
        args = ['verify-wheels', str(tmp_path), '--before-wheel', str(tmp_path / 'a.whl'),
                '--after-wheel', str(tmp_path / 'b.whl'), '--review-plan', str(plan),
                '--confirm-run', '--output', str(tmp_path / 'output')]
        policy = 'Sender plan functional gate'
    else:
        args = ['check-wheels', str(archive)]
        policy = 'Evidence integrity'
        if case == 'bad_zip':
            archive.write_text('private corrupt archive')
        elif case == 'bad_plan':
            args.extend(('--review-plan', str(plan), '--verify-git-repository', str(tmp_path)))
            policy = 'Receiver release plan'
    assert main([*args, '--format', 'markdown']) == 2
    streams = capsys.readouterr()
    assert f'Exit policy · {policy} · blocked · 2' in streams.out
    assert 'Business gate · unavailable' in streams.out
    assert str(tmp_path) not in streams.out and 'private' not in streams.out
    assert json.loads(streams.err)['status'] == 'error'


def test_same_wheel_divergence_blocks_case_gates_in_both_directions(tmp_path, capsys):
    root = tmp_path / 'evidence'
    record = _comparison(root, before=[('test_improvement', 'failure'), ('test_regression', None)],
                         after=[('test_improvement', None), ('test_regression', 'failure')],
                         selectors=['tests/test_case.py::test_improvement', 'tests/test_case.py::test_regression'],
                         same_wheel=True)
    archive = tmp_path / 'same-wheel.zip'
    assert main(['pack-wheels', str(root), '--output', str(archive)]) == 0
    capsys.readouterr()
    assert main(['check-wheels', str(archive)]) == 0
    assert json.loads(capsys.readouterr().out) == {**record, 'integrity': 'consistent'}
    for options in (['--gate', 'regression'], ['--gate', 'no-new-failures'],
                    ['--require-improvement', 'tests/test_case.py::test_improvement']):
        assert main(['check-wheels', str(archive), *options]) == 2
        result = json.loads(capsys.readouterr().out)['comparison']
        assert result['counts'] == {'same_wheel_divergence': 2}
        assert [(row['before'], row['after']) for row in result['entries']] == [('failed', 'passed'), ('passed', 'failed')]
    assert result['required_improvements'][0]['status'] == 'blocked'
    assert main(['check-wheels', str(archive), '--format', 'markdown']) == 0
    assert 'Pass/fail diverged on the same wheel' in capsys.readouterr().out



@pytest.mark.parametrize(('after_other', 'issue'), [
    ([('test_other', 'error')], None),
    ([('test_other', None), ('test_other', None)], 'duplicate_identity'),
    ([], 'selection_without_junit_case'),
    ([('test_other', None), ('test_extra', None)], 'outside_selection'),
])
def test_confirmed_regression_is_preserved_with_uncertain_evidence(tmp_path, capsys, after_other, issue):
    root = tmp_path / 'comparison'
    _comparison(root, before=[('test_changed', None), ('test_other', None)],
                after=[('test_changed', 'failure'), *after_other],
                selectors=['tests/test_case.py::test_changed', 'tests/test_case.py::test_other'])
    assert main(['check-wheels', str(root), '--gate', 'regression']) == 1
    comparison = json.loads(capsys.readouterr().out)['comparison']
    assert comparison['counts']['regression_detected'] == 1
    if issue:
        assert comparison['issues']['after'][issue] == 1
    else:
        assert comparison['counts']['inconclusive'] == 1


def test_installation_failure_and_missing_exact_parameter_block(tmp_path, capsys):
    root = tmp_path / 'comparison'
    _comparison(root, before=[], after=[('test_case[one]', None)],
                selectors=['tests/test_case.py::test_case'], install_failure=True)
    assert main(['check-wheels', str(root), '--gate', 'no-new-failures']) == 2
    comparison = json.loads(capsys.readouterr().out)['comparison']
    assert comparison['issues']['before'] == {'installation_incomplete': 1}
    assert comparison['counts'] == {'inconclusive': 1}

    other = tmp_path / 'two outcomes'
    _comparison(other, before=[('test_case[one]', 'failure')], after=[('test_case[one]', None)],
                selectors=['tests/test_case.py::test_case'])
    required = ['--require-improvement', 'tests/test_case.py::test_case[one]']
    assert main(['check-wheels', str(other), *required]) == 0
    assert json.loads(capsys.readouterr().out)['comparison']['required_improvements'][0]['status'] == 'passed'
    assert main(['check-wheels', str(other), '--require-improvement', 'tests/test_case.py::test_case']) == 2
    assert json.loads(capsys.readouterr().out)['comparison']['required_improvements'][0]['status'] == 'blocked'
    path = other / 'after/pytest.xml'
    path.write_bytes(path.read_bytes() + b' ')
    assert main(['check-wheels', str(other), '--gate', 'regression']) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'


def test_explicit_phase_evidence_prevents_forged_pass_transition(tmp_path, capsys):
    root = tmp_path / 'comparison'
    node = 'tests/test_case.py::test_case'
    _comparison(root, before=[('test_case', 'failure')], after=[('test_case', None)],
                selectors=[node],
                phases={'before': {'selected': [node], 'events': [[node, 'setup', 'failed', False]],
                                   'truncated': False}})
    assert main(['check-wheels', str(root), '--gate', 'regression']) == 2
    comparison = json.loads(capsys.readouterr().out)['comparison']
    assert comparison['counts'] == {'inconclusive': 1}
    assert comparison['issues']['before']['phase_or_junit_incomplete'] == 1


def _write_wheel(path, value):
    with ZipFile(path, 'w') as archive:
        archive.writestr('wheel_fixture.py', f'VALUE = {value}\n')
        metadata = path.name.split('-py3-')[0] + '.dist-info/'
        archive.writestr(metadata + 'WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr(metadata + 'METADATA', f'Metadata-Version: 2.1\nName: wheel-fixture\nVersion: {value}.0\n')
        archive.writestr(metadata + 'RECORD', '')


def test_verify_wheels_records_phases_and_zip_rechecks_them(tmp_path, capsys):
    repo = tmp_path / 'downstream project'
    repo.mkdir()
    (repo / 'test_wheel.py').write_text(
        'import pytest\nfrom wheel_fixture import VALUE\n'
        '@pytest.mark.parametrize("expected", [2], ids=["don\'t include $ parameter"])\n'
        'def test_corrected(expected): assert VALUE == expected\n'
        'def test_regressed(): assert VALUE == 1\n'
        '@pytest.fixture\ndef broken(): raise AssertionError("setup secret")\n'
        'def test_uncertain(broken): pass\n'
    )
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
           '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={os.devnull}']
    for args in (('init', '-q'), ('add', '.'), ('commit', '-qm', 'fixture')):
        subprocess.run([*git, *args], check=True, capture_output=True)
    wheels = [tmp_path / f'wheel_fixture-{value}.0-py3-none-any.whl' for value in (1, 2)]
    for wheel, value in zip(wheels, (1, 2)):
        _write_wheel(wheel, value)
    output = tmp_path / 'wheel evidence'
    args = ['verify-wheels', str(repo), '--before-wheel', str(wheels[0]), '--after-wheel', str(wheels[1]),
            '--module', 'wheel_fixture', '--output', str(output), '--python', sys.executable,
            '--gate', 'no-new-failures', '--confirm-run']
    for name in ('test_corrected', 'test_regressed', 'test_uncertain'):
        args.extend(('--test', 'test_wheel.py::' + name))
    assert main(args) == 1
    streams = capsys.readouterr()
    comparison = json.loads(streams.out)['comparison']
    assert comparison['counts'] == {'improved': 1, 'regression_detected': 1, 'inconclusive': 1}
    assert set(comparison['phase_basis'].values()) == {'explicit_pytest_phases'}
    record = json.loads((output / 'wheel-comparison.json').read_text())
    assert all(PHASE_FILE in row['evidence_sha256'] for row in record['runs'].values())
    environment = {'python_version': platform.python_version(), 'implementation': platform.python_implementation(),
                   'platform': sys.platform, 'machine': platform.machine()}
    for side in ('before', 'after'):
        phase_record = json.loads((output / side / PHASE_FILE).read_bytes())
        assert phase_record['environment'] == environment
        assert phase_record['pytest_version'] == pytest.__version__
    archive = tmp_path / 'received.zip'
    assert main(['pack-wheels', str(output), '--output', str(archive)]) == 0
    capsys.readouterr()
    assert main(['check-wheels', str(archive), '--gate', 'no-new-failures']) == 1
    assert json.loads(capsys.readouterr().out)['comparison'] == comparison
    plan = {'kind': 'reprolab_wheel_review_plan', 'schema_version': 1, 'gate': 'no-new-failures',
            'required_test_environment': environment,
            **{key: record[key] for key in ('downstream_commit', 'module', 'selected_tests')},
            'wheel_artifacts': {side: {'filename': Path(item['file']).name, 'sha256': item['sha256']}
                                for side, item in record['wheel_artifacts'].items()}}
    plan_file = tmp_path / 'test-environment-plan.json'
    plan_file.write_text(json.dumps(plan))
    assert main(['check-wheels', str(archive), '--review-plan', str(plan_file), '--verify-git-repository', str(repo)]) == 1
    accepted = json.loads(capsys.readouterr().out)['review_plan_acceptance']
    assert accepted['test_environment']['status'] == 'matched'


@pytest.mark.parametrize(('location', 'other_regression', 'code', 'finding'), [
    (4, False, 0, 'matched'),
    (5, False, 2, 'changed'),
    (None, False, 2, 'unverified'),
    (5, True, 1, 'changed'),
])
def test_double_red_requires_matched_failure_evidence(tmp_path, capsys, location, other_regression, code, finding):
    root = tmp_path / 'comparison'
    selectors = ['tests/test_case.py::test_red', 'tests/test_case.py::test_other']
    record = _comparison(root, before=[('test_red', 'failure'), ('test_other', None)],
                         after=[('test_red', 'failure'), ('test_other', 'failure' if other_regression else None)],
                         selectors=selectors)
    for side, line in (('before', 4), ('after', location)):
        xml = root / side / 'pytest.xml'
        tree = ET.parse(xml)
        if line:
            tree.find('.//failure').text = f'private traceback\ntests/test_case.py:{line}: AssertionError'
        tree.write(xml, encoding='utf-8')
        record['runs'][side]['evidence_sha256']['pytest.xml'] = _digest(xml)
    (root / 'wheel-comparison.json').write_text(json.dumps(record))
    assert main(['check-wheels', str(root), '--gate', 'no-new-failures']) == code
    output = capsys.readouterr().out
    comparison = json.loads(output)['comparison']
    assert comparison['counts']['baseline_failure'] == 1
    assert comparison['existing_failures'] == {finding: 1}
    assert 'private traceback' not in output



def test_legacy_identity_collision_and_missing_expected_phase_record_block(tmp_path, capsys):
    root = tmp_path / 'comparison'
    node = 'tests/test_case.py::test_case'
    record = _comparison(root, before=[('test_case', None)], after=[('test_case', None)], selectors=[node])
    snapshot = root / 'downstream'
    duplicate = snapshot / 'tests.test_case.py'
    duplicate.write_text('# same dotted JUnit classname\n')
    record['downstream'] = {'sha256': source_fingerprint(snapshot)[1], 'mode_sha256': source_mode_fingerprint(snapshot)}
    record['runs']['after']['phase_record_expected'] = True
    (root / 'wheel-comparison.json').write_text(json.dumps(record))
    assert main(['check-wheels', str(root), '--gate', 'regression']) == 2
    comparison = json.loads(capsys.readouterr().out)['comparison']
    assert comparison['issues']['before']['unmapped_junit_case'] == 1
    assert comparison['issues']['after'] == {'phase_record_incomplete': 1}
