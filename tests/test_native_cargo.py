"""Run the native CLI against a real local Rust package when Cargo is available."""
import json
import os
import shutil
import subprocess

import pytest

from reprolab.native_cargo import _case_outcome, _catalog
from reprolab.native_project import _compare
from reprolab.project_cli import main


def test_libtest_requires_exact_inventory_and_completed_case_evidence():
    assert _catalog('tests::a: test\ntests::b: test\n\n2 tests, 0 benchmarks\n') == ['tests::a', 'tests::b']
    assert _catalog('tests::a: test\ntests::a: test\n') is None
    assert _catalog('tests::a: benchmark\n') is None
    proof = '\nrunning 1 test\ntest tests::a ... ok\n\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1 filtered out\n'
    assert _case_outcome('tests::a', {'status': 'completed', 'exit_code': 0}, proof, catalog_size=2) == 'passed'
    assert _case_outcome('tests::a', {'status': 'timed_out', 'exit_code': None}, proof, catalog_size=2) == 'blocked'
    assert _case_outcome('tests::a', {'status': 'completed', 'exit_code': 0}, proof, catalog_size=1) == 'blocked'


def test_native_names_alone_or_empty_scope_cannot_give_green_gate():
    sides = {side: {'status': 'completed', 'tests': {'same': 'passed'}} for side in ('before', 'after')}
    assert _compare(sides, [], None)['counts'] == {'native_only': 1}
    assert _compare(sides, [], None)['gate']['exit_code'] == 2
    assert _compare(sides, [{'matches': False}], None)['gate']['exit_code'] == 2
    assert _compare(sides, [{'matches': True}], ['not_observed'])['gate']['exit_code'] == 2
    for side in sides:
        sides[side]['tests'] = {}
    assert _compare(sides, [{'matches': True}], None)['gate']['exit_code'] == 2


@pytest.fixture
def rust_project(tmp_path):
    tool = shutil.which('cargo')
    if tool is None:
        pytest.skip('Cargo unavailable')
    probe = subprocess.run([tool, '--version'], cwd=tmp_path, capture_output=True, timeout=15)
    if probe.returncode:
        pytest.skip('Cargo toolchain not initialized')
    root = tmp_path / 'Rust package'
    src = root / 'src'
    src.mkdir(parents=True)
    (root / 'Cargo.toml').write_text('[package]\nname = "native_fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / 'Cargo.lock').write_text('version = 3\n\n[[package]]\nname = "native_fixture"\nversion = "0.1.0"\n')
    (src / 'tests.rs').write_text(
        '#[test] fn improves() { match super::value() { 3 => panic!("fixture unavailable"), '
        '1 => panic!("receipt price differs"), _ => {} } }\n'
        '#[test] fn regresses() { assert_eq!(super::value(), 1); }\n'
        '#[test] fn remains_green() { assert!(super::value() > 0); }\n'
        '#[test] #[ignore = "fixture"] fn ignored() { panic!(); }\n')
    def write(value):
        (src / 'lib.rs').write_text(f'fn value() -> i32 {{ {value} }}\n#[cfg(test)] mod tests;\n')

    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), '-c', 'user.name=Native fixture',
                                        '-c', 'user.email=native@example.invalid', '-c', 'commit.gpgsign=false',
                                        '-c', f'core.hooksPath={os.devnull}', *args], text=True).strip()
    git('init', '-q')
    write(1)
    git('add', '.')
    git('commit', '-qm', 'before')
    before = git('rev-parse', 'HEAD')
    write(2)
    git('commit', '-qam', 'after')
    after = git('rev-parse', 'HEAD')
    return root, before, after, git, tool


def _run(rust_project, out, capsys, *, expected, revision=None, sources=('src/tests.rs',), targets=(), confirm=True, attest=False):
    root, before, after, _, tool = rust_project
    args = ['native', str(root), '--runner', 'cargo', '--before', before, '--after', revision or after,
            '--cargo', tool, '--output', str(out)]
    if confirm:
        args.append('--confirm-run')
    if attest:
        args.append('--attest-test-inputs')
    for source in sources:
        args.extend(('--shared-test-source', source))
    for name in targets:
        args.extend(('--test', name))
    assert main(args) == expected
    streams = capsys.readouterr()
    assert not streams.err
    return json.loads(streams.out)


def test_real_cargo_comparison_has_improvement_regression_and_ignored_with_read_only_zip(rust_project, tmp_path, capsys):
    out = tmp_path / 'native comparison'
    assert _run(rust_project, out, capsys, expected=0, confirm=False)['status'] == 'ready'
    assert not out.exists()
    report = _run(rust_project, out, capsys, expected=1)
    assert report['comparison']['counts'] == {'improved': 1, 'regression_detected': 1,
                                              'regression_passed': 1, 'inconclusive': 1}, report
    assert report['runs']['before']['tests']['tests::ignored'] == 'skipped'
    assert report['comparison']['shared_count'] == 4
    passing = _run(rust_project, tmp_path / 'selected comparison', capsys, expected=0,
                   targets=('tests::improves', 'tests::remains_green'), attest=True)
    assert passing['comparison']['counts'] == {'improved': 1, 'regression_passed': 1}
    plan = tmp_path / 'marker-plan.json'
    plan.write_text(json.dumps({
        'schema_version': 2, 'runner': 'cargo', 'commits': passing['commits'],
        'selected_tests': passing['selected_tests'], 'required_fixes': ['tests::improves'],
        'shared_test_sources': ['src/tests.rs'], 'shared_test_inputs': [],
        'baseline_failure_markers': {'tests::improves': 'receipt price differs'},
    }))
    marked_zip = tmp_path / 'marked-cargo.zip'
    assert main(['pack-native', passing['directory'], '--output', str(marked_zip)]) == 0
    capsys.readouterr()
    for marker, expected in [('receipt price differs', 0), ('another panic', 2), ('0 passed; 1 failed', 2)]:
        payload = json.loads(plan.read_bytes())
        payload['baseline_failure_markers']['tests::improves'] = marker
        variant = plan if expected == 0 else tmp_path / 'negative-marker-plan.json'
        if expected != 0:
            variant.write_text(json.dumps(payload))
        assert main(['check-native', str(marked_zip), '--review-plan', str(variant)]) == expected
        marked = json.loads(capsys.readouterr().out)
        assert marked['review_plan_acceptance']['baseline_marker_checks'] == {
            'tests::improves': 'matched' if expected == 0 else 'mismatch',
        }
        assert marked['comparison']['counts']['improved'] == 1

    root, _, after, git, cargo = rust_project
    (root / 'src/lib.rs').write_text('fn value() -> i32 { 3 }\n#[cfg(test)] mod tests;\n')
    git('commit', '-qam', 'alternate baseline panic')
    alternate_before = git('rev-parse', 'HEAD')
    alternate = _run((root, alternate_before, after, git, cargo), tmp_path / 'alternate', capsys,
                     expected=0, targets=('tests::improves', 'tests::remains_green'), attest=True)
    payload = json.loads(plan.read_bytes())
    payload.update(commits=alternate['commits'], baseline_failure_markers={'tests::improves': 'receipt price differs'})
    alternate_plan = tmp_path / 'alternate-marker-plan.json'
    alternate_plan.write_text(json.dumps(payload))
    alternate_zip = tmp_path / 'alternate-cargo.zip'
    assert main(['pack-native', alternate['directory'], '--output', str(alternate_zip)]) == 0
    capsys.readouterr()
    assert main(['check-native', str(alternate_zip), '--review-plan', str(alternate_plan)]) == 2
    wrong_failure = json.loads(capsys.readouterr().out)
    assert wrong_failure['comparison']['counts']['improved'] == 1
    assert wrong_failure['review_plan_acceptance']['reason'] == 'baseline_failure_marker_unverified'
    assert main(['check-native', str(alternate_zip), '--review-plan', str(alternate_plan), '--format', 'markdown']) == 2
    summary = capsys.readouterr().out
    assert '0/1 matched' in summary
    assert 'receipt price differs' not in summary and 'fixture unavailable' not in summary

    regression_plan = tmp_path / 'planned-regression.json'
    regression_plan.write_text(json.dumps({
        **json.loads(plan.read_bytes()), 'selected_tests': ['tests::improves', 'tests::regresses'],
    }))
    regression_dir = tmp_path / 'planned-regression'
    assert main(['native', str(root), '--review-plan', str(regression_plan), '--confirm-run',
                 '--attest-test-inputs', '--output', str(regression_dir)]) == 1
    regression_sender = json.loads(capsys.readouterr().out)
    regression_zip = tmp_path / 'planned-regression.zip'
    assert main(['pack-native', str(regression_dir), '--output', str(regression_zip)]) == 0
    capsys.readouterr()
    assert main(['check-native', str(regression_zip), '--review-plan', str(regression_plan)]) == 1
    regression_receiver = json.loads(capsys.readouterr().out)
    assert regression_sender['review_plan_acceptance'] == regression_receiver['review_plan_acceptance']
    assert regression_receiver['review_plan_acceptance']['reason'] == 'regression_detected'

    archive = tmp_path / 'portable.zip'
    (out / 'unrelated.txt').write_text('must not archive')
    assert main(['pack-native', str(out), '--output', str(archive)]) == 0
    capsys.readouterr()
    receiver = tmp_path / 'receiver'
    receiver.mkdir()
    relocated = receiver / 'received.zip'
    shutil.copyfile(archive, relocated)
    assert main(['check-native', str(relocated)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked['integrity'] == 'consistent' and checked['comparison'] == report['comparison']
    from zipfile import ZipFile
    with ZipFile(relocated) as zipped:
        assert not any('unrelated.txt' in name for name in zipped.namelist())
    (out / 'after/list.log').write_text('lost evidence')
    assert main(['check-native', str(out)]) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'


def test_real_cargo_changed_definitions_renamed_tests_and_build_failure_block(rust_project, tmp_path, capsys):
    root, _, _, git, _ = rust_project
    (root / 'src/tests.rs').write_text('#[test] fn renamed() { assert!(super::value() > 0); }\n')
    git('commit', '-qam', 'rename and remove')
    renamed = _run(rust_project, tmp_path / 'renamed', capsys, revision=git('rev-parse', 'HEAD'), expected=2)
    assert renamed['comparison']['shared_test_sources'] == 'changed_or_missing'
    assert renamed['comparison']['counts'] == {'only_before': 4, 'only_after': 1}
    (root / 'src/lib.rs').write_text('this cannot compile\n')
    git('commit', '-qam', 'syntax error')
    failed = _run(rust_project, tmp_path / 'compile error', capsys, revision=git('rev-parse', 'HEAD'), expected=2,
                  targets=('tests::remains_green',))
    assert failed['runs']['after']['reason'] == 'cargo_build_unverified'


def test_ctest_changed_or_missing_settings_keep_observations_without_hiding_a_regression():
    left = {'status': 'completed', 'tests': {'changed': 'failed', 'unknown': 'passed', 'regression': 'passed'},
            'test_settings': {'changed': 'a' * 64, 'regression': 'b' * 64}}
    right = {'status': 'completed', 'tests': {'changed': 'passed', 'unknown': 'passed', 'regression': 'failed'},
             'test_settings': {'changed': 'c' * 64, 'regression': 'b' * 64}}
    verified = [{'matches': True}]
    result = _compare({'before': left, 'after': right}, verified, None, runner='cmake')
    rows = {row['test_id']: row for row in result['entries']}
    assert rows['changed']['test_configuration'] == 'changed' and rows['changed']['status'] == 'native_only'
    assert rows['unknown']['test_configuration'] == 'unverified' and rows['unknown']['status'] == 'native_only'
    assert rows['regression']['status'] == 'regression_detected'
    assert result['gate']['exit_code'] == 1
    restricted = _compare({'before': left, 'after': right}, verified, ['changed', 'unknown'], runner='cmake')
    assert restricted['gate']['exit_code'] == 2
