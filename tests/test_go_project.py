"""Exercise the Go CLI with actual tests and portable evidence."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

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
''', encoding='utf-8')

    def cli(*arguments, code=0):
        assert main(list(map(str, arguments))) == code
        return json.loads(capsys.readouterr().out)

    report = cli('go', repo, '--before', 'HEAD', '--working-tree', '--package', './internal/cart',
                 '--test', 'TestFixed', '--test', 'TestRegressed', '--test', 'TestSkipped',
                 '--output', tmp_path / 'reports', code=1)
    assert report['counts'] == {'improved': 1, 'regression_detected': 1, 'inconclusive': 1}
    assert 'Go 环境与执行范围' in Path(report['report_dir'], 'report.html').read_text(encoding='utf-8')
    assert cli('check', report['report_dir'])['status'] == 'consistent'
    bundle = tmp_path / 'evidence.zip'
    cli('pack', report['report_dir'], '--output', bundle)
    imported = cli('unpack', bundle, tmp_path / 'imported')
    assert cli('replay', imported['report_dir'])['go_package'] == 'internal/cart'
    replay = cli('replay', imported['report_dir'], '--confirm-run', '--output', tmp_path / 'replays', code=1)
    assert replay['counts'] == report['counts']
    assert cli('check', replay['report_dir'])['status'] == 'consistent'
