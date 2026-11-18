"""Exercise both execution orders through the project CLI and portable evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from reprolab.project_cli import main


TEST = 'test_cache.py::test_request'


@pytest.mark.parametrize(('case', 'expected', 'exit_code'), [
    ('warm-up', 'second_position_passes', 1),
    ('repair', 'candidate_passes_both', 0),
    ('blocked', 'incomplete', 2),
    ('original-regression', 'both_pass', 1),
])
def test_version_order_from_saved_evidence(tmp_path, monkeypatch, capsys, case, expected, exit_code):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)

    git('init', '-q')
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\npass_env = ["EXAMPLE_CACHE"]\n', encoding='utf-8')
    (repo / 'test_cache.py').write_text(
        'import os\nimport time\nfrom pathlib import Path\n'
        'from version import CANDIDATE\n'
        'def test_request():\n'
        '    managed = os.environ.get("REPROLAB_PAIR_STATE_DIR")\n'
        '    marker = Path(managed or os.environ["EXAMPLE_CACHE"]) / "ready"\n'
        '    warm = marker.exists()\n'
        '    marker.touch()\n'
        f'    case = {case!r}\n'
        '    if case == "original-regression":\n'
        '        assert managed or not CANDIDATE\n'
        '        return\n'
        '    if case == "blocked" and CANDIDATE and not warm:\n'
        '        time.sleep(30)\n'
        '    assert warm or (case == "repair" and CANDIDATE)\n',
        encoding='utf-8',
    )
    version = repo / 'version.py'
    version.write_text('CANDIDATE = False\n', encoding='utf-8')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'baseline')
    version.write_text('CANDIDATE = True\n', encoding='utf-8')
    cache = tmp_path / 'original-cache'
    cache.mkdir()
    monkeypatch.setenv('EXAMPLE_CACHE', str(cache))

    def cli(args, code=0):
        assert main(list(map(str, args))) == code
        return json.loads(capsys.readouterr().out)

    src = cli(['review', repo, '--before', 'HEAD', '--working-tree', '--test', TEST,
               '--python', sys.executable, '--output', tmp_path / 'original'],
              1 if case == 'original-regression' else 0)
    assert src['source_sha256']['before'] != src['source_sha256']['after']
    bundle = tmp_path / 'original.zip'
    cli(['pack', src['report_dir'], '--output', bundle])
    original_bytes = Path(src['report_dir'], 'report.json').read_bytes()
    output = tmp_path / 'controlled'
    args = ['version-order', bundle, '--output', output, '--timeout', 2]
    assert cli(args)['status'] == 'ready'
    assert not output.exists()

    result = cli([*args, '--confirm-run'], exit_code)
    assert result['entries'][0]['pattern'] == expected
    assert result['status'] == ('incomplete' if case == 'blocked' else 'completed')
    assert result['original']['gate']['status'] == ('failed' if case == 'original-regression' else 'passed')
    assert Path(src['report_dir'], 'report.json').read_bytes() == original_bytes
    assert [result['groups'][group]['started_versions'] for group in ('ab', 'ba')] == [
        ['before', 'after'], ['after', 'before'],
    ]
    if case == 'warm-up':
        assert result['entries'][0]['observations'] == {
            'ab': {'before': 'failed', 'after': 'passed'},
            'ba': {'before': 'passed', 'after': 'failed'},
        }
    elif case == 'repair':
        assert result['entries'][0]['observations']['ba']['after'] == 'passed'
    elif case == 'blocked':
        assert result['groups']['ba']['runs']['after'] == 'timed_out'

    reports = {}
    for group, report in result['reports'].items():
        reports[group] = tmp_path / f'{group}.zip'
        cli(['pack', report, '--output', reports[group]])
    args = ['check-version-order', bundle, '--ab', reports['ab']]
    missing = cli(args, 2)
    assert missing['entries'][0]['pattern'] == 'incomplete'
    assert missing['groups']['ba'] is None
    markdown = Path(result['directory'], 'version-order.md')
    checked = cli([*args, '--ba', reports['ba'], '--markdown', markdown], 2 if case == 'blocked' else 0)
    assert checked['markdown_status'] == 'consistent'
    assert checked['entries'] == result['entries']
    assert main([*map(str, args), '--ba', str(reports['ab'])]) == 2
    assert '版本先后对照' in capsys.readouterr().err
    checked_gate = cli(['summary', reports['ab'], '--gate', 'no-new-failures', '--format', 'json',
                        '--require-improvement', TEST], 2)
    assert checked_gate['required_improvements'][0]['status'] == 'blocked'
