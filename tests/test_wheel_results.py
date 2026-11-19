"""Per-test wheel gates keep evidence consistency and execution decisions separate."""
import hashlib
import json
import os
from pathlib import Path
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


def _comparison(root, *, before, after, selectors, phases=None, install_failure=False):
    snapshot = root / 'downstream'
    (snapshot / 'tests').mkdir(parents=True)
    (snapshot / 'tests/test_case.py').write_text('# synthetic pytest JUnit evidence\n')
    runs, wheels = {}, {}
    for side, cases in (('before', before), ('after', after)):
        folder = root / side
        folder.mkdir()
        wheel = folder / 'fixture-0.1-py3-none-any.whl'
        wheel.write_bytes(side.encode())
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
    archive = tmp_path / 'received.zip'
    assert main(['pack-wheels', str(output), '--output', str(archive)]) == 0
    capsys.readouterr()
    assert main(['check-wheels', str(archive), '--gate', 'no-new-failures']) == 1
    assert json.loads(capsys.readouterr().out)['comparison'] == comparison


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
