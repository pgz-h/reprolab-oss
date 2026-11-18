"""Use an OS network denial policy for a trusted replay and record its startup probes."""
import errno
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

_PROFILE = '(version 1)(allow default)(deny network*)'
_MACOS_RECORD = {'schema_version': 1, 'mechanism': 'macos_sandbox_exec',
           'profile_sha256': hashlib.sha256(_PROFILE.encode()).hexdigest(),
           'tcp_bind': 'denied', 'tcp_connect': 'denied'}

_LINUX_RECORD = {'schema_version': 1, 'mechanism': 'linux_network_namespace',
                 'namespace_distinct': True, 'interfaces': ['lo'], 'ipv4_routes': 0}


_WINDOWS_RECORD = {'schema_version': 1, 'mechanism': 'windows_container_none',
                   'network_mode': 'none', 'active_external_interfaces': 0}


def validate_network_record(report, execution):
    value = report.get('network_isolation')
    if value != execution.get('network_isolation') or value is not None and value not in (_MACOS_RECORD, _LINUX_RECORD, _WINDOWS_RECORD):
        raise ValueError('报告的网络隔离记录与固定执行记录不一致。')


def run_isolated(argv):
    policy_args = []
    if sys.platform == 'darwin':
        sandbox = Path('/usr/bin/sandbox-exec')
        if not sandbox.is_file():
            raise ValueError('当前系统没有 macOS 系统隔离入口。')
        prefix = [str(sandbox), '-p', _PROFILE]
    elif sys.platform.startswith('linux'):
        unshare = shutil.which('unshare')
        if not unshare:
            raise ValueError('需要 Linux unshare 来建立独立网络命名空间。')
        parent = os.readlink('/proc/self/ns/net')
        prefix = [unshare, '--user', '--map-root-user', '--net', '--']
        policy_args = ['--original-netns', parent]
    elif sys.platform == 'win32':
        raise ValueError('Windows 断网重放需要 --windows-container NAME，且容器须以 --network none 启动。')
    else:
        raise ValueError('当前平台没有可用的操作系统网络隔离入口，未启动重放。')
    if getattr(sys, 'frozen', False):
        child = [sys.executable, '--reprolab-network-replay']
    else:
        child = [sys.executable, '-I', '-c',
                 'from reprolab.project_network import isolated_main; raise SystemExit(isolated_main())']
    with tempfile.TemporaryDirectory(prefix='reprolab-network-probe-') as directory:
        marker = Path(directory) / 'ready'
        process = subprocess.run([*prefix, *child, '--isolation-marker', str(marker), *policy_args, *argv],
                                 check=False)
        if marker.is_file() and marker.read_text(encoding='ascii') == 'verified\n':
            return process.returncode if process.returncode in (0, 1, 2) else 2
        return 2


def isolated_main():
    from .project_cli import _emit, main

    args = sys.argv[1:]
    try:
        if len(args) < 2 or args[0] != '--isolation-marker':
            raise ValueError('缺少隔离启动记录位置。')
        marker = Path(args[1])
        args = args[2:]
        if sys.platform.startswith('linux'):
            if len(args) < 2 or args[0] != '--original-netns':
                raise ValueError('缺少父进程网络命名空间记录。')
            current = os.readlink('/proc/self/ns/net')
            interfaces = sorted(name for _, name in socket.if_nameindex())
            routes = Path('/proc/net/route').read_text(encoding='ascii').strip().splitlines()
            # An empty namespace can expose either an empty table or just the route header.
            no_routes = not routes or (len(routes) == 1 and routes[0].split()[:2] == ['Iface', 'Destination'])
            if current == args[1] or interfaces != ['lo'] or not no_routes:
                raise ValueError(f'网络隔离预检受阻：独立命名空间={current != args[1]}，'
                                 f'接口={interfaces}，IPv4 路由表行数={len(routes)}。')
            args = args[2:]
            record = _LINUX_RECORD
        elif sys.platform == 'darwin':
            for operation, port in (('bind', 0), ('connect', 1)):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.settimeout(.25)
                    try:
                        getattr(probe, operation)(('127.0.0.1', port))
                    except OSError as exc:
                        if exc.errno in (errno.EACCES, errno.EPERM):
                            continue
                    raise ValueError(f'系统未明确拒绝 TCP {operation}，未启动重放。')
            record = _MACOS_RECORD
        else:
            raise ValueError('没有可用的系统网络隔离入口。')
        with marker.open('x', encoding='ascii', newline='\n') as stream:
            stream.write('verified\n')
    except (OSError, ValueError) as exc:
        _emit({'status': 'blocked', 'error': {'type': type(exc).__name__, 'message': str(exc)}}, stream=sys.stderr)
        return 2
    return main(args, _network_isolation=dict(record))
