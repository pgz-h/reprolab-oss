"""Probe only local tool versions, with the runner's environment and cleanup."""
from concurrent.futures import ThreadPoolExecutor
import os
import shlex
import time

import pytest

from reprolab import replay_toolchain
from reprolab.runner import RunCancelled, RunControl


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='tool fixtures use POSIX executables')


def _tool(folder, name, body):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text('#!/bin/sh\nset -eu\n' + body + '\n', encoding='utf-8')
    path.chmod(0o755)
    return path


def test_go_probe_uses_path_and_queries_outside_evidence(tmp_path, monkeypatch):
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    monkeypatch.chdir(evidence)
    tools = tmp_path / 'local tools'
    go = _tool(tools, 'go', f'''
test "$#" -eq 1
test "$1" = version
test "$PWD" != {shlex.quote(str(evidence))}
test "$GOENV:$GOWORK:$GOTOOLCHAIN:$GOPROXY:$CGO_ENABLED" = off:off:local:off:0
printf 'go version go1.27.3 darwin/arm64\n'
''')
    monkeypatch.setenv('PATH', str(tools))
    assert replay_toolchain.probe_replay_tools('go', control=RunControl()) == {
        'go': str(go), 'go_version': 'go version go1.27.3 darwin/arm64',
    }
    assert list(evidence.iterdir()) == []


def test_vitest_probe_uses_the_selected_node_environment_for_npm(tmp_path, monkeypatch):
    fallback = tmp_path / 'fallback'
    _tool(fallback, 'node', "printf 'v20.0.0\n'")
    node = _tool(tmp_path / 'selected', 'node', '''
test "$#" -eq 1
test "$1" = --version
test -z "${NODE_OPTIONS-}"
test -z "${NODE_PATH-}"
printf 'v26.13.0\n'
''')
    npm = _tool(fallback, 'npm', '''
test "$#" -eq 1
test "$1" = --version
test "$(node --version)" = v26.13.0
printf '11.20.0\n'
''')
    monkeypatch.setenv('PATH', str(fallback))
    monkeypatch.setenv('NODE_OPTIONS', '--invalid')
    monkeypatch.setenv('NODE_PATH', '/elsewhere')
    assert replay_toolchain.probe_replay_tools('vitest', node=node, control=RunControl()) == {
        'node': str(node), 'node_version': 'v26.13.0', 'npm': str(npm), 'npm_version': '11.20.0',
    }


def test_probe_reports_missing_start_failure_exit_and_timeout(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='找不到 Go'):
        replay_toolchain.probe_replay_tools('go', go=tmp_path / 'missing')
    invalid = tmp_path / 'invalid'
    invalid.write_text('#!/no-such-reprolab-interpreter\n', encoding='utf-8')
    invalid.chmod(0o755)
    with pytest.raises(ValueError, match='无法启动 Go'):
        replay_toolchain.probe_replay_tools('go', go=invalid, control=RunControl())
    failing = _tool(tmp_path, 'failing', "printf 'unavailable\n' >&2; exit 3")
    with pytest.raises(ValueError, match=r'退出码 3[\s\S]*unavailable'):
        replay_toolchain.probe_replay_tools('go', go=failing, control=RunControl())
    slow = _tool(tmp_path, 'slow', 'exec /bin/sleep 10')
    monkeypatch.setattr(replay_toolchain, '_VERSION_TIMEOUT', .1)
    with pytest.raises(ValueError, match='Go 版本查询超时'):
        replay_toolchain.probe_replay_tools('go', go=slow, control=RunControl())


def test_probe_cancels_an_active_version_process(tmp_path):
    marker = tmp_path / 'started'
    slow = _tool(tmp_path, 'slow', f'touch {shlex.quote(str(marker))}\nexec /bin/sleep 10')
    control = RunControl()
    with ThreadPoolExecutor(max_workers=1) as executor:
        query = executor.submit(replay_toolchain.probe_replay_tools, 'go', go=slow, control=control)
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        control.cancel()
        assert marker.exists()
        with pytest.raises(RunCancelled):
            query.result(timeout=3)
