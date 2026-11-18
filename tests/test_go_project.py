"""Exercise the Go CLI with actual tests and portable evidence."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from reprolab.acceptance_plan import AcceptancePlan, BaselinePin, baseline_pin, save_acceptance_plan
from reprolab.project_cli import main


@pytest.mark.skipif(shutil.which('go') is None, reason='Go toolchain is not installed')
def test_go_review_uses_candidate_tests_and_replays_checked_evidence(tmp_path, capsys):
    repo = tmp_path / 'fixture'
    package = repo / 'internal/cart'
    package.mkdir(parents=True)
    (repo / 'go.mod').write_text('module example.invalid/cart\n\ngo 1.20\n', encoding='utf-8')
    implementation = package / 'cart.go'
    implementation.write_text('package cart\nfunc Price() int { return 1 }\n', encoding='utf-8')
    old_test = package / 'old_test.go'
    old_test.write_text('package cart\nimport "testing"\n'
                        'func TestFixed(t *testing.T) { t.Fatal("old test") }\n', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false',
           '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid']
    for arguments in (('init', '-q'), ('add', '.'), ('commit', '-qm', 'baseline')):
        subprocess.run([*git, *arguments], check=True, capture_output=True)
    implementation.write_text('package cart\nfunc Price() int { return 2 }\n', encoding='utf-8')
    old_test.unlink()
    (package / 'cart_test.go').write_text('''package cart
import "testing"
func TestFixed(t *testing.T) {
    t.Run("price", func(t *testing.T) { if Price() != 2 { t.Fatal(Price()) } })
}
func TestRegressed(t *testing.T) { if Price() != 1 { t.Fatal(Price()) } }
func TestSkipped(t *testing.T) { t.Skip("fixture") }
func BenchmarkPrice(b *testing.B) {}
''', encoding='utf-8')
    (package / 'excluded_test.go').write_text(
        '//go:build reprolab_fixture_disabled\n\npackage cart\nimport "testing"\n'
        'func TestExcluded(t *testing.T) {}\n', encoding='utf-8')

    def cli(*arguments, code=0):
        assert main(list(map(str, arguments))) == code
        return json.loads(capsys.readouterr().out)

    selection = ('go', repo, '--before', 'HEAD', '--package', './internal/cart')
    committed = cli(*selection, '--after', 'HEAD', '--list-tests')
    assert committed['tests'] == ['TestFixed']
    discovered = cli(*selection, '--working-tree', '--list-tests')
    assert discovered['kind'] == 'go_test_discovery' and discovered['status'] == 'collected'
    assert discovered['tests'] == ['TestFixed', 'TestRegressed', 'TestSkipped']
    assert discovered['go_package'] == 'internal/cart'
    assert discovered['before_commit'] == committed['after_commit']

    report = cli(*selection, '--working-tree', '--all-tests', '--output', tmp_path / 'reports', code=1)
    assert report['selected_tests'] == discovered['tests']
    assert report['counts'] == {'improved': 1, 'regression_detected': 1, 'inconclusive': 1}
    assert 'Go 环境与执行范围' in Path(report['report_dir'], 'report.html').read_text(encoding='utf-8')
    assert cli('check', report['report_dir'])['status'] == 'consistent'
    summary = cli('summary', report['report_dir'], '--format', 'json',
                  '--include-test-ids', '--gate', 'no-new-failures', code=1)
    assert summary['runner'] == 'go' and summary['counts'] == report['counts']
    assert [item['test_id'] for item in summary['details']['items']] == [
        'TestRegressed', 'TestSkipped', 'TestFixed',
    ]
    plan = tmp_path / 'go-plan.json'
    pin = baseline_pin(report)
    save_acceptance_plan(AcceptancePlan('go', 'strict', ('TestFixed',), baseline=pin), plan)
    accepted = cli(*selection, '--working-tree', '--acceptance-plan', plan, '--verify-plan',
                   '--output', tmp_path / 'planned')
    assert accepted['selected_tests'] == ['TestFixed']
    assert accepted['acceptance']['gate']['status'] == 'passed'
    assert accepted['acceptance']['baseline_check']['reason'] == 'matched'
    assert cli('check', accepted['report_dir'])['status'] == 'consistent'

    markdown = tmp_path / 'regressed.md'
    selection_file = tmp_path / 'regression-tests.json'
    selection_file.write_text(json.dumps(['TestFixed', 'TestRegressed']), encoding='utf-8')
    wider = cli(*selection, '--working-tree', '--selection-file', selection_file,
                '--acceptance-plan', plan, '--verify-plan', '--summary-file', markdown,
                '--output', tmp_path / 'wider', code=1)
    assert wider['summary_file'] == str(markdown)
    assert '**CI gate** · Failed' in markdown.read_text(encoding='utf-8')
    assert wider['selected_tests'] == ['TestFixed', 'TestRegressed']
    assert wider['acceptance']['required_improvements'] == [
        {'test_id': 'TestFixed', 'status': 'passed', 'outcome': 'improved'}]
    assert wider['acceptance']['comparison_gate']['reason'] == 'new_failures'
    assert cli('check', wider['report_dir'])['status'] == 'consistent'

    missing_plan = tmp_path / 'missing-plan.json'
    save_acceptance_plan(AcceptancePlan('go', 'strict', ('TestFixed', 'TestExcluded')), missing_plan)
    blocked = cli(*selection, '--working-tree', '--acceptance-plan', missing_plan,
                  '--output', tmp_path / 'missing', code=2)
    assert blocked['status'] == 'missing_targets' and blocked['missing_tests'] == ['TestExcluded']
    assert not list((tmp_path / 'missing').iterdir())

    wrong_plan = tmp_path / 'wrong-plan.json'
    save_acceptance_plan(AcceptancePlan('go', 'strict', ('TestFixed',), baseline=BaselinePin('0'*64, '0'*64)),
                         wrong_plan)
    blocked = cli(*selection, '--working-tree', '--acceptance-plan', wrong_plan,
                  '--output', tmp_path / 'wrong', code=2)
    assert blocked['status'] == 'baseline_mismatch'
    assert not list((tmp_path / 'wrong').iterdir())

    bundle = tmp_path / 'evidence.zip'
    cli('pack', report['report_dir'], '--output', bundle)
    gated = cli('summary', bundle, '--format', 'json', '--require-improvement', 'TestFixed',
                '--require-improvement', 'TestSkipped', code=1)
    assert gated['required_improvements'] == [
        {'test_id': 'TestFixed', 'status': 'passed', 'outcome': 'improved'},
        {'test_id': 'TestSkipped', 'status': 'blocked', 'outcome': 'inconclusive'},
    ]
    imported = cli('unpack', bundle, tmp_path / 'imported')
    preview = cli('replay', bundle)
    assert preview['go_package'] == 'internal/cart'
    assert preview['source_archive'] == str(bundle) and 'report_dir' not in preview
    replay = cli('replay', imported['report_dir'], '--confirm-run', '--output', tmp_path / 'replays', code=1)
    assert replay['counts'] == report['counts']
    assert cli('check', replay['report_dir'])['status'] == 'consistent'
    proof = cli('verify-replay', bundle, replay['report_dir'], code=2)
    assert proof['status'] == 'incomplete'
    assert proof['counts'] == {'matched': 3, 'different': 0, 'incomplete': 1}
    assert all(proof['inputs'].values())

    implementation.write_text('package cart\nfunc invalid', encoding='utf-8')
    failed_output = tmp_path / 'broken-comparison'
    assert main(list(map(str, (*selection, '--working-tree', '--all-tests', '--output', failed_output)))) == 2
    error = capsys.readouterr()
    assert not error.out
    assert '完整 Go 测试清单' in json.loads(error.err)['error']['message']
    assert not list(failed_output.iterdir())

    implementation.write_text('package cart\nfunc Price() int { return 2 }\n', encoding='utf-8')
    (package / 'cart_test.go').unlink()
    assert main(list(map(str, (*selection, '--working-tree', '--list-tests')))) == 2
    empty = capsys.readouterr()
    assert not empty.out
    assert '没有可发现的顶层测试' in json.loads(empty.err)['error']['message']


@pytest.mark.skipif(shutil.which('go') is None, reason='Go toolchain is not installed')
def test_go_subtest_regression_does_not_disappear_when_parent_stays_red(tmp_path, capsys):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'go.mod').write_text('module example.invalid/mixed\n\ngo 1.20\n', encoding='utf-8')
    implementation = repo / 'mixed.go'
    implementation.write_text('package mixed\nconst Fixed, Broken = false, true\n', encoding='utf-8')
    (repo / 'mixed_test.go').write_text('''package mixed
import "testing"
func TestMixed(t *testing.T) {
    t.Run("repaired", func(t *testing.T) { if !Fixed { t.Fatal("old failure") } })
    t.Run("regressed", func(t *testing.T) { if !Broken { t.Fatal("new failure") } })
}
''', encoding='utf-8')
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for command in [('init', '-q'), ('add', '.'), ('commit', '-qm', 'baseline')]:
        subprocess.run([*git, *command], check=True, capture_output=True)
    implementation.write_text('package mixed\nconst Fixed, Broken = true, false\n', encoding='utf-8')

    def cli(*args, code=0):
        assert main(list(map(str, args))) == code
        return json.loads(capsys.readouterr().out)

    report = cli('go', repo, '--before', 'HEAD', '--working-tree', '--test', 'TestMixed',
                 '--output', tmp_path / 'reports', code=1)
    entry = report['entries'][0]
    assert entry['before']['status'] == entry['after']['status'] == 'failed'
    assert {case['test_id']: case['status'] for case in entry['case_entries']} == {
        'TestMixed/repaired': 'improved', 'TestMixed/regressed': 'regression_detected',
    }
    archive = tmp_path / 'go.zip'
    cli('pack', report['report_dir'], '--output', archive)
    summary = cli('summary', archive, '--gate', 'no-new-failures', '--format', 'json', code=1)
    assert summary['go_subtests']['counts'] == {'improved': 1, 'regression_detected': 1}


@pytest.mark.skipif(shutil.which('go') is None, reason='Go toolchain is not installed')
def test_go_large_log_preserves_terminal_regression_and_portable_evidence(tmp_path, capsys):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'go.mod').write_text('module example.invalid/noisy\n\ngo 1.20\n')
    source = repo / 'service.go'
    source.write_text('package noisy\nconst Ok = true\n')
    (repo / 'service_test.go').write_text('''package noisy
import ("fmt"; "strings"; "testing")
func TestNoisy(t *testing.T) {
    fmt.Print(strings.Repeat(strings.Repeat("x", 1023)+"\\n", 9000))
    if !Ok { t.Fatal("terminal regression") }
}
''')
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    for args in (('init', '-q'), ('add', '.'), ('commit', '-qm', 'baseline')):
        subprocess.run([*git, *args], check=True, capture_output=True)
    source.write_text('package noisy\nconst Ok = false\n')

    def cli(*args, code=0):
        assert main(list(map(str, args))) == code
        return json.loads(capsys.readouterr().out)

    report = cli('go', repo, '--before', 'HEAD', '--working-tree', '--test', 'TestNoisy',
                 '--output', tmp_path / 'reports', code=1)
    assert report['go_report_version'] == 3
    assert report['counts'] == {'regression_detected': 1}
    assert report['after']['omitted_output_chars'] > 8 * 1024 * 1024
    assert 'terminal regression' in report['entries'][0]['after']['exception']['message']
    archive = tmp_path / 'noisy.zip'
    cli('pack', report['report_dir'], '--output', archive)
    checked = cli('summary', archive, '--gate', 'strict', '--format', 'json', code=1)
    assert checked['gate']['reason'] == 'new_failures'
