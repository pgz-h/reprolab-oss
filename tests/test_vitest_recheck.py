"""Exercise frozen Vitest worktrees and checked candidate evidence without npm."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

import pytest

from reprolab import vitest_project as vitest
from reprolab.acceptance_plan import AcceptancePlan, baseline_pin, save_acceptance_plan
from reprolab.persistence import save_json
from reprolab.project_cli import main
from reprolab.project_history import read_report_snapshot, report_reference
from reprolab.project_sessions import _manifest, check_project_report, replay_project_report
from reprolab.runner import RunCancelled
from reprolab.workspace import WORKTREE


_FILES = ['order totals.test.ts', 'other.test.ts']
_TOOLS = {'node': '/local/node', 'node_version': 'v26.13.0', 'npm': '/local/npm', 'npm_version': '11.20.0'}


def _git(repository, *args):
    subprocess.run(['git', '-C', str(repository), '-c', 'core.hooksPath=' + os.devnull,
                    '-c', 'commit.gpgsign=false', '-c', 'user.name=ReproLab test',
                    '-c', 'user.email=test@example.invalid', *args], check=True, capture_output=True)


def _case(name, status, *, retried=False):
    return {'name': name, 'status': status, 'expected_failure': False, 'errors': [],
            'retry_count': int(retried), 'flaky': retried}


def _version_run(folder, side, evidence, selected, tools, expected_version, timeout, offline, npm_cache, control,
                 *, project_dir, locked):
    control.check()
    assert locked['version'] == expected_version
    root = evidence / 'inputs' / side / project_dir
    fixed = (root / 'value.txt').read_text() == 'fixed'
    cases = [_case('suite > price', 'passed' if fixed else 'failed'),
             _case('suite > discount', 'failed' if fixed else 'passed'),
             _case('suite > skipped', 'skipped'),
             _case('suite > retried', 'passed' if fixed else 'failed', retried=fixed)]
    if (root / 'remove-case').exists():
        cases.pop(1)
    project = 'browser' if (root / 'browser-project').exists() else 'unit'
    first = (PurePosixPath(project_dir) / _FILES[0]).as_posix()
    files = [{'path': name, 'project': project, 'errors': [], 'tests': cases if name == first
              else [_case('suite > price', 'failed' if fixed else 'passed')]} for name in selected]
    raw = {'schema_version': 2, 'errors': [], 'files': files, 'vitest_version': expected_version}
    (folder / (side + '.log')).write_text('', encoding='utf-8')
    save_json(folder / (side + '.events.json'), raw)
    execution = {'status': 'completed', 'phase': 'vitest', 'exit_code': 0, 'duration_seconds': 0.1}
    return execution, vitest._derive_version(folder, side, raw, execution, selected, expected_version, 2)


@pytest.fixture
def source_and_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(vitest, '_version_run', _version_run)
    monkeypatch.setattr(vitest, '_runtime', lambda node, npm: _TOOLS)
    repository = tmp_path / 'source repository'
    repository.mkdir()
    _git(repository, 'init', '-q')
    save_json(repository / 'package.json', {'name': 'fixture', 'private': True})
    save_json(repository / 'package-lock.json', {'lockfileVersion': 3, 'packages': {
        'node_modules/vitest': {'version': '4.1.11', 'integrity': 'fixture', 'resolved': 'https://example.invalid/vitest'},
    }})
    (repository / 'value.txt').write_text('baseline', encoding='utf-8')
    (repository / 'value.txt').chmod(0o755)
    for name in _FILES:
        (repository / name).write_text('// selected fixture\n', encoding='utf-8')
    _git(repository, 'add', '.')
    _git(repository, 'commit', '-qm', 'baseline')
    (repository / 'value.txt').write_text('broken', encoding='utf-8')
    report = vitest.run_vitest_project(repository, 'HEAD', WORKTREE, _FILES, tmp_path / 'source reports', offline=True)
    assert check_project_report(report['report_dir'])['status'] == 'consistent'
    worktree = tmp_path / 'alternate worktree'
    shutil.copytree(Path(report['report_dir']) / 'evidence/project/inputs/after', worktree)
    _git(worktree, 'init', '-q')
    (worktree / 'value.txt').write_text('fixed', encoding='utf-8')
    return read_report_snapshot(report['report_dir']), worktree


def _recheck(source, worktree, output, **options):
    return vitest.recheck_vitest_project(source['report_dir'], output, expected_report=report_reference(source),
                                        repository=worktree, confirmed=True, **options)


def _cases(comparison, index=0):
    return {entry['test_id']: (entry['status'], entry['previous'], entry['current'])
            for entry in comparison['entries'][index]['case_entries']}


@pytest.mark.parametrize('workspace', [False, True])
def test_vitest_subproject_is_bound_to_comparison_replay_and_recheck(tmp_path, monkeypatch, capsys, workspace):
    monkeypatch.setattr(vitest, '_version_run', _version_run)
    monkeypatch.setattr(vitest, '_runtime', lambda node, npm: _TOOLS)
    repository = tmp_path / 'repository'
    project_dir = 'tools/apps/web client' if workspace else 'apps/web client'
    project = repository / project_dir
    install_root = repository / 'tools' if workspace else project
    install = {'root': install_root.relative_to(repository).as_posix(), 'workspaces': workspace}
    project.mkdir(parents=True)
    _git(repository, 'init', '-q')
    save_json(repository / 'package.json', {'name': 'outside', 'private': True})
    save_json(project / 'package.json', {'name': 'inside', 'private': True})
    lock = install_root / 'package-lock.json'
    packages = {'node_modules/vitest': {
        'version': '4.1.11', 'integrity': 'fixture', 'resolved': 'https://example.invalid/vitest'}}
    if workspace:
        patterns = ['apps/*', '!apps/excluded']
        save_json(install_root / 'package.json', {'name': 'npm-root', 'workspaces': patterns})
        for name in ('shared', 'excluded'):
            member = install_root / 'apps' / name
            member.mkdir()
            save_json(member / 'package.json', {'name': f'@fixture/{name}'})
        packages.update({
            '': {'workspaces': patterns},
            'apps/web client': {'name': 'inside'},
            'apps/shared': {'name': '@fixture/shared'},
            'node_modules/inside': {'resolved': 'apps/web client', 'link': True},
            'node_modules/@fixture/shared': {'resolved': 'apps/shared', 'link': True},
        })
    save_json(lock, {'lockfileVersion': 3, 'packages': packages})
    (project / 'value.txt').write_text('baseline', encoding='utf-8')
    (project / _FILES[1]).write_text('// selected fixture\n', encoding='utf-8')
    _git(repository, 'add', '.')
    _git(repository, 'commit', '-qm', 'baseline')
    selected = [(PurePosixPath(project_dir) / _FILES[1]).as_posix()]

    def cli(*arguments, code=0):
        assert main(list(map(str, arguments))) == code
        streams = capsys.readouterr()
        return json.loads(streams.out or streams.err)

    options = ('vitest', repository, '--before', 'HEAD', '--working-tree', '--project-dir', project_dir, '--offline')
    output = ('--output', tmp_path / 'reports')
    invalid = cli(*options, *output, '--file', _FILES[1], code=2)
    assert '不在 npm 项目目录' in invalid['error']['message']
    assert not (tmp_path / 'reports').exists()
    saved_lock = lock.read_bytes()
    lock.unlink()
    missing = cli(*options, *output, '--file', selected[0], code=2)
    assert '候选的 npm 项目目录' in missing['error']['message']
    assert 'package-lock.json' in missing['error']['message']
    lock.write_bytes(saved_lock)
    if workspace:
        packages['node_modules/@fixture/shared']['resolved'] = '../outside'
        packages['../outside'] = {'name': '@fixture/shared'}
        save_json(lock, {'lockfileVersion': 3, 'packages': packages})
        rejected = cli(*options, *output, '--file', selected[0], code=2)
        assert '仓库内' in rejected['error']['message']
        lock.write_bytes(saved_lock)

    found = selected * 2

    def discover(log, snapshots, label, files, tools, timeout, offline, npm_cache, control, *, project_dir, locked):
        assert label == 'after' and files is None and offline
        assert (snapshots / label / selected[0]).is_file()
        log.write_text('discovery fixture', encoding='utf-8')
        return {'status': 'completed', 'phase': 'vitest', 'exit_code': 0}, {
            'schema_version': 2, 'vitest_version': locked['version'], 'files': found, 'errors': [],
        }

    monkeypatch.setattr(vitest, '_invoke_vitest', discover)
    duplicate = cli(*options, *output, '--all-files', code=2)
    assert '同一文件' in duplicate['error']['message']
    assert not list((tmp_path / 'reports').iterdir())
    found = []
    empty = cli(*options, '--list-files', code=2)
    assert '没有匹配' in empty['error']['message']
    found = selected
    preview = cli(*options, '--list-files')
    assert preview['kind'] == 'vitest_file_discovery' and preview['files'] == selected
    assert preview['vitest_install'] == install

    report = cli(*options, *output, '--all-files')
    assert report['vitest_project_dir'] == project_dir
    assert report['vitest_discovery'] is True
    assert {'discovery.json', 'discovery.log'} <= report['persistence']['sha256'].keys()
    assert report['vitest_install'] == install
    assert report['selected_tests'] == selected
    folder = Path(report['report_dir'])
    assert project_dir in (folder / 'report.html').read_text(encoding='utf-8')
    summary = cli('summary', folder, '--format', 'json')
    assert summary['vitest_project_dir'] == project_dir
    if workspace:
        assert summary['vitest_install'] == install
    bundle = tmp_path / 'project.zip'
    cli('pack', folder, '--output', bundle)
    preview = cli('replay', bundle)
    assert preview['vitest_project_dir'] == project_dir and preview['selected_tests'] == selected
    assert preview['vitest_install'] == install
    receipt_dir = tmp_path / 'receipt'
    replayed = cli('replay', bundle, '--confirm-run', '--receipt-dir', receipt_dir, '--output', tmp_path / 'replayed')
    assert replayed['vitest_project_dir'] == project_dir
    assert replayed['replay_verification']['status'] == 'verified'
    assert 'vitest_discovery' not in replayed
    assert replayed['replay_verification']['inputs']['scope'] is True
    assert cli('verify-replay', bundle, receipt_dir / 'replay-evidence.zip') == replayed['replay_verification']

    source = read_report_snapshot(folder)
    (project / 'value.txt').write_text('fixed', encoding='utf-8')
    plan = tmp_path / 'vitest-plan.json'
    case = (selected[0], 'suite > price')
    # The second file's price case regresses when value.txt is fixed.
    save_acceptance_plan(AcceptancePlan('vitest', 'strict', (case,), baseline=baseline_pin(source)), plan)
    planned = cli(*options, '--acceptance-plan', plan, '--verify-plan', '--output', tmp_path / 'planned', code=1)
    assert planned['selected_tests'] == selected
    assert planned['acceptance']['required_improvements'][0]['outcome'] == 'regression_detected'
    assert check_project_report(planned['report_dir'])['status'] == 'consistent'

    missing_case_plan = tmp_path / 'missing-case.json'
    missing = (selected[0], 'suite > missing')
    save_acceptance_plan(AcceptancePlan('vitest', 'strict', (missing,)), missing_case_plan)
    (project / 'value.txt').write_text('baseline', encoding='utf-8')
    unresolved = cli(*options, '--acceptance-plan', missing_case_plan, '--verify-plan',
                     '--output', tmp_path / 'missing-case', code=2)
    assert unresolved['status'] == 'regression_passed'
    assert unresolved['acceptance']['required_improvements'] == [
        {'file': missing[0], 'test_id': missing[1], 'status': 'blocked', 'outcome': None}]
    assert Path(unresolved['report_dir']).is_dir()

    omitted_file_plan = tmp_path / 'omitted-file.json'
    outside_selection = (PurePosixPath(project_dir) / _FILES[0]).as_posix()
    save_acceptance_plan(AcceptancePlan('vitest', 'strict', ((outside_selection, 'suite > price'),),
                                        baseline=baseline_pin(source)), omitted_file_plan)
    markdown = tmp_path / 'blocked.md'
    selection_file = tmp_path / 'regression-tests.json'
    selection_file.write_text(json.dumps(['./' + selected[0]]), encoding='utf-8')
    explicit = cli(*options, '--selection-file', selection_file, '--acceptance-plan', omitted_file_plan,
                   '--verify-plan', '--summary-file', markdown, '--output', tmp_path / 'explicit', code=2)
    assert explicit['summary_file'] == str(markdown)
    assert 'Missing comparable result' in markdown.read_text(encoding='utf-8')
    assert explicit['selected_tests'] == selected
    assert explicit['acceptance']['required_improvements'] == [
        {'file': outside_selection, 'test_id': 'suite > price', 'status': 'blocked', 'outcome': None}]
    assert check_project_report(explicit['report_dir'])['status'] == 'consistent'

    missing_file_plan = tmp_path / 'missing-file.json'
    missing_file = (PurePosixPath(project_dir) / 'missing.test.ts').as_posix()
    save_acceptance_plan(AcceptancePlan('vitest', 'strict', ((missing_file, 'suite > price'),)), missing_file_plan)
    stopped = cli(*options, '--acceptance-plan', missing_file_plan, '--verify-plan',
                  '--output', tmp_path / 'missing-file', code=2)
    assert stopped['status'] == 'missing_targets' and stopped['missing_tests'] == [missing_file]
    assert not list((tmp_path / 'missing-file').iterdir())

    (project / 'value.txt').write_text('fixed', encoding='utf-8')
    rechecked = _recheck(source, repository, tmp_path / 'rechecked')
    assert rechecked['report']['vitest_project_dir'] == project_dir
    assert rechecked['report']['source_sha256']['before'] == source['source_sha256']['before']
    assert rechecked['comparison']['changes'] == []
    if workspace:
        changed_lock = json.loads(saved_lock)
        changed_lock['fixture_change'] = True
        save_json(lock, changed_lock)
        changed = _recheck(source, repository, tmp_path / 'changed-lock')
        assert '候选的 tools/package-lock.json 已变化' in changed['comparison']['changes']
        save_json(folder / 'report.json', {**report, 'vitest_install': {'root': '.', 'workspaces': True}})
        rejected = check_project_report(folder)
        assert rejected['status'] == 'inconsistent'
        assert '安装范围' in rejected['issues'][0]

    save_json(folder / 'report.json', {**report, 'vitest_project_dir': '.'})
    rejected = check_project_report(folder)
    assert rejected['status'] == 'inconsistent'
    assert '项目目录' in rejected['issues'][0]


def test_vitest_replay_verification_distinguishes_files_skips_and_retried_cases(source_and_worktree, tmp_path):
    source, worktree = source_and_worktree
    fixed = _recheck(source, worktree, tmp_path / 'fixed')['report']
    replay = replay_project_report(fixed['report_dir'], tmp_path / 'replayed', confirmed=True, verify_result=True)
    proof = replay['replay_verification']
    assert proof['status'] == 'incomplete'
    assert proof['counts'] == {'matched': 3, 'different': 0, 'incomplete': 2}
    by_case = {(row['file'], row['test_id']): row for row in proof['entries']}
    assert by_case[_FILES[0], 'suite > skipped']['status'] == 'incomplete'
    assert by_case[_FILES[0], 'suite > retried']['status'] == 'incomplete'
    assert by_case[_FILES[0], 'suite > price']['source'] != by_case[_FILES[1], 'suite > price']['source']


def test_vitest_recheck_matches_file_and_actual_case_and_keeps_evidence(source_and_worktree, tmp_path):
    source, worktree = source_and_worktree
    folder = Path(source['report_dir'])
    original = (folder / 'report.json').read_bytes()
    result = _recheck(source, worktree, tmp_path / 'reports')
    report, comparison = result['report'], result['comparison']
    assert comparison['changes'] == []
    assert _cases(comparison) == {
        'suite > price': ('resolved', 'failed', 'passed'),
        'suite > discount': ('new_failure', 'passed', 'failed'),
        'suite > skipped': ('not_reviewed', 'skipped', 'skipped'),
        'suite > retried': ('not_reviewed', 'failed', 'retry_passed'),
    }
    assert _cases(comparison, 1) == {'suite > price': ('new_failure', 'passed', 'failed')}
    assert report['before_commit'] == source['before_commit'] and report['after_commit'] == WORKTREE
    assert report['repository'] == str(worktree) and report['run_id'] != source['run_id']
    assert report['selected_tests'] == _FILES and report['offline'] is True
    for key in ('source_sha256', 'source_mode_sha256'):
        assert report[key]['before'] == source[key]['before']
    assert report['review_source'] == {**report_reference(source), 'selection_mode': 'all', 'selected_tests': _FILES}
    assert check_project_report(report['report_dir'])['status'] == 'consistent'
    assert (folder / 'report.json').read_bytes() == original
    replayed = replay_project_report(report['report_dir'], tmp_path / 'replayed', confirmed=True)
    assert replayed['review_source'] == report['review_source']
    assert check_project_report(replayed['report_dir'])['status'] == 'consistent'


def test_vitest_recheck_marks_condition_and_inventory_changes_and_reports_missing_file(source_and_worktree, tmp_path):
    source, worktree = source_and_worktree
    (worktree / _FILES[0]).write_text('// changed fixture\n', encoding='utf-8')
    lock = worktree / 'package-lock.json'
    lock.write_bytes(lock.read_bytes() + b'\n')
    (worktree / 'vitest.config.js').touch()
    (worktree / 'browser-project').touch()
    (worktree / 'remove-case').touch()
    output = tmp_path / 'reports'
    result = _recheck(source, worktree, output, offline=False, timeout=60)
    comparison = result['comparison']
    assert set(comparison['changes']) == {'候选的 package-lock.json 已变化', '候选的 Vitest 配置文件已变化',
                                         '离线安装模式不同或无法核对', '每版限时不同或无法核对'}
    assert set(comparison['entries'][0]['changes']) == {
        '共享测试文件内容已变化', '两轮候选所属的 Vitest 项目不同', '两轮候选的实际用例清单不同',
    }
    assert _cases(comparison)['suite > price'] == ('conditions_changed', 'failed', 'passed')
    assert _cases(comparison)['suite > discount'] == ('not_reviewed', 'passed', 'missing')
    assert 'resolved' not in comparison['counts']
    assert result['report']['selected_tests'] == _FILES
    assert check_project_report(result['report']['report_dir'])['status'] == 'consistent'

    (worktree / _FILES[0]).unlink()
    with pytest.raises(ValueError, match='没有所选测试文件：order totals.test.ts'):
        _recheck(source, worktree, output)
    assert list(output.iterdir()) == [Path(result['report']['report_dir'])]
    assert check_project_report(source['report_dir'])['status'] == 'consistent'


def test_vitest_recheck_preview_rechecks_source_and_cancel_cleans_up(source_and_worktree, tmp_path, monkeypatch):
    source, worktree = source_and_worktree
    folder = Path(source['report_dir'])
    path = folder / 'report.json'
    original = path.read_bytes()
    output = tmp_path / 'reports'
    arguments = {'expected_report': report_reference(source), 'repository': worktree}
    with monkeypatch.context() as patch:
        def unexpected(*args, **kwargs):
            pytest.fail('preview must not launch Node or Vitest')

        patch.setattr(vitest, '_runtime', unexpected)
        patch.setattr(vitest, '_version_run', unexpected)
        preview = vitest.recheck_vitest_project(folder, output, **arguments)
    assert preview['repository'] == source['repository']
    assert preview['selected_tests'] == _FILES and preview['offline'] is True
    assert not output.exists()

    path.write_bytes(original + b'\n')
    with pytest.raises(ValueError, match='来源身份不符'):
        vitest.recheck_vitest_project(folder, output, confirmed=True, **arguments)
    assert not output.exists()
    path.write_bytes(original)

    def cancel(*args, project_dir, locked):
        assert project_dir == locked['root'] == '.'
        control = args[-1]
        control.cancel()
        control.check()

    monkeypatch.setattr(vitest, '_version_run', cancel)
    with pytest.raises(RunCancelled):
        vitest.recheck_vitest_project(folder, output, confirmed=True, **arguments)
    assert not list(output.iterdir())
    assert path.read_bytes() == original


def test_vitest_recheck_old_checked_evidence_cannot_confirm_case_resolution(source_and_worktree, tmp_path):
    source, worktree = source_and_worktree
    folder = Path(source['report_dir'])
    execution = json.loads((folder / 'execution.json').read_text(encoding='utf-8'))
    execution['vitest_report_version'] = 1
    save_json(folder / 'execution.json', execution)
    for side in ('before', 'after'):
        path = folder / (side + '.events.json')
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['schema_version'] = 1
        save_json(path, raw)
        source[side] = vitest._derive_version(folder, side, raw, execution[side], _FILES, '4.1.11', 1)
    status, entries, counts = vitest._compare_legacy(source['before'], source['after'], _FILES)
    source.update(vitest_report_version=1, status=status, entries=entries, counts=counts, persistence=_manifest(folder))
    source.pop('case_counts')
    source.pop('_report_sha256')
    save_json(folder / 'report.json', source)
    assert check_project_report(folder)['status'] == 'consistent'
    comparison = _recheck(read_report_snapshot(folder), worktree, tmp_path / 'reports')['comparison']
    assert comparison['counts'] == {'not_reviewed': 5}
    assert '上一轮报告缺少可核对的逐用例证据' in comparison['entries'][0]['changes']
    assert _cases(comparison)['suite > price'] == ('not_reviewed', 'inconclusive', 'passed')
