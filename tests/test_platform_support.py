"""Host contracts; Windows-native checks are executed by the Windows workflow."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from reprolab import platform_support as support
from reprolab.process_tree import spawn, terminate_tree


@pytest.mark.parametrize('system,relative', [
    ('darwin', 'Library/Application Support/ReproLab'),
    ('win32', 'AppData/Local/ReproLab'),
    ('linux', '.local/share/ReproLab'),
])
def test_user_data_directory_matches_host(tmp_path, monkeypatch, system, relative):
    monkeypatch.setattr(support.sys, 'platform', system)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    work, reports = support.application_directories()
    assert work == tmp_path / relative
    assert reports == tmp_path / 'Documents/ReproLab'


def test_windows_localappdata_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(support.sys, 'platform', 'win32')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'redirected'))
    assert support.application_directories()[0] == tmp_path / 'redirected/ReproLab'


def test_lock_blocks_another_process_and_releases_after_exception(tmp_path):
    lock = tmp_path / 'lock'
    code = '''from reprolab.platform_support import exclusive_lock
import sys
try:
    with exclusive_lock(sys.argv[1], 'busy'): pass
except ValueError:
    raise SystemExit(7)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
    with pytest.raises(RuntimeError):
        with support.exclusive_lock(lock, 'busy'):
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(lock)], env=env)
            assert result.returncode == 7
            raise RuntimeError('interrupted editor')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(lock)], env=env)
    assert result.returncode == 0


def test_gate_does_not_execute_before_release_or_on_eof(tmp_path):
    gate = Path(__file__).parents[1] / 'src/reprolab/_process_gate.py'
    marker = tmp_path / 'executed'
    command = [sys.executable, '-I', str(gate), sys.executable, '-c',
               'from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b"ran")', str(marker)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        time.sleep(.1)
        assert not marker.exists()
        process.stdin.close()
        assert process.wait(timeout=10) == 2
        assert not marker.exists()
    finally:
        if process.poll() is None: process.kill(); process.wait()
    result = subprocess.run(command, input=b'\x01', timeout=10)
    assert result.returncode == 0 and marker.read_bytes() == b'ran'


def test_managed_worker_runs_unicode_path_and_preserves_exit_code(tmp_path):
    script = tmp_path / '中文 worker.py'
    script.write_text('raise SystemExit(7)\n', encoding='utf-8')
    process = spawn([sys.executable, str(script)], stdin=subprocess.DEVNULL)
    try:
        assert process.wait(timeout=10) == 7
    finally:
        terminate_tree(process)


@pytest.mark.skipif(os.name != 'nt', reason='Requires Windows native ACL APIs')
def test_private_file_has_protected_user_and_system_acl(tmp_path):
    import win32api
    import win32con
    import win32security
    path = tmp_path / 'profile.json'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try: support.private_permissions(fd)
    finally: os.close(fd)
    info = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION)
    assert info.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    acl = info.GetSecurityDescriptorDacl()
    assert acl.GetAceCount() == 2
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try: user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally: token.Close()
    allowed = {win32security.ConvertSidToStringSid(acl.GetAce(i)[2]) for i in range(2)}
    assert win32security.ConvertSidToStringSid(user) in allowed
    assert 'S-1-5-18' in allowed


@pytest.mark.skipif(os.name != 'nt', reason='Requires Windows Job Objects')
@pytest.mark.parametrize('parent_exits', [False, True])
def test_windows_job_cleans_descendants_on_cancel_and_normal_exit(tmp_path, parent_exits):
    import win32api
    import win32con
    import win32event
    pidfile = tmp_path / 'child.pid'
    child = 'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)'
    script = tmp_path / 'parent.py'
    script.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
        f'child=subprocess.Popen([sys.executable,"-c",{child!r},{str(pidfile)!r}])\n'
        f'while not Path({str(pidfile)!r}).exists(): time.sleep(.01)\n'
        + ('' if parent_exits else 'time.sleep(60)\n'), encoding='utf-8')
    process = spawn([sys.executable, str(script)])
    handle = None
    try:
        deadline = time.monotonic() + 15
        while not pidfile.exists() and time.monotonic() < deadline: time.sleep(.02)
        assert pidfile.exists()
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(pidfile.read_text()))
        if parent_exits: assert process.wait(timeout=10) == 0
        terminate_tree(process)
        assert win32event.WaitForSingleObject(handle, 5000) == win32event.WAIT_OBJECT_0
    finally:
        terminate_tree(process)
        if handle is not None: handle.Close()
