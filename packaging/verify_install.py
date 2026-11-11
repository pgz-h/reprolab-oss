"""Exercise installed CLI entry points outside the source checkout; no model calls."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True, help='New directory outside the source repository')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    output = args.output.expanduser().resolve()
    if output.is_relative_to(source):
        parser.error('Installation checks must run outside the source repository.')
    import reprolab
    installed = Path(reprolab.__file__).resolve()
    if installed.is_relative_to(source):
        parser.error('Use a fresh environment with the built wheel installed, not an editable/source install.')
    expected = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
    if reprolab.__version__ != expected or importlib.metadata.version('reprolab-local') != expected:
        parser.error('Installed code, metadata and source candidate versions differ.')
    output.mkdir(parents=True, exist_ok=False)
    checks = []

    def cli(*arguments, expected_code=0):
        command = [sys.executable, '-I', '-m', 'reprolab', *map(str, arguments)]
        result = subprocess.run(command, cwd=output, capture_output=True, text=True, timeout=90)
        if result.returncode != expected_code:
            raise RuntimeError(f'CLI failed: {arguments[0]} / exit {result.returncode}\n{result.stderr}\n{result.stdout}')
        return json.loads(result.stdout)

    for name in ('demo', 'history'):
        report = cli(name, '--output', output / name, '--json')
        if report['status'] != 'verified':
            raise RuntimeError(name + ' did not verify the declared fix.')
        checked = cli('report', 'check', report['report_dir'], '--json')
        if checked['status'] != 'consistent':
            raise RuntimeError(name + ' evidence is inconsistent.')
        checks.append(name + ': verified and evidence consistent')

    repo = output / 'project'
    repo.mkdir()
    (repo / 'answer.py').write_text('def answer():\n    return 0\n')
    (repo / 'test_answer.py').write_text('from answer import answer\ndef test_answer():\n    assert answer() == 42\n')
    def git(*arguments):
        subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        '-c', 'user.name=ReproLab installation check', '-c', 'user.email=test@example.invalid',
                        *arguments], check=True, capture_output=True)
    git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
    (repo / 'answer.py').write_text('def answer():\n    return 42\n')
    report = cli('project', 'review', repo, '--before', 'HEAD', '--working-tree',
                 '--python', sys.executable, '--test', 'test_answer.py::test_answer',
                 '--output', output / 'review', '--progress')
    if report['status'] != 'regression_passed' or report['entries'][0]['status'] != 'improved':
        raise RuntimeError('Installed project worker did not preserve before/after results.')
    if cli('project', 'check', report['report_dir'])['status'] != 'consistent':
        raise RuntimeError('Project evidence is inconsistent.')
    bundle = output / 'project.zip'
    cli('project', 'pack', report['report_dir'], '--output', bundle)
    imported = cli('project', 'unpack', bundle, output / 'imported')
    replay = cli('project', 'replay', imported['report_dir'], '--output', output / 'replayed', '--confirm-run')
    if replay['status'] != 'regression_passed' or replay['run_id'] == report['run_id']:
        raise RuntimeError('Replay did not create a new passing record.')
    if cli('project', 'check', replay['report_dir'])['status'] != 'consistent':
        raise RuntimeError('Replayed evidence is inconsistent.')
    checks.append('project: review, check, pack, unpack and replay')
    result = {'version': expected, 'installed_module': str(installed), 'python': sys.version.split()[0],
              'status': 'passed', 'checks': checks,
              'scope': 'Installed CLI and local controlled fixtures; no GUI or live model service.'}
    (output / 'installation-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
