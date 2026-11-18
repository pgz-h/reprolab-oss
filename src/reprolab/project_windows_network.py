"""Run trusted replay in a caller-prepared, network-disconnected Windows container."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import __version__


def run_container(name, arguments):
    if sys.platform != 'win32' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise ValueError('请选择 Windows 本机的有效容器名或 ID。')
    docker = shutil.which('docker')
    if not docker:
        raise ValueError('找不到本机 Windows 容器引擎的 docker CLI。')
    engine = [docker, '--host', 'npipe:////./pipe/docker_engine']
    inspected = subprocess.run([*engine, 'container', 'inspect', name], capture_output=True, timeout=20)
    if inspected.returncode:
        raise ValueError('无法检查指定的本机 Windows 容器。')
    info, = json.loads(inspected.stdout)
    identity = info.get('Id', '')
    if (not re.fullmatch(r'[a-f0-9]{64}', identity) or info.get('Platform') != 'windows'
            or info.get('State', {}).get('Running') is not True
            or info.get('HostConfig', {}).get('NetworkMode') != 'none'
            or set(info.get('NetworkSettings', {}).get('Networks') or {}) - {'none'}):
        raise ValueError('容器须在本机运行 Windows，网络模式为 none，且没有连接其他网络。')
    # Reparse the original arguments inside the container. The internal record
    # prevents redispatch; target paths and tool locations belong to the guest.
    child = [*engine, 'exec', identity, 'python', '-I', '-X', 'utf8', '-c',
             'from reprolab.project_windows_network import guest_main; raise SystemExit(80 + guest_main())',
             __version__, *arguments]
    # A Docker transport error may itself return 1. Only a completed guest
    # verdict can be presented to CI as a confirmed test failure.
    code = subprocess.run(child).returncode
    return code - 80 if code in (80, 81, 82) else 2


def guest_main():
    from .project_cli import _emit, main
    from .project_network import _WINDOWS_RECORD

    # Server Core includes Windows PowerShell. Check both connected IPv4 and
    # IPv6 interfaces, independently of the daemon's network-mode metadata.
    query = (
        "$ErrorActionPreference='Stop'; "
        "$active=@([System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | "
        "Where-Object { $_.OperationalStatus -eq 'Up' -and $_.NetworkInterfaceType -ne 'Loopback' }); "
        "Write-Output $active.Count"
    )
    try:
        if sys.platform != 'win32' or sys.argv[1] != __version__:
            raise ValueError('容器需要 Windows 和相同版本的 ReproLab。')
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        probe = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', query],
                               capture_output=True, timeout=30)
        if probe.returncode or probe.stdout.strip() != b'0':
            raise ValueError('Windows 容器内未能确认活动非回环接口数为零。')
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _emit({'status': 'blocked', 'error': {'type': type(exc).__name__, 'message': str(exc)}}, stream=sys.stderr)
        return 2
    return main(sys.argv[2:], _network_isolation=dict(_WINDOWS_RECORD))
