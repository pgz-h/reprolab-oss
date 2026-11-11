"""Verify the Windows release CLI and exercise Qt from the build environment."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This acceptance script requires Windows and the actual Windows executable.')
    exe = args.exe.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((source/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', QT_QPA_PLATFORM='offscreen')
    env.pop('PYTHONPATH', None)
    checks = []

    def cli(*arguments):
        result = subprocess.run([str(exe), '--cli', *map(str, arguments)], cwd=output,
            env=env, capture_output=True, encoding='utf-8', timeout=120)
        if result.returncode:
            raise RuntimeError(f'Frozen CLI failed: {arguments[0]}\n{result.stdout}\n{result.stderr}')
        return json.loads(result.stdout)

    version = subprocess.run([str(exe), '--cli', '--version'], cwd=output,
        env=env, capture_output=True, encoding='utf-8', timeout=30, check=True).stdout.strip()
    if expected not in version:
        raise RuntimeError('Frozen executable version differs from source.')
    checks.append('frozen version')
    for name in ('demo', 'history'):
        report = cli(name, '--output', output/(name+' 中文 报告'), '--json')
        assert report['status'] == 'verified'
        assert cli('report', 'check', report['report_dir'], '--json')['status'] == 'consistent'
        checks.append(name+': frozen workers, Unicode paths and evidence')

    repo = output/'project with spaces'
    repo.mkdir()
    (repo/'answer.py').write_text('def answer(): return 0\n', encoding='utf-8')
    (repo/'test_answer.py').write_text('from answer import answer\ndef test_answer(): assert answer()==42\n', encoding='utf-8')
    def git(*arguments):
        subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=NUL', '-c', 'commit.gpgsign=false',
            '-c', 'user.name=ReproLab acceptance', '-c', 'user.email=test@example.invalid',
            *arguments], check=True, capture_output=True, env=env)
    git('init', '-q'); git('add', '.'); git('commit', '-qm', 'baseline')
    (repo/'answer.py').write_text('def answer(): return 42\n', encoding='utf-8')
    state = cli('project', 'prepare', repo, '--before', 'HEAD', '--working-tree',
                '--path', 'test_answer.py', '--output', output/'sessions')
    report = cli('project', 'run', state['directory'], '--output', output/'reports')
    assert report['status']=='regression_passed' and report['entries'][0]['status']=='improved'
    assert report['collection']['collection_path'] == 'test_answer.py'
    assert cli('project', 'check', report['report_dir'])['status']=='consistent'
    cli('project', 'pack', report['report_dir'], '--output', output/'evidence.zip')
    imported = cli('project', 'unpack', output/'evidence.zip', output/'imported')
    replay = cli('project', 'replay', imported['report_dir'], '--output', output/'replayed', '--confirm-run')
    assert replay['status']=='regression_passed'
    assert replay['collection']['collection_path'] == 'test_answer.py'
    assert cli('project', 'check', replay['report_dir'])['status']=='consistent'
    checks.append('project preparation, execution, evidence import/export and replay')

    for name, filename, key in (
        ('desktop', 'smoke-result.json', 'ok'),
        ('desktop_onboarding', 'onboarding-result.json', 'status'),
        ('desktop_config', 'config-result.json', 'status'),
        ('desktop_suite', 'suite-result.json', 'ok'),
    ):
        destination = output/name
        script = source / 'packaging' / (name + '_smoke.py')
        result = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(script), str(destination)], cwd=output,
            env=env, capture_output=True, encoding='utf-8', timeout=240)
        (output/(name+'-stdout.log')).write_text(result.stdout+'\n'+result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'{name} Qt check failed; inspect its saved result and logs.')
        record = json.loads((destination/filename).read_text(encoding='utf-8'))
        if record.get(key) not in (True, 'passed'):
            raise RuntimeError(f'{name} Qt check did not record success.')
        checks.append(name + ': Qt workflow from installed Python package')
    record = {'version': expected, 'status': 'passed', 'platform': platform.platform(),
        'checks': checks, 'limits': 'Windows runner, frozen CLI, Qt offscreen from installed Python package, trusted local fixtures and mock model services; unsigned; no independent user trial or live vendor account.'}
    (output/'windows-validation.json').write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
