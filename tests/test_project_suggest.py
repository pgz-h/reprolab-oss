"""Verify the snapshot, collection review, and explicit run through normal CLI entry points."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from reprolab.project_cli import main
from reprolab.project_selections import read_selection


@pytest.fixture
def project(tmp_path):
    repository = tmp_path / 'my project'
    repository.mkdir()
    marker = tmp_path / 'test body executed'
    (repository / 'logic.py').write_text('VALUE = 1\n', encoding='utf-8')
    (repository / 'test_logic.py').write_text(
        'from pathlib import Path\nfrom logic import VALUE\n'
        f'def test_existing(): Path({str(marker)!r}).touch(); assert VALUE > 0\n'
        'def test_changed(): assert VALUE > 0\n', encoding='utf-8')
    (repository / 'test_other.py').write_text('def test_other(): pass\n', encoding='utf-8')
    git = ['git', '-C', str(repository), '-c', 'user.name=Suggest fixture',
           '-c', 'user.email=suggest@example.invalid', '-c', 'commit.gpgsign=false',
           '-c', f'core.hooksPath={os.devnull}']
    for args in (('init', '-q'), ('add', '.'), ('commit', '-qm', 'baseline')):
        subprocess.run([*git, *args], check=True, capture_output=True)
    return repository, marker


def suggest(repository, output, capsys, *, code=0):
    options = ['suggest', str(repository), '--before', 'HEAD', '--working-tree',
               '--python', sys.executable, '--output', str(output)]
    assert main([*options, '--format', 'json']) == code
    response = capsys.readouterr()
    assert not response.err
    return json.loads(response.out), options


def test_suggest_previews_changed_definitions_then_runs_reviewed_order_from_same_session(project, tmp_path, capsys):
    repository, marker = project
    (repository / 'logic.py').write_text('VALUE = 2\n', encoding='utf-8')
    tests = repository / 'test_logic.py'
    tests.write_text(tests.read_text().replace('def test_changed(): assert VALUE > 0',
                                              'def test_changed(): assert VALUE == 2'), encoding='utf-8')
    output = tmp_path / 'review output'
    result, options = suggest(repository, output, capsys)
    assert not marker.exists()
    assert result['status'] == 'ready' and result['discovered_count'] == 3
    expected = ['test_logic.py::test_existing', 'test_logic.py::test_changed']
    assert read_selection(result['selection_file']) == expected
    assert [row['test_id'] for row in result['recommendations']] == expected[::-1]
    assert result['recommendations'][0]['definition']['status'] == 'modified'
    assert 'test_logic.py:' in result['recommendations'][0]['definition_reason']
    assert Path(result['discovered_file']).read_text().splitlines() == [*expected, 'test_other.py::test_other']
    assert json.loads(Path(result['suggestion_file']).read_text()) == result
    assert hashlib.sha256(Path(result['collection_record']).read_bytes()).hexdigest() == result['collection_sha256']

    assert main(options) == 0
    text = capsys.readouterr().out
    assert '修改测试定义' in text and '确认或编辑清单后' in text
    assert main([*options, '--language', 'en']) == 0
    english = capsys.readouterr().out
    assert 'Modified test definition (test_logic.py:' in english
    assert 'This test file changed' in english
    assert 'Review or edit the list, then run the same frozen session' in english
    assert english.index(expected[1]) < english.index(expected[0])
    assert not marker.exists()
    assert main(['run', result['directory'], '--output', str(output / 'reports')]) == 2
    assert '正式运行须明确选择' in json.loads(capsys.readouterr().err)['error']['message']
    assert not marker.exists()

    (repository / 'logic.py').write_text('VALUE = -1\n', encoding='utf-8')
    run = shlex.split(result['run_command']) if sys.platform != 'win32' else [
        'reprolab', 'project', 'run', result['directory'], '--selection-file', result['selection_file'],
        '--output', str(output / 'reports')]
    assert run[:2] == ['reprolab', 'project']
    runs = []
    for _ in range(2):
        assert main(run[2:]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report['selected_tests'] == expected and report['project_id'] == result['project_id']
        assert report['source_sha256'] == result['source_sha256']
        assert report['counts'] == {'regression_passed': 1, 'improved': 1}
        runs.append(report['run_id'])
    assert marker.exists() and len(set(runs)) == 2
    assert len(list((Path(result['directory']) / 'collections').iterdir())) == 1


def test_suggest_empty_recommendations_leave_manual_selection(project, tmp_path, capsys):
    repository, marker = project
    (repository / 'README.md').write_text('Documentation only.\n', encoding='utf-8')
    result, _ = suggest(repository, tmp_path / 'out', capsys)
    assert result['status'] == 'no_recommendations'
    assert result['selection_file'] is result['run_command'] is None
    assert result['discovered_count'] == 3 and not marker.exists()
    arguments = ['run', result['directory'], '--output', str(tmp_path / 'reports')]
    assert main(arguments) == 2
    assert '正式运行须明确选择' in json.loads(capsys.readouterr().err)['error']['message']
    assert main([*arguments, '--test', 'test_logic.py::test_existing']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['selected_tests'] == ['test_logic.py::test_existing'] and marker.exists()


def test_suggest_partial_discovery_keeps_available_tests_without_ready_selection(project, tmp_path, capsys):
    repository, marker = project
    (repository / 'logic.py').write_text('VALUE = 2\n', encoding='utf-8')
    (repository / 'test_missing.py').write_text('import suggest_missing_dependency\n', encoding='utf-8')
    result, _ = suggest(repository, tmp_path / 'out', capsys, code=2)
    assert result['status'] == 'incomplete' and result['collection']['partial']
    assert result['selection_file'] is result['run_command'] is None
    assert result['recommended_count'] == 2 and not marker.exists()
    assert Path(result['collection_log']).is_file() and Path(result['discovered_file']).is_file()
    arguments = ['run', result['directory'], '--output', str(tmp_path / 'reports')]
    assert main(arguments) == 2
    capsys.readouterr()
    assert not marker.exists()
    assert main([*arguments, '--test', 'test_logic.py::test_existing']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['selected_tests'] == ['test_logic.py::test_existing'] and report['collection']['partial']


@pytest.mark.parametrize('truncated', [False, True])
def test_suggest_blocked_or_truncated_never_writes_ready_selection(project, tmp_path, capsys, truncated):
    repository, marker = project
    if truncated:
        source = ('import pytest\n'
                  '@pytest.mark.parametrize("value", range(10001))\n'
                  'def test_many(value): raise AssertionError("not collected-only")\n')
    else:
        source = 'import suggest_missing_dependency\n'
    (repository / 'test_scope.py').write_text(source, encoding='utf-8')
    assert main(['suggest', str(repository), '--before', 'HEAD', '--working-tree',
                 '--path', 'test_scope.py', '--output', str(tmp_path / 'reports'), '--format', 'json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == ('incomplete' if truncated else 'blocked')
    assert result['collection']['truncated'] is truncated
    assert result['collection']['collection_path'] == 'test_scope.py'
    assert result['selection_file'] is result['run_command'] is None
    assert result['discovered_count'] == (10000 if truncated else 0)
    assert not marker.exists()


@pytest.mark.parametrize(('reason', 'expected'), [
    ('直接静态导入关联到改动模块 · app.core', 'Direct static import of a changed module · app.core'),
    ('间接静态导入推断 · tests/my test.py:9 → service.py:20（基线快照）',
     'Indirect static import chain to a changed module · tests/my test.py:9 → service.py:20 (baseline snapshot)'),
    ('收集绑定的 fixture 影响推断 · cart（my tests/conftest.py:10） → app.py',
     'Collected fixture binding leads to a changed module · cart（my tests/conftest.py:10） → app.py'),
])
def test_english_suggestion_keeps_recorded_dependency_paths(reason, expected):
    from reprolab.project_suggest import _english_reason

    assert _english_reason(reason) == expected
