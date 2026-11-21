import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

from reprolab.native_cmake import _settings_digest, check_failure_markers, read_cmake
from reprolab.native_project import _compare, _source_record
from reprolab.project_cli import main


def test_native_markdown_reports_early_verification_failure(tmp_path, capsys):
    evidence = tmp_path / 'broken.zip'
    evidence.write_bytes(b'invalid ZIP')
    assert main(['check-native', str(evidence), '--format', 'markdown']) == 2
    streams = capsys.readouterr()
    assert 'Review · Blocked (exit 2)' in streams.out
    assert 'Evidence integrity · Unverified' in streams.out
    assert str(tmp_path) not in streams.out
    assert json.loads(streams.err)['status'] == 'error'


def _saved_side(folder):
    names = ('suite.green', 'suite.fixed[1]', 'disabled', 'skip', 'timeout', 'missing')
    stages = {stage: {'status': 'completed', 'exit_code': 0} for stage in
              ('cmake', 'ctest', 'configure', 'build', 'discover', 'run')}
    stages['run']['exit_code'] = 8
    (folder / 'commands.json').write_text(json.dumps(stages))
    for log in ('configure.log', 'build.log', 'ctest.log'):
        (folder / log).write_text('fixture phase completed')
    for tool in ('cmake', 'ctest'):
        (folder / (tool + '-version.log')).write_text(tool + ' version 3.29.6\n')
    (folder / 'discovery.json').write_text(json.dumps({'kind': 'ctestInfo', 'version': {'major': 1, 'minor': 0},
                                                        'tests': [{'name': name} for name in names]}))
    rows = [('run', ''), ('fail', '<failure message="Failed"/>'), ('disabled', ''),
            ('notrun', '<skipped message="SKIP_RETURN_CODE=42"/>'), ('fail', '<failure message="Timeout"/>'),
            ('notrun', '<skipped message="Unable to find executable"/>')]
    xml = '<testsuite tests="6" failures="2" disabled="1" skipped="2">' + ''.join(
        f'<testcase name="{name}" status="{status}">{issue}</testcase>'
        for name, (status, issue) in zip(names, rows)) + '</testsuite>'
    (folder / 'results.xml').write_text(xml)
    return xml


def test_ctest_records_skips_infrastructure_and_regular_failures_separately(tmp_path):
    _saved_side(tmp_path)
    record = read_cmake(tmp_path, selected=None)
    assert record['status'] == 'blocked'
    assert record['tests'] == {'suite.green': 'passed', 'suite.fixed[1]': 'failed', 'disabled': 'skipped',
                               'skip': 'skipped', 'timeout': 'blocked', 'missing': 'blocked'}


@pytest.mark.parametrize(('old', 'new'), [('tests="6"', 'tests="5"'), ('failures="2"', 'failures="1"'),
                                         ('name="missing"', 'name="disabled"')])
def test_inconsistent_ctest_xml_cannot_supply_a_pass(tmp_path, old, new):
    xml = _saved_side(tmp_path)
    (tmp_path / 'results.xml').write_text(xml.replace(old, new))
    assert read_cmake(tmp_path, selected=None)['tests'] == {}


@pytest.fixture
def cmake_available():
    for tool in ('cmake', 'ctest'):
        exe = shutil.which(tool)
        version = subprocess.check_output([exe, '--version'], text=True) if exe else ''
        match = re.search(r'version (\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (3, 29):
            pytest.skip('Requires CMake and CTest 3.29+')


def test_native_ctest_public_entry_and_portable_evidence(tmp_path, capsys, cmake_available):
    repo = tmp_path / 'native'
    repo.mkdir()
    git = ['git', '-C', str(repo), '-c', 'user.name=Reviewer', '-c', 'user.email=review@example.invalid',
           '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'init', '-q'], check=True)
    (repo / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.29)\nproject(check LANGUAGES NONE)\n'
        'enable_testing()\n'
        'add_test(NAME "receipt[1]" COMMAND "${CMAKE_COMMAND}" -P "${CMAKE_CURRENT_SOURCE_DIR}/probe.cmake")\n'
        'add_test(NAME other COMMAND "${CMAKE_COMMAND}" -E true)\n')
    (repo / 'probe.cmake').write_text('include("${CMAKE_CURRENT_LIST_DIR}/behavior.cmake")\n'
                                     'if(NOT RECEIPT)\nmessage(FATAL_ERROR "wrong receipt")\nendif()\n')
    for value in ('false', 'true'):
        (repo / 'behavior.cmake').write_text(f'set(RECEIPT {value})\n')
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-qm', value], check=True)
    evidence = tmp_path / 'native receipt'
    def cli(args, code):
        assert main(list(map(str, args))) == code
        streams = capsys.readouterr()
        return json.loads(streams.out or streams.err)
    opts = ['native', repo, '--runner', 'cmake', '--before', 'HEAD~1', '--after', 'HEAD',
            '--shared-test-source', 'CMakeLists.txt', '--shared-test-source', 'probe.cmake', '--test', 'receipt[1]', '--confirm-run']
    result = cli([*opts, '--output', evidence], 0)
    assert result['comparison']['counts'] == {'improved': 1}
    assert result['runs']['after']['tests'] == {'receipt[1]': 'passed'}
    archive = tmp_path / 'ctest.zip'
    cli(['pack-native', evidence, '--output', archive], 0)
    evidence.rename(tmp_path / 'sender moved')
    checked = cli(['check-native', archive], 0)
    assert checked['comparison'] == result['comparison'] and checked['integrity'] == 'consistent'
    missing = cli([*opts, '--test', 'not-discovered', '--output', tmp_path / 'missing'], 2)
    assert missing['runs']['after']['reason'] == 'selected_scope_mismatch'
    assert not (tmp_path / 'missing' / 'after' / 'results.xml').exists()

    # Only an included setting changes. Same CTest ID and byte-identical
    # declared file, but a different executable argument is not a verified fix.
    (repo / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.29)\nproject(check LANGUAGES NONE)\n'
        'enable_testing()\ninclude(command.cmake)\n'
        'add_test(NAME same COMMAND "${CMAKE_COMMAND}" -E ${ACTION})\n')
    for action in ('false', 'true'):
        (repo / 'command.cmake').write_text(f'set(ACTION {action})\n')
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-qm', action], check=True)
    shifted = cli(['native', repo, '--runner', 'cmake', '--before', 'HEAD~1', '--after', 'HEAD',
                   '--shared-test-source', 'CMakeLists.txt', '--test', 'same', '--confirm-run',
                   '--output', tmp_path / 'shifted'], 2)
    row, = shifted['comparison']['entries']
    assert (row['before'], row['after'], row['test_configuration'], row['shared']) == ('failed', 'passed', 'changed', False)


def test_native_git_scope_and_fix_acceptance(tmp_path, capsys, cmake_available):
    repo = tmp_path / 'fixture'
    repo.mkdir()
    git = ['git', '-C', str(repo), '-c', 'user.name=Reviewer',
           '-c', 'user.email=review@example.invalid', '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'init', '-q'], check=True)

    def commit(message):
        subprocess.run([*git, 'add', '-f', '.'], check=True)
        subprocess.run([*git, 'commit', '-qm', message], check=True)
        return subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()

    def cli(args, code):
        assert main(list(map(str, args))) == code
        streams = capsys.readouterr()
        return json.loads(streams.out or streams.err)

    def markdown(location, code, *options):
        assert main(list(map(str, ['check-native', location, '--format', 'markdown', *options]))) == code
        summary = capsys.readouterr().out
        assert 'Evidence integrity · Consistent' in summary
        assert str(tmp_path) not in summary and 'expectation.txt' not in summary and '| receipt |' not in summary
        return summary

    def compare(name, before, after, code, *options):
        return cli(['native', repo, '--runner', 'cmake', '--before', before, '--after', after,
                    '--test', 'receipt', '--shared-test-source', 'CMakeLists.txt',
                    '--shared-test-source', 'probe.cmake', '--confirm-run',
                    '--output', tmp_path / name, *options], code)

    (repo / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.29)\nproject(probe LANGUAGES NONE)\nenable_testing()\n'
        'add_test(NAME receipt COMMAND "${CMAKE_COMMAND}" '
        '"-DINPUT=${CMAKE_CURRENT_SOURCE_DIR}/expectation.txt" '
        '-P "${CMAKE_CURRENT_SOURCE_DIR}/probe.cmake" "--input=${CMAKE_CURRENT_SOURCE_DIR}/actual.txt")\n'
    )
    (repo / 'probe.cmake').write_text(
        'file(READ "${INPUT}" expected)\n'
        'string(REGEX REPLACE "^--input=" "" actual_file "${CMAKE_ARGV4}")\n'
        'file(READ "${actual_file}" actual)\n'
        'if(NOT actual STREQUAL expected)\nmessage(FATAL_ERROR "mismatch")\nendif()\n'
    )
    (repo / 'actual.txt').write_text('2')
    (repo / 'expectation.txt').write_text('1')
    ignored = repo / 'nested' / 'build'
    ignored.mkdir(parents=True)
    (ignored / 'rules.cmake').write_text('# before\n')
    base = commit('baseline')
    (repo / 'expectation.txt').write_text('2')
    shifted = commit('change expectation')
    opts = ['--require-fix', 'receipt']
    observed = compare('observation', base, shifted, 0)
    assert observed['comparison']['counts'] == {'improved': 1}
    assert observed['comparison']['test_input_coverage'] == 'unknown'
    strict = compare('unknown', base, shifted, 2, *opts)
    assert strict['fix_acceptance']['reason'] == 'test_inputs_unknown'

    attested = ['--attest-test-inputs', '--shared-test-input', 'expectation.txt', *opts]
    strict = compare('changed', base, shifted, 2, *attested)
    assert strict['comparison']['counts'] == {'improved': 1}
    assert strict['fix_acceptance']['reason'] == 'test_inputs_changed_or_missing'
    missing = compare('missing-input', base, shifted, 2, *attested, '--shared-test-input', 'absent')
    assert missing['shared_test_inputs'][1]['before'] is None
    assert missing['fix_acceptance']['reason'] == 'test_inputs_changed_or_missing'

    (ignored / 'rules.cmake').write_text('# after\n')
    excluded = commit('excluded only')
    blocked = compare('excluded', shifted, excluded, 2)
    assert blocked['snapshot_scope']['excluded_changes_only']
    assert blocked['snapshot_scope']['excluded_tracked_changes'] == 1
    assert blocked['comparison']['counts'] == {'regression_passed': 1}
    archive = tmp_path / 'excluded.zip'
    cli(['pack-native', tmp_path / 'excluded', '--output', archive], 0)
    assert cli(['check-native', archive], 0)['comparison']['gate']['exit_code'] == 2
    blocked_gate = cli(['check-native', archive, '--verify-result'], 2)['verified_result']
    assert blocked_gate == {'rules': 'current_schema_2', 'status': 'blocked', 'exit_code': 2}
    assert 'Ordinary comparison · Blocked (exit 2)' in markdown(archive, 2, '--verify-result')
    assert 'Exit policy · Evidence integrity only (exit 0)' in markdown(archive, 0)
    assert cli(['check-native', archive, *opts], 2)['fix_acceptance']['reason'] == 'excluded_tracked_changes'
    control = compare('same-commit', excluded, excluded, 0)
    assert control['snapshot_scope']['snapshots_match']
    assert not control['snapshot_scope']['excluded_changes_only']

    (repo / 'actual.txt').write_text('1')
    (repo / 'expectation.txt').write_text('1')
    fixed = commit('use correct implementation and restore expectation')
    partial = compare('fixed', base, fixed, 2, *attested)
    assert partial['comparison']['gate']['exit_code'] == 0
    assert partial['comparison']['counts'] == {'improved': 1}
    assert partial['fix_acceptance']['reason'] == 'excluded_tracked_changes'
    (ignored / 'rules.cmake').write_text('# before\n')
    included = commit('restore excluded file')
    accepted = compare('included', base, included, 0, *attested)
    assert accepted['fix_acceptance']['status'] == 'passed'
    archive = tmp_path / 'accepted.zip'
    cli(['pack-native', tmp_path / 'included', '--output', archive], 0)
    assert cli(['check-native', archive, *opts], 0)['fix_acceptance'] == accepted['fix_acceptance']

    assert cli(['check-native', archive, '--verify-result'], 0)['verified_result']['status'] == 'passed'

    def plan_file(name, candidate, *, selected=None):
        plan = tmp_path / name
        plan.write_text(json.dumps({
            'schema_version': 1,
            'runner': 'cmake',
            'commits': {'before': base, 'after': candidate},
            'selected_tests': selected or ['receipt'],
            'required_fixes': ['receipt'],
            'shared_test_sources': ['probe.cmake', 'CMakeLists.txt'],
            'shared_test_inputs': ['expectation.txt'],
        }))
        return plan

    receiver = plan_file('maintainer-plan.json', included)
    result = cli(['check-native', archive, '--review-plan', receiver], 0)['review_plan_acceptance']
    assert result['targets'] == {'receipt': 'improved'}
    assert result['plan_sha256'] == hashlib.sha256(receiver.read_bytes()).hexdigest()
    summary = markdown(archive, 0, '--review-plan', receiver)
    assert 'Receiver plan acceptance · Passed (exit 0)' in summary
    assert result['plan_sha256'] in summary and hashlib.sha256(b'receipt').hexdigest()[:16] in summary

    trusted_git = tmp_path / 'trusted.git'
    subprocess.run(['git', 'clone', '--quiet', '--bare', '--no-local', str(repo), str(trusted_git)], check=True)
    git_args = ('--verify-git-repository', trusted_git)
    cli(['check-native', archive, *git_args], 2)
    verified = cli(['check-native', archive, '--review-plan', receiver, *git_args], 0)['git_verification']
    assert verified['snapshots'] == {side: {'content': 'matched', 'mode': 'matched'} for side in ('before', 'after')}
    assert verified['excluded_tracked_changes'] == {'status': 'matched', 'recorded': 0, 'observed': 0}
    assert 'Receiver-supplied local Git objects · Matched' in markdown(archive, 0, '--review-plan', receiver, *git_args)

    forged = tmp_path / 'different-snapshot'
    shutil.copytree(tmp_path / 'included', forged)
    record_path = forged / 'native-comparison.json'
    forged_record = json.loads(record_path.read_text())
    actual_file = forged / 'inputs/after/actual.txt'
    actual_file.write_text('99')
    forged_record['source_snapshots']['after'] = _source_record(forged / 'inputs/after')
    record_path.write_text(json.dumps(forged_record))
    forged_archive = tmp_path / 'different-snapshot.zip'
    cli(['pack-native', forged, '--output', forged_archive], 0)
    cli(['check-native', forged_archive, '--review-plan', receiver], 0)
    mismatch = cli(['check-native', forged_archive, '--review-plan', receiver, *git_args], 2)
    assert mismatch['review_plan_acceptance']['reason'] == 'git_snapshot_mismatch'
    assert mismatch['git_verification']['snapshots']['after'] == {'content': 'mismatch', 'mode': 'matched'}

    if os.name != 'nt':
        actual_file.write_text('1')
        actual_file.chmod(0o755)
        forged_record['source_snapshots']['after'] = _source_record(forged / 'inputs/after')
        record_path.write_text(json.dumps(forged_record))
        mismatch = cli(['check-native', forged, '--review-plan', receiver, *git_args], 2)
        assert mismatch['git_verification']['snapshots']['after'] == {'content': 'matched', 'mode': 'mismatch'}

    omitted_record = tmp_path / 'fixed' / 'native-comparison.json'
    false_scope = json.loads(omitted_record.read_text())
    false_scope['snapshot_scope']['excluded_tracked_changes'] = 0
    omitted_record.write_text(json.dumps(false_scope))
    omission_plan = plan_file('unreported-exclusion-plan.json', fixed)
    cli(['check-native', tmp_path / 'fixed', '--review-plan', omission_plan], 0)
    mismatch = cli(['check-native', tmp_path / 'fixed', '--review-plan', omission_plan, *git_args], 2)
    assert mismatch['review_plan_acceptance']['reason'] == 'git_omitted_changes_mismatch'
    assert mismatch['git_verification']['excluded_tracked_changes'] == {'status': 'mismatch', 'recorded': 0, 'observed': 1}

    # Keep the commit and tree objects, but make one included blob available only from a promisor remote.
    partial_repo = tmp_path / 'partial-git'
    shutil.copytree(repo / '.git', partial_repo / '.git')
    blob = subprocess.check_output([*git, 'rev-parse', f'{included}:actual.txt'], text=True).strip()
    (partial_repo / '.git' / 'objects' / blob[:2] / blob[2:]).unlink()
    partial_git = ['git', '-C', str(partial_repo)]
    subprocess.run([*partial_git, 'remote', 'add', 'origin', trusted_git.as_uri()], check=True)
    subprocess.run([*partial_git, 'config', 'remote.origin.promisor', 'true'], check=True)
    subprocess.run([*partial_git, 'config', 'extensions.partialClone', 'origin'], check=True)
    object_count = ['git', '-C', str(partial_repo), 'count-objects', '-v']
    before_count = subprocess.check_output(object_count)
    unavailable = cli(['check-native', archive, '--review-plan', receiver,
                       '--verify-git-repository', partial_repo], 2)
    assert unavailable['review_plan_acceptance']['reason'] == 'git_objects_unavailable'
    assert subprocess.check_output(object_count) == before_count

    embedded = tmp_path / 'included' / 'plan.json'
    shutil.copyfile(receiver, embedded)
    cli(['check-native', tmp_path / 'included', '--review-plan', embedded], 2)
    cli(['check-native', archive, '--review-plan', receiver, '--verify-result'], 2)

    changed_plan = plan_file('expectation-change-plan.json', shifted)
    wrong_candidate = cli(['check-native', archive, '--review-plan', changed_plan], 2)
    assert wrong_candidate['review_plan_acceptance']['mismatched_fields'] == ['commits']
    wrong_tests = plan_file('wrong-tests.json', included, selected=['receipt', 'other'])
    mismatch = cli(['check-native', archive, '--review-plan', wrong_tests], 2)
    assert mismatch['review_plan_acceptance']['mismatched_fields'] == ['selected_tests']

    sender_only = compare('sender-only', base, shifted, 0, '--attest-test-inputs', *opts)
    assert sender_only['shared_test_inputs'] == []
    sender_archive = tmp_path / 'sender-only.zip'
    cli(['pack-native', tmp_path / 'sender-only', '--output', sender_archive], 0)
    omitted_input = cli(['check-native', sender_archive, '--review-plan', changed_plan], 2)
    assert omitted_input['review_plan_acceptance']['mismatched_fields'] == ['shared_test_inputs']
    summary = markdown(sender_archive, 2, '--review-plan', changed_plan)
    assert 'Evidence scope differs from the receiver plan' in summary and 'test support inputs' in summary
    changed_input = cli(['check-native', tmp_path / 'changed', '--review-plan', changed_plan], 2)
    assert changed_input['review_plan_acceptance']['unmatched_test_files'] == ['expectation.txt']

    # Re-project the same raw CTest evidence with the published schema 1 rules.
    legacy_dir = tmp_path / 'legacy'
    shutil.copytree(tmp_path / 'included', legacy_dir)
    legacy_file = legacy_dir / 'native-comparison.json'
    legacy = json.loads(legacy_file.read_text())
    legacy.update(schema_version=1, runs={
        side: read_cmake(legacy_dir / side, selected=['receipt'], schema_version=1)
        for side in ('before', 'after')
    })
    for key in ('snapshot_scope', 'shared_test_inputs', 'test_inputs_attested'):
        del legacy[key]
    legacy['comparison'] = _compare(legacy['runs'], legacy['shared_test_files'], ['receipt'], runner='cmake')
    legacy_file.write_text(json.dumps(legacy))
    legacy_archive = tmp_path / 'legacy.zip'
    cli(['pack-native', legacy_dir, '--output', legacy_archive], 0)
    checked = cli(['check-native', legacy_archive], 0)
    assert checked['comparison']['entries'][0]['test_configuration'] == 'changed'
    assert checked['comparison']['gate']['exit_code'] == 2
    assert checked['snapshot_scope']['excluded_tracked_changes'] is None
    assert cli(['check-native', legacy_archive, *opts], 2)['fix_acceptance']['reason'] == 'historical_scope_unknown'
    legacy_gate = cli(['check-native', legacy_archive, '--verify-result'], 2)['verified_result']
    assert legacy_gate == {'rules': 'historical_schema_1', 'status': 'blocked', 'exit_code': 2}
    legacy_plan = cli(['check-native', legacy_archive, '--review-plan', receiver], 2)
    assert legacy_plan['review_plan_acceptance']['reason'] == 'historical_scope_unknown'
    assert 'Evidence rules · Historical schema 1' in markdown(legacy_archive, 2, '--review-plan', receiver)
    historical = cli(['check-native', legacy_archive, '--review-plan', receiver, *git_args], 2)
    assert historical['git_verification']['status'] == 'partial'
    marker_plan = tmp_path / 'legacy-marker-plan.json'
    marker_plan.write_text(json.dumps({**json.loads(receiver.read_bytes()), 'schema_version': 2,
                                      'baseline_failure_markers': {'receipt': 'mismatch'}}))
    historical_marker = cli(['check-native', legacy_archive, '--review-plan', marker_plan], 2)
    assert historical_marker['review_plan_acceptance']['reason'] == 'historical_scope_unknown'
    assert historical['git_verification']['excluded_tracked_changes'] == {
        'status': 'unrecorded', 'recorded': None, 'observed': 0,
    }

    state = (tmp_path / 'cold-start').as_posix()
    with (repo / 'probe.cmake').open('a') as script:
        script.write(f'if(NOT EXISTS "{state}")\nfile(WRITE "{state}" "warm")\n'
                     'message(FATAL_ERROR "cold start")\nendif()\n')
    cold = commit('repeat identical snapshot with outside state')
    divergent = compare('divergence', cold, cold, 2)
    assert divergent['comparison']['counts'] == {'same_snapshot_divergence': 1}
    assert [divergent['runs'][side]['tests']['receipt'] for side in ('before', 'after')] == ['failed', 'passed']
    (repo / 'probe.cmake').write_bytes(subprocess.check_output([*git, 'show', f'{included}:probe.cmake']))
    (repo / 'actual.txt').write_text('2')
    (ignored / 'rules.cmake').write_text('# mixed\n')
    regressed = commit('regression alongside excluded change')
    mixed = compare('mixed', included, regressed, 1, *attested)
    assert mixed['snapshot_scope']['excluded_tracked_changes'] == 1
    assert not mixed['snapshot_scope']['excluded_changes_only']
    assert mixed['fix_acceptance']['reason'] == 'regression_detected'
    cli(['pack-native', tmp_path / 'mixed', '--output', tmp_path / 'regressed.zip'], 0)
    assert cli(['check-native', tmp_path / 'regressed.zip', '--verify-result'], 1)['verified_result']['status'] == 'failed'
    assert 'Confirmed new failure' in markdown(tmp_path / 'regressed.zip', 1, '--verify-result')
    regression_plan = tmp_path / 'regression-marker-plan.json'
    regression_plan.write_text(json.dumps({**json.loads(marker_plan.read_bytes()),
                                           'commits': {'before': included, 'after': regressed}}))
    regression = cli(['check-native', tmp_path / 'regressed.zip', '--review-plan', regression_plan], 1)
    assert regression['review_plan_acceptance']['reason'] == 'regression_detected'

    if os.name != 'nt':
        subprocess.run([*git, 'update-index', '--chmod=+x', 'actual.txt'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'included mode only'], check=True)
        mode = compare('mode', regressed, 'HEAD', 2)
        assert mode['source_snapshots']['before']['sha256'] == mode['source_snapshots']['after']['sha256']
        assert not mode['snapshot_scope']['snapshots_match']
        assert not mode['snapshot_scope']['excluded_changes_only']


def test_ctest_path_option_normalization_keeps_boundaries_and_legacy():
    def digest(root, value, *, schema=2):
        row = {'command': ['cmake', value]}
        return _settings_digest(row, {'source': [root]}, schema_version=schema)

    for flag in ('--input=', '-DINPUT=', '-DINPUT:FILEPATH='):
        assert digest('/tmp/a', flag + '/tmp/a/case') == digest('/tmp/b', flag + '/tmp/b/case')
        assert digest('/tmp/a', flag + '/tmp/a/case', schema=1) != digest('/tmp/b', flag + '/tmp/b/case', schema=1)
        for changed in ('/tmp/b/other', '/tmp/b-sibling/case', 'literal/tmp/b/case'):
            assert digest('/tmp/a', flag + '/tmp/a/case') != digest('/tmp/b', flag + changed)
    assert digest('/tmp/a', '--input=/tmp/a/../case') != digest('/tmp/b', '--input=/tmp/b/../case')
    assert digest('/tmp/a', 'literal=/tmp/a/case') != digest('/tmp/b', 'literal=/tmp/b/case')


def test_plan_native_pr_reads_target_policy_and_checks_received_evidence(
    tmp_path, capsys, cmake_available, monkeypatch,
):
    repo = tmp_path / 'pr'
    repo.mkdir()
    git = ['git', '-C', str(repo), '-c', 'user.name=Reviewer',
           '-c', 'user.email=review@example.invalid', '-c', 'commit.gpgsign=false']

    def git_output(*args):
        return subprocess.check_output([*git, *args], text=True).strip()

    def commit(message):
        git_output('add', '.')
        git_output('commit', '-qm', message)
        return git_output('rev-parse', 'HEAD')

    def cli(*args, code=0):
        assert main(list(map(str, args))) == code
        captured = capsys.readouterr()
        return json.loads(captured.out or captured.err)

    git_output('init', '-q')
    (repo / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.29)\nproject(receipt LANGUAGES NONE)\nenable_testing()\n'
        'add_test(NAME receipt COMMAND "${CMAKE_COMMAND}" -P "${CMAKE_CURRENT_SOURCE_DIR}/probe.cmake")\n')
    (repo / 'probe.cmake').write_text(
        'file(READ "${CMAKE_CURRENT_LIST_DIR}/implementation" actual)\n'
        'file(READ "${CMAKE_CURRENT_LIST_DIR}/expectation" expected)\n'
        'if(NOT actual STREQUAL expected)\nmessage(FATAL_ERROR "receipt price differs")\nendif()\n')
    (repo / 'implementation').write_text('2')
    (repo / 'expectation').write_text('1')
    common = commit('common')
    path = repo / '.github' / 'native-policy.json'
    path.parent.mkdir()
    policy = {'schema_version': 2, 'runner': 'cmake', 'selected_tests': ['receipt'],
              'baseline_failure_markers': {'receipt': 'receipt price differs'},
              'required_fixes': ['receipt'], 'shared_test_sources': ['probe.cmake', 'CMakeLists.txt'],
              'shared_test_inputs': ['expectation']}
    policy_raw = json.dumps(policy).encode()
    path.write_bytes(policy_raw)
    base = commit('add reviewed policy')
    git_output('checkout', '-q', '--detach', common)
    path.parent.mkdir(exist_ok=True)
    path.write_text('{"schema_version":999}')
    (repo / 'implementation').write_text('1')
    head = commit('fix implementation and change candidate policy')
    path.write_text('uncommitted policy')
    merge = git_output('commit-tree', f'{head}^{{tree}}', '-p', base, '-p', head, '-m', 'merge')
    reverse = git_output('commit-tree', f'{head}^{{tree}}', '-p', head, '-p', base, '-m', 'wrong order')
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'pull_request': {'base': {'sha': base}, 'head': {'sha': head},
                                                   'merge_commit_sha': merge}}))
    opts = ('plan-native-pr', repo, '--github-event', event, '--policy-path', '.github/native-policy.json')
    plan_path = tmp_path / 'head-plan.json'
    receipt = cli(*opts, '--pr-mode', 'head', '--output', plan_path)
    head_context = {'mode': 'head', 'base': base, 'head': head}
    assert json.loads(plan_path.read_bytes()) == {
        **policy, 'commits': {'before': common, 'after': head}, 'pr_context': head_context,
    }
    assert receipt['plan_sha256'] == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert receipt['policy']['sha256'] == hashlib.sha256(policy_raw).hexdigest()
    assert receipt['pr_context']['event_sha256'] == hashlib.sha256(event.read_bytes()).hexdigest()
    cli(*opts, '--pr-mode', 'head', '--output', plan_path, code=2)
    merge_plan = tmp_path / 'merge-plan.json'
    cli(*opts, '--pr-mode', 'merge', '--output', merge_plan)
    assert json.loads(merge_plan.read_bytes())['commits'] == {'before': base, 'after': merge}
    failed = tmp_path / 'no-plan.json'
    cli(*opts, '--pr-mode', 'merge', '--merge-commit', reverse, '--output', failed, code=2)
    cli(*opts, '--pr-mode', 'head', '--policy-path', '.github/missing.json', '--output', failed, code=2)
    assert not failed.exists()
    evidence = tmp_path / 'evidence'
    raw_plan = plan_path.read_bytes()
    from reprolab import native_cmake
    original_run = native_cmake.run_cmake

    def changing_plan(*args, **kwargs):
        plan_path.write_bytes(raw_plan + b'\n')
        return original_run(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(native_cmake, 'run_cmake', changing_plan)
        sender = cli('native', repo, '--review-plan', plan_path, '--attest-test-inputs',
                     '--confirm-run', '--output', evidence)
    assert sender['directory'] == str(evidence)
    assert sender['review_plan_sha256'] == receipt['plan_sha256']
    archive = tmp_path / 'pr.zip'
    cli('pack-native', evidence, '--output', archive)
    changed = cli('check-native', archive, '--review-plan', plan_path, code=2)
    assert changed['review_plan_acceptance']['reason'] == 'review_plan_digest_mismatch'
    plan_path.write_bytes(raw_plan)
    result = cli('check-native', archive, '--review-plan', plan_path, '--verify-git-repository', repo)
    assert result['review_plan_acceptance'] == sender['review_plan_acceptance']
    assert result['review_plan_acceptance']['baseline_marker_checks'] == {'receipt': 'matched'}
    cli('check-native', archive, '--review-plan', merge_plan, code=2)

    # Equivalent event deliveries share a plan. A target-only advance can keep the
    # comparison pair and policy unchanged, but it must invalidate the PR gate.
    current_event = tmp_path / 'current-event.json'
    pull = json.loads(event.read_bytes())['pull_request']
    current_event.write_text(json.dumps({'action': 'synchronize', 'pull_request': pull}, indent=2))
    verify = ('--github-event', current_event, '--pr-mode', 'head', '--policy-path', '.github/native-policy.json',
              '--verify-git-repository', repo)
    current = cli('check-native', archive, *verify)
    assert current['review_plan_acceptance'] == sender['review_plan_acceptance']
    assert current['git_verification']['status'] == 'matched'
    advanced = git_output('commit-tree', f'{base}^{{tree}}', '-p', base, '-m', 'advance target')
    pull['base']['sha'] = advanced
    current_event.write_text(json.dumps({'pull_request': pull}))
    stale = cli('check-native', archive, *verify, code=2)
    assert stale['review_plan_acceptance']['reason'] == 'review_plan_digest_mismatch'
    assert stale['review_plan_acceptance']['pr_context'] == {**head_context, 'base': advanced}
    assert stale['commits'] == current['commits']
    assert stale['git_verification']['status'] == 'matched'
    assert stale['comparison']['gate']['exit_code'] == 0
    assert main(list(map(str, ['check-native', archive, *verify, '--format', 'markdown']))) == 2
    summary = capsys.readouterr().out
    assert 'Receiver plan acceptance · Blocked (exit 2)' in summary
    assert f'PR target · `{advanced}`' in summary
    cli('check-native', archive, '--github-event', current_event, '--pr-mode', 'head',
        '--policy-path', '.github/native-policy.json', code=2)
    cli('check-native', archive, '--review-plan', plan_path, '--pr-mode', 'head', code=2)
    cli('check-native', archive, *verify, '--review-plan', plan_path, code=2)
    cli('check-native', archive, '--verify-result')

    unbound = tmp_path / 'unbound'
    cli('native', repo, '--runner', 'cmake', '--before', common, '--after', head, '--test', 'receipt',
        '--shared-test-source', 'probe.cmake', '--shared-test-source', 'CMakeLists.txt',
        '--shared-test-input', 'expectation', '--attest-test-inputs', '--confirm-run', '--output', unbound)
    blocked = cli('check-native', unbound, *verify, code=2)
    assert blocked['review_plan_acceptance']['reason'] == 'pr_review_plan_unrecorded'
    generic = tmp_path / 'generic-plan.json'
    generic.write_text(json.dumps({**policy, 'commits': {'before': common, 'after': head}}))
    cli('check-native', unbound, '--review-plan', generic)

    pull['base']['sha'] = '0' * len(advanced)
    current_event.write_text(json.dumps({'pull_request': pull}))
    assert main(list(map(str, ['check-native', archive, *verify, '--format', 'markdown']))) == 2
    streams = capsys.readouterr()
    assert 'Local Git lacks the target or PR head commit' in streams.out
    assert 'Evidence integrity · Unverified' in streams.out
    assert json.loads(streams.err)['reason'] == 'pr_parent_missing'

    git_output('checkout', '-q', '--detach', '-f', common)
    (repo / 'CMakeLists.txt').write_text((repo / 'CMakeLists.txt').read_text() +
        'add_custom_target(noise ALL COMMAND "${CMAKE_COMMAND}" -E echo "receipt price differs")\n'
        'add_test(NAME other COMMAND "${CMAKE_COMMAND}" -E echo "receipt price differs")\n')
    (repo / 'probe.cmake').write_text((repo / 'probe.cmake').read_text().replace('receipt price differs', 'fixture unavailable'))
    alternate_before = commit('another baseline failure with marker in other output')
    (repo / 'implementation').write_text('1')
    alternate_after = commit('fix alternate outcome')
    alternate = tmp_path / 'alternate'
    cli('native', repo, '--runner', 'cmake', '--before', alternate_before, '--after', alternate_after,
        '--test', 'receipt', '--test', 'other', '--shared-test-source', 'probe.cmake',
        '--shared-test-source', 'CMakeLists.txt', '--shared-test-input', 'expectation', '--attest-test-inputs',
        '--confirm-run', '--output', alternate)
    assert 'receipt price differs' in (alternate / 'before' / 'build.log').read_text()
    junit = (alternate / 'before' / 'results.xml').read_text()
    assert 'receipt price differs' in junit and 'fixture unavailable' in junit
    alternate_zip = tmp_path / 'alternate.zip'
    cli('pack-native', alternate, '--output', alternate_zip)
    alternate_plan = {**policy, 'selected_tests': ['receipt', 'other'],
                      'commits': {'before': alternate_before, 'after': alternate_after}}
    marked = tmp_path / 'alternate-marker-plan.json'
    marked.write_text(json.dumps(alternate_plan))
    old = tmp_path / 'alternate-unmarked-plan.json'
    old.write_text(json.dumps({k: v for k, v in {**alternate_plan, 'schema_version': 1}.items()
                               if k != 'baseline_failure_markers'}))
    cli('check-native', alternate_zip, '--review-plan', old)
    checked = cli('check-native', alternate_zip, '--review-plan', marked, code=2)
    assert checked['comparison']['counts']['improved'] == 1
    assert checked['review_plan_acceptance']['reason'] == 'baseline_failure_marker_unverified'
    assert checked['review_plan_acceptance']['baseline_marker_checks'] == {'receipt': 'mismatch'}
    assert main(['check-native', str(alternate_zip), '--review-plan', str(marked), '--format', 'markdown']) == 2
    summary = capsys.readouterr().out
    assert '0/1 matched' in summary
    assert all(value not in summary for value in ('receipt price differs', 'fixture unavailable', str(tmp_path)))

    blocked_out = tmp_path / 'planned-blocked'
    sent_blocked = cli('native', repo, '--review-plan', marked, '--attest-test-inputs', '--confirm-run',
                       '--output', blocked_out, code=2)
    assert sent_blocked['review_plan_acceptance'] == checked['review_plan_acceptance']
    blocked_zip = tmp_path / 'planned-blocked.zip'
    cli('pack-native', blocked_out, '--output', blocked_zip)
    received = cli('check-native', blocked_zip, '--review-plan', marked, code=2)
    assert received['review_plan_acceptance'] == sent_blocked['review_plan_acceptance']
    unattested = cli('native', repo, '--review-plan', old, '--confirm-run',
                     '--output', tmp_path / 'planned-unattested', code=2)
    assert unattested['review_plan_acceptance']['reason'] == 'test_inputs_unknown'


def test_ctest_marker_requires_case_output_and_verified_failure(tmp_path):
    xml = tmp_path / 'results.xml'
    raw = (b'<testsuite><testcase name="empty"><failure message="Failed"/></testcase>'
           b'<testcase name="green"><system-err>marker</system-err></testcase>'
           b'<testcase name="failed"><system-err>marker</system-err></testcase></testsuite>')
    xml.write_bytes(raw)
    tests = {'empty': 'failed', 'green': 'passed', 'failed': 'failed'}
    markers = dict.fromkeys(tests, 'marker')
    assert check_failure_markers(xml, hashlib.sha256(raw).hexdigest(), tests, markers) == {
        'empty': 'missing', 'green': 'missing', 'failed': 'matched',
    }


@pytest.mark.parametrize('options', [
    [], ['--confirm-run', '--runner', 'cmake'], ['--confirm-run', '--before', 'HEAD'],
    ['--confirm-run', '--after', 'HEAD'], ['--confirm-run', '--test', 'receipt'],
    ['--confirm-run', '--shared-test-source', 'probe.cmake'],
    ['--confirm-run', '--shared-test-input', 'expectation'], ['--confirm-run', '--require-fix', 'receipt'],
])
def test_native_plan_rejects_preview_and_manual_scope(tmp_path, capsys, options):
    output = tmp_path / 'output'
    args = ['native', str(tmp_path), '--review-plan', str(tmp_path / 'not-read.json'),
            '--output', str(output), *options]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)['error']['type'] == 'argument_error'
    assert not output.exists()
