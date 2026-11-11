"""Multi-round history keeps exact ancestry, stale observations and independent notes."""
from copy import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from reprolab import processing_history as timeline
from reprolab import project_history
from reprolab.project_refresh import refresh_from_report
from reprolab.project_sessions import prepare_project, collect_project, run_project, check_project_report
from reprolab.workspace import WORKTREE

ONE = 'tests/test_one.py::test_value'
TWO = 'tests/test_two.py::test_value'
THREE = 'tests/test_three.py::test_value'


def make_chain(root):
    repo = root / 'repo'; repo.mkdir()
    (repo / 'tests').mkdir()
    def values(one, two, three):
        (repo / 'component.py').write_text(f'VALUES = {{"one": {one}, "two": {two}, "three": {three}}}\n')
    values(1, 1, 1)
    for key in ('one', 'two', 'three'):
        (repo / 'tests' / f'test_{key}.py').write_text(f'from component import VALUES\ndef test_value(): assert VALUES["{key}"] == 1\n')
    def git(*args):
        subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid', *args], check=True, capture_output=True)
    git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
    values(0, 0, 0)
    first_state = prepare_project(repo, 'HEAD', WORKTREE, root / 'sessions', python_executable=sys.executable)
    collect_project(first_state['directory'])
    first = run_project(first_state['directory'], root / 'reports', selected_ids=[ONE, TWO, THREE], confirmed=True)
    values(1, 0, 0)
    second_state = refresh_from_report(first['report_dir'], root / 'sessions', selected_ids=[ONE])
    collect_project(second_state['directory'])
    second = run_project(second_state['directory'], root / 'reports', confirmed=True)
    values(1, 1, 0)
    third_state = refresh_from_report(second['report_dir'], root / 'sessions')
    collect_project(third_state['directory'])
    third = run_project(third_state['directory'], root / 'reports', selected_ids=[ONE, TWO], confirmed=True)
    return [first, second, third]


@pytest.fixture(scope='module')
def chain(tmp_path_factory):
    return make_chain(tmp_path_factory.mktemp('processing-chain'))


def test_history_zip_restores_chain_without_sender_paths(tmp_path, capsys):
    from reprolab.project_cli import main

    first, second, latest = make_chain(tmp_path)
    source = Path(second['report_dir'])
    moved = source.rename(tmp_path / 'moved')
    snapshot = Path(latest['report_dir'], 'report.json').read_bytes()
    bundle = tmp_path / 'history.zip'
    command = ['pack', latest['report_dir'], '--output', str(bundle), '--include-sources']
    assert main(command) == 2 and not bundle.exists()
    assert second['run_id'] in capsys.readouterr().err
    assert main([*command, '--source-directory', str(moved)]) == 0
    assert json.loads(capsys.readouterr().out)['report_count'] == 3

    # Neither the recipient nor the engine can read the sender's reports now.
    (tmp_path / 'reports').rename(tmp_path / 'sender-reports')
    moved.rename(tmp_path / 'sender-moved')
    imported = tmp_path / 'recipient'
    assert main(['unpack', str(bundle), str(imported)]) == 0
    assert json.loads(capsys.readouterr().out)['report_count'] == 3
    assert (imported / 'report.json').read_bytes() == snapshot
    assert main(['history', str(imported)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'complete' and result['summary']['not_rechecked'] == 1
    assert [row['reference']['run_id'] for row in result['rounds']] == [
        report['run_id'] for report in (first, second, latest)]
    assert project_history.read_recheck_progress(imported)['status'] == 'compared'
    assert main(['replay', str(imported), '--confirm-run', '--output', str(tmp_path / 'replay')]) == 0
    assert json.loads(capsys.readouterr().out)['counts'] == latest['counts']


def test_history_zip_rejects_missing_or_inconsistent_source(chain, tmp_path):
    from reprolab.project_sessions import pack_project_report, unpack_project_report

    original = tmp_path / 'history.zip'
    pack_project_report(chain[-1]['report_dir'], original, include_sources=True)
    with zipfile.ZipFile(original) as intact:
        root = Path(chain[-1]['report_dir']).name
        prefix = f'{root}/{project_history.BUNDLED_SOURCES}/'
        changed = tmp_path / 'changed.zip'
        removed = tmp_path / 'removed.zip'
        with zipfile.ZipFile(changed, 'w') as bad, zipfile.ZipFile(removed, 'w') as missing:
            for member in intact.infolist():
                content = intact.read(member)
                is_source = member.filename.startswith(prefix)
                if not is_source:
                    missing.writestr(copy(member), content)
                bad.writestr(copy(member), b'changed evidence' if is_source and member.filename.endswith('/before.log')
                             else content)
    for bundle, message in ((changed, '不一致'), (removed, '清单或目录缺失')):
        target = tmp_path / 'recipient'
        with pytest.raises(ValueError, match=message):
            unpack_project_report(bundle, target)
        assert not target.exists()


def test_three_rounds_keep_tests_omitted_for_multiple_rounds_and_use_real_conditions(chain):
    before = {report['report_dir']: (Path(report['report_dir']) / 'report.json').read_bytes() for report in chain}
    result = timeline.read_processing_history(chain[-1]['report_dir'])
    assert result['status'] == 'complete' and result['summary']['rounds'] == 3
    assert [row['reference']['run_id'] for row in result['rounds']] == [report['run_id'] for report in chain]
    assert result['summary']['selected_now'] == 2 and result['summary']['not_rechecked'] == 1
    assert result['summary']['not_rechecked_last_attention'] == 1
    rows = {row['test_id']: row for row in result['tests']}
    assert not rows[THREE]['selected_now'] and rows[THREE]['latest_observation']['round'] == 1
    assert rows[THREE]['latest_observation']['status'] == 'regression_detected'
    assert rows[TWO]['comparison']['previous_round'] == 1
    assert rows[TWO]['comparison']['change'] == 'conditions_changed'
    assert result['rounds'][2]['candidate_changes']['files'] == [{'path': 'component.py', 'kind': 'modified'}]
    assert all((Path(path) / 'report.json').read_bytes() == raw for path, raw in before.items())


def test_missing_wrong_and_moved_ancestor_are_explicit_without_guessing(chain, tmp_path):
    ref = chain[-1]['review_source']
    missing = timeline.read_processing_history(chain[-1]['report_dir'], locations={ref['report_sha256']: str(tmp_path / 'missing')})
    assert missing['status'] == 'partial' and missing['summary']['rounds'] == 1
    assert missing['source_gap']['reason'] == 'missing' and '已核对区间' in missing['summary_text']
    assert '更早的测试范围尚未读取' in missing['summary_text']
    assert '来源报告本次未找到' in missing['source_gap']['message']
    wrong = timeline.read_processing_history(chain[-1]['report_dir'], locations={ref['report_sha256']: chain[0]['report_dir']})
    assert wrong['status'] == 'partial' and '身份不符' in wrong['source_gap']['message']
    moved = tmp_path / 'relocated'; shutil.copytree(chain[1]['report_dir'], moved)
    result = timeline.read_processing_history(chain[-1]['report_dir'], locations={ref['report_sha256']: str(moved)})
    assert result['status'] == 'complete' and result['rounds'][1]['reference']['report_dir'] == str(moved)


def test_tampered_ancestor_does_not_contribute_results(chain, tmp_path):
    copy = tmp_path / 'tampered'; shutil.copytree(chain[1]['report_dir'], copy)
    (copy / 'before.log').write_text('changed evidence')
    ref = chain[-1]['review_source']
    result = timeline.read_processing_history(chain[-1]['report_dir'], locations={ref['report_sha256']: str(copy)})
    assert result['status'] == 'partial' and result['summary']['rounds'] == 1
    assert result['rounds'][0]['comparison'] is None and '不一致' in result['source_gap']['message']


def test_limits_do_not_claim_the_whole_history_was_read(chain, monkeypatch):
    result = timeline.read_processing_history(chain[-1]['report_dir'], max_rounds=2)
    assert result['status'] == 'partial' and result['source_gap']['reason'] == 'limit'
    assert result['summary']['rounds'] == 2
    size = (Path(chain[-1]['report_dir']) / 'report.json').stat().st_size
    monkeypatch.setattr(timeline, 'MAX_REPORT_BYTES', size + 1)
    result = timeline.read_processing_history(chain[-1]['report_dir'])
    assert result['status'] == 'partial' and result['summary']['rounds'] == 1


def test_history_does_not_execute_tests_and_notes_never_change_evidence(chain, tmp_path, monkeypatch):
    import reprolab.project_sessions as sessions
    def forbidden(*args, **kwargs): raise AssertionError('history must not run project code')
    monkeypatch.setattr(sessions, 'run_project', forbidden)
    monkeypatch.setattr(sessions, 'collect_project', forbidden)
    report = project_history._checked_report(chain[-1]['report_dir'])
    ref = project_history.report_reference(report)
    original = (Path(report['report_dir']) / 'report.json').read_bytes()
    notes = tmp_path / 'notes'
    saved = timeline.save_run_note(ref, '修复第二项；第三项尚未复查。', notes)
    result = timeline.read_processing_history(report['report_dir'], notes_root=notes)
    assert result['rounds'][-1]['note']['text'] == '修复第二项；第三项尚未复查。'
    assert saved['sha256']
    with pytest.raises(ValueError, match='其他窗口'):
        timeline.save_run_note(ref, '不能覆盖另一份说明', notes)
    with pytest.raises(ValueError, match='4000'):
        timeline.save_run_note(ref, 'x' * 4001, notes, expected_note_sha256=saved['sha256'])
    with pytest.raises(ValueError, match='之外'):
        timeline.save_run_note(ref, 'wrong destination', report['report_dir'])
    assert (Path(report['report_dir']) / 'report.json').read_bytes() == original
    assert check_project_report(report['report_dir'])['status'] == 'consistent'
    target = tmp_path / 'history.json'
    timeline.export_processing_history(result, target)
    assert json.loads(target.read_text())['rounds'][-1]['note']['text'] == saved['text']
    with pytest.raises(ValueError, match='新文件'):
        timeline.export_processing_history(result, target)
    with pytest.raises(ValueError, match='之外'):
        timeline.export_processing_history(result, Path(chain[0]['report_dir']) / 'history.json')


def test_corrupt_local_note_is_visible_but_does_not_hide_checked_rounds(chain, tmp_path):
    ref = project_history.report_reference(project_history._checked_report(chain[-1]['report_dir']))
    notes = tmp_path / 'notes'; notes.mkdir()
    (notes / (ref['report_sha256'] + '.json')).write_text('{broken')
    result = timeline.read_processing_history(chain[-1]['report_dir'], notes_root=notes)
    assert result['status'] == 'complete' and result['rounds'][-1]['note']['error']
    assert result['summary']['not_rechecked'] == 1


def test_cli_history_exports_json_and_marks_partial_sources(chain, tmp_path, capsys):
    from reprolab.project_cli import main
    assert main(['history', chain[-1]['report_dir'], '--output', str(tmp_path / 'history.json')]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['summary']['not_rechecked'] == 1
    assert main(['history', chain[-1]['report_dir'], '--max-rounds', '1']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'partial'


def test_same_title_without_explicit_source_remains_a_separate_record(chain):
    result = timeline.read_processing_history(chain[0]['report_dir'])
    assert result['summary']['rounds'] == 1 and result['source_gap'] is None


def test_workspace_refresh_links_displayed_report_without_replacing_selection_intent(chain, tmp_path):
    from reprolab.project_refresh import refresh_from_displayed_report
    latest = project_history._checked_report(chain[-1]['report_dir'])
    state = refresh_from_displayed_report(latest['project_directory'], tmp_path / 'sessions',
        project_history.report_reference(latest), selected_ids=[THREE])
    assert state['review_source']['run_id'] == latest['run_id']
    assert state['review_source']['selection_mode'] == 'workspace'
    assert state['selection_request']['selected_tests'] == [THREE]
    collect_project(state['directory'])
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    result = timeline.read_processing_history(report['report_dir'])
    assert result['summary']['rounds'] == 4
    assert result['summary']['not_rechecked'] == 2
    assert '当前工作区的勾选' in (Path(report['report_dir']) / 'report.html').read_text()
    wrong = project_history.report_reference(project_history._checked_report(chain[0]['report_dir']))
    with pytest.raises(ValueError, match='不属于'):
        refresh_from_displayed_report(latest['project_directory'], tmp_path / 'wrong', wrong)
    assert not (tmp_path / 'wrong').exists()



def test_cancellation_scope_limit_and_repeated_source_identity_are_explicit(chain, monkeypatch):
    from copy import deepcopy
    from reprolab.runner import RunControl, RunCancelled
    control = RunControl(); control.cancel()
    with pytest.raises(RunCancelled):
        timeline.read_processing_history(chain[-1]['report_dir'], control=control)
    monkeypatch.setattr(timeline, 'MAX_TESTS', 2)
    limited = timeline.read_processing_history(chain[-1]['report_dir'])
    assert limited['status'] == 'partial' and limited['source_gap']['reason'] == 'scope_limit'
    original = project_history._checked_report(chain[-1]['report_dir'])
    repeated = deepcopy(original)
    repeated['review_source'] = project_history.report_reference(original)
    monkeypatch.setattr(project_history, '_checked_report', lambda *args, **kwargs: repeated)
    result = timeline.read_processing_history(chain[-1]['report_dir'])
    assert result['status'] == 'partial' and result['source_gap']['reason'] == 'cycle'
