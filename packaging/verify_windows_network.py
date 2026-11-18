"""Exercise native Windows network-isolated replay on an ephemeral container host."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4


def run(*args, code=0, cwd=None):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, cwd=cwd,
                            encoding='utf-8', errors='replace', timeout=300)
    if result.returncode != code:
        raise RuntimeError(f'{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}')
    return result.stdout


def main(root, image):
    name = 'reprolab-' + uuid4().hex
    repo = root / 'fixture'
    repo.mkdir(parents=True)
    cli = [sys.executable, '-I', '-X', 'utf8', '-m', 'reprolab', 'project']
    created = []
    try:
        for suffix, network in [('online', 'nat'), ('offline', 'none')]:
            container = run('docker', 'run', '-d', '--name', name + suffix, '--network', network,
                            image, 'python', '-m', 'http.server', '8123').strip()
            created.append(container)
        online, offline = created
        info = json.loads(run('docker', 'inspect', online))[0]
        address = info['NetworkSettings']['Networks']['nat']['IPAddress']
        if not address:
            raise RuntimeError('The control container has no NAT address.')
        for attempt in range(20):
            try:
                with socket.create_connection((address, 8123), timeout=2):
                    break
            except OSError:
                if attempt == 19:
                    raise
                time.sleep(.5)
        probe = f'import socket; socket.create_connection(({address!r}, 8123), timeout=3).close()'
        (repo / 'test_network.py').write_text(
            'import subprocess, sys, socket\n\n'
            f'def test_worker():\n    {probe}\n\n'
            f'def test_child():\n    subprocess.run([sys.executable, "-I", "-c", {probe!r}], check=True)\n',
            encoding='utf-8')
        for args in [('init', '-q'), ('add', '.'), ('commit', '-qm', 'positive network control')]:
            run('git', '-c', 'user.name=CI', '-c', 'user.email=ci@example.invalid', *args, cwd=repo)
        source = json.loads(run(*cli, 'review', repo, '--before', 'HEAD', '--after', 'HEAD',
                                '--output', root / 'source'))
        assert source['counts'] == {'regression_passed': 2}, source['counts']
        bundle = root / 'source.zip'
        run(*cli, 'pack', source['report_dir'], '--output', bundle)
        replay = ['replay', r'C:\source.zip', '--confirm-run', '--verify-result', '--output', r'C:\results']
        for container in created:
            run('docker', 'cp', bundle, container + r':C:\source.zip')
        positive = json.loads(run('docker', 'exec', online, 'python', '-I', '-X', 'utf8', '-m', 'reprolab', 'project', *replay))
        assert positive['replay_verification']['counts']['matched'] == 2
        run(*cli, *replay, '--network-isolated', '--windows-container', online, code=2)
        result = json.loads(run(*cli, *replay, '--network-isolated', '--windows-container', offline, code=1))
        assert result['replay_verification']['counts']['different'] == 2
        assert result['network_isolation']['mechanism'] == 'windows_container_none'
        for entry in result['entries']:
            assert entry['before']['status'] == entry['after']['status'] == 'failed'
            error = entry['after']['exception']
            if entry['test_id'].endswith('test_worker'):
                assert error['type'] in ('OSError', 'TimeoutError', 'ConnectionRefusedError'), error
            else:
                assert error['type'] == 'CalledProcessError', error
        checked = json.loads(run('docker', 'exec', offline, 'python', '-I', '-X', 'utf8', '-m', 'reprolab',
                                 'project', 'check', result['report_dir']))
        assert checked['status'] == 'consistent'
        run('docker', 'exec', offline, 'python', '-I', '-X', 'utf8', '-m', 'reprolab', 'project', 'pack',
            result['report_dir'], '--output', r'C:\isolated.zip')
        run('docker', 'cp', offline + r':C:\isolated.zip', root / 'isolated.zip')
        (root / 'replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('NAT control matched both tests; connected target refused; offline worker and child both failed.')
    finally:
        for container in created:
            subprocess.run(['docker', 'rm', '-f', container], check=False)


if __name__ == '__main__':
    main(Path(sys.argv[1]), sys.argv[2])
