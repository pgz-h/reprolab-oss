"""Verify Go worktree recheck against real frozen, portable evidence."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from reprolab import go_project
from reprolab.project_history import read_report_snapshot, report_reference
from reprolab.project_sessions import check_project_report, replay_project_report
from reprolab.runner import RunCancelled
from reprolab.workspace import WORKTREE


_TESTS = '''package sample
import "testing"
func TestFixed(t *testing.T) { if Value() != 2 { t.Fail() } }
func TestPersistent(t *testing.T) { t.Fail() }
func TestRegression(t *testing.T) { if Value() != 1 { t.Fail() } }
func TestGreen(t *testing.T) { if Value() < 1 { t.Fail() } }
func TestSkipped(t *testing.T) { t.Skip("fixture") }
func TestRemoved(t *testing.T) { if Value() < 1 { t.Fail() } }
'''
_SELECTED = ['TestFixed', 'TestPersistent', 'TestRegression', 'TestGreen', 'TestSkipped', 'TestRemoved']


def _git(repository, *args):
    subprocess.run(['git', '-C', str(repository), '-c', 'core.hooksPath=' + os.devnull,
                    '-c', 'commit.gpgsign=false', '-c', 'user.name=ReproLab test',
                    '-c', 'user.email=test@example.invalid', *args], check=True, capture_output=True)


@pytest.fixture
def source_and_worktree(tmp_path):
    go = shutil.which('go')
    if go is None:
        pytest.skip('Go toolchain is not installed')
    source = tmp_path / 'original'
    source.mkdir()
    _git(source, 'init', '-q')
    (source / 'go.mod').write_text('module example.invalid/sample\n\ngo 1.20\n', encoding='utf-8')
    value = source / 'value.go'
    value.write_text('package sample\nfunc Value() int { return 0 }\n', encoding='utf-8')
    value.chmod(0o755)
    (source / 'value_test.go').write_text(_TESTS, encoding='utf-8')
    _git(source, 'add', '.')
    _git(source, 'commit', '-qm', 'baseline')
    value.write_text('package sample\nfunc Value() int { return 1 }\n', encoding='utf-8')
    report = go_project.run_go_project(source, 'HEAD', WORKTREE, tmp_path / 'source-reports',
                                      package='.', selected=_SELECTED, go=go)
    assert check_project_report(report['report_dir'])['status'] == 'consistent'
    source = read_report_snapshot(report['report_dir'])

    # A separate checkout without any commits forces recheck to use evidence for its baseline.
    worktree = tmp_path / 'local checkout'
    worktree.mkdir()
    _git(worktree, 'init', '-q')
    for name in ('go.mod', 'value_test.go'):
        shutil.copyfile(Path(report['report_dir']) / 'evidence/project/inputs/after' / name, worktree / name)
    (worktree / 'value.go').write_text('package sample\nfunc Value() int { return 2 }\n', encoding='utf-8')
    return source, worktree, go


def test_go_recheck_compares_actual_candidates_and_preserves_baseline(source_and_worktree, tmp_path):
    source, worktree, go = source_and_worktree
    folder = Path(source['report_dir'])
    original = (folder / 'report.json').read_bytes()
    reference = report_reference(source)
    output = tmp_path / 'reports'

    result = go_project.recheck_go_project(folder, output, expected_report=reference, repository=worktree,
                                          go=go, confirmed=True)
    report, comparison = result['report'], result['comparison']
    assert comparison['changes'] == []
    assert {entry['test_id']: entry['status'] for entry in comparison['entries']} == {
        'TestFixed': 'resolved', 'TestPersistent': 'still_failed', 'TestRegression': 'new_failure',
        'TestGreen': 'still_passed', 'TestSkipped': 'not_reviewed', 'TestRemoved': 'still_passed',
    }
    assert report['before_commit'] == source['before_commit']
    assert report['after_commit'] == WORKTREE
    assert report['repository'] == str(worktree)
    assert report['run_id'] != source['run_id']
    assert report['selected_tests'] == _SELECTED
    for key in ('source_sha256', 'source_mode_sha256'):
        assert report[key]['before'] == source[key]['before']
    assert report['review_source'] == {**reference, 'selection_mode': 'all', 'selected_tests': _SELECTED}
    assert (folder / 'report.json').read_bytes() == original
    assert check_project_report(report['report_dir'])['status'] == 'consistent'
    assert check_project_report(folder)['status'] == 'consistent'
    assert reference['run_id'] in (Path(report['report_dir']) / 'report.html').read_text(encoding='utf-8')
    replayed = replay_project_report(report['report_dir'], output, go=go, confirmed=True)
    assert replayed['review_source'] == report['review_source']
    assert check_project_report(replayed['report_dir'])['status'] == 'consistent'

    changed = {**report, 'timeout': 45, 'runtime': {**report['runtime'], 'go_version': 'different'},
               'environment': {**report['environment'], 'machine': 'other'}}
    comparison = go_project.compare_go_recheck(source, changed)
    assert set(comparison['changes']) == {'每版限时已变化', '记录的 Go 工具链已变化', '平台或架构已变化'}
    assert comparison['counts'] == {'conditions_changed': 5, 'not_reviewed': 1}
    incomplete = {**report, 'after': {**report['after'], 'status': 'timed_out'}}
    assert go_project.compare_go_recheck(source, incomplete)['counts'] == {'not_reviewed': len(_SELECTED)}


def test_go_recheck_keeps_missing_tests_and_flags_changed_shared_files(source_and_worktree, tmp_path):
    source, worktree, go = source_and_worktree
    tests = worktree / 'value_test.go'
    tests.write_text(_TESTS[:_TESTS.index('func TestRemoved')], encoding='utf-8')
    output = tmp_path / 'reports'
    arguments = {'expected_report': report_reference(source), 'repository': worktree, 'go': go, 'confirmed': True}
    result = go_project.recheck_go_project(source['report_dir'], output, **arguments)
    assert result['report']['selected_tests'] == _SELECTED
    assert result['comparison']['changes'] == ['共享测试文件内容或清单已变化']
    entries = {entry['test_id']: entry for entry in result['comparison']['entries']}
    assert entries['TestFixed'] == {'test_id': 'TestFixed', 'status': 'conditions_changed',
                                    'previous': 'failed', 'current': 'passed'}
    assert entries['TestRemoved'] == {'test_id': 'TestRemoved', 'status': 'not_reviewed',
                                      'previous': 'passed', 'current': 'missing'}
    assert check_project_report(result['report']['report_dir'])['status'] == 'consistent'

    tests.unlink()
    with pytest.raises(ValueError, match='恢复该包的测试文件'):
        go_project.recheck_go_project(source['report_dir'], output, **arguments)
    assert list(output.iterdir()) == [Path(result['report']['report_dir'])]
    assert check_project_report(source['report_dir'])['status'] == 'consistent'


def test_go_recheck_preview_source_identity_and_cancel(source_and_worktree, tmp_path, monkeypatch):
    source, worktree, go = source_and_worktree
    folder = Path(source['report_dir'])
    path = folder / 'report.json'
    original = path.read_bytes()
    output = tmp_path / 'reports'
    arguments = {'expected_report': report_reference(source), 'repository': worktree, 'go': go}
    with monkeypatch.context() as patch:
        def unexpected(*args, **kwargs):
            pytest.fail('preview must not resolve a worktree or invoke Go')

        patch.setattr(go_project, 'repository_root', unexpected)
        patch.setattr(go_project, '_runtime', unexpected)
        preview = go_project.recheck_go_project(folder, output, **arguments)
    assert preview['repository'] == source['repository']
    assert preview['selected_tests'] == _SELECTED
    assert not output.exists()

    path.write_bytes(original + b'\n')
    with pytest.raises(ValueError, match='来源身份不符'):
        go_project.recheck_go_project(folder, output, confirmed=True, **arguments)
    assert not output.exists()
    path.write_bytes(original)

    def cancel(*args):
        control = args[-1]
        control.cancel()
        control.check()

    monkeypatch.setattr(go_project, '_run_version', cancel)
    with pytest.raises(RunCancelled):
        go_project.recheck_go_project(folder, output, confirmed=True, **arguments)
    assert not list(output.iterdir())
    assert path.read_bytes() == original
    assert check_project_report(folder)['status'] == 'consistent'
