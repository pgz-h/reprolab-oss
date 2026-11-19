import json
import re
import shutil
import subprocess

import pytest

from reprolab.native_cmake import read_cmake
from reprolab.project_cli import main


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


def test_native_ctest_public_entry_and_portable_evidence(tmp_path, capsys):
    for tool in ('cmake', 'ctest'):
        exe = shutil.which(tool)
        version = subprocess.check_output([exe, '--version'], text=True) if exe else ''
        match = re.search(r'version (\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (3, 29):
            pytest.skip('Requires CMake and CTest 3.29+')
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
