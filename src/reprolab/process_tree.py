"""Own a trusted worker tree until cleanup, including its normal exit."""
import os
from pathlib import Path
import signal
import subprocess
import sys


class WindowsJob:
    def __init__(self):
        import win32job
        self.handle = win32job.CreateJobObject(None, None)
        try:
            info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)
        except BaseException:
            self.close()
            raise

    def assign(self, pid):
        import win32api
        import win32con
        import win32job
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(self.handle, handle)
        finally:
            handle.Close()

    def close(self):
        if self.handle is not None:
            self.handle.Close()
            self.handle = None


def spawn(command, **options):
    options.setdefault('start_new_session', os.name == 'posix')
    if os.name != 'nt':
        return subprocess.Popen(command, **options)
    # The gate imports no project code and waits for a release byte. Assignment
    # must succeed before the requested worker or installer can create children.
    if options.get('stdin') not in (None, subprocess.DEVNULL):
        raise ValueError('Windows worker gate requires noninteractive stdin.')
    options.pop('start_new_session', None)
    options['stdin'] = subprocess.PIPE
    options['creationflags'] = options.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    if getattr(sys, 'frozen', False):
        gate = [sys.executable, '--reprolab-process-gate']
    else:
        gate = [sys.executable, '-I', '-X', 'utf8', str(Path(__file__).with_name('_process_gate.py'))]
    job = WindowsJob()
    process = None
    try:
        process = subprocess.Popen([*gate, *map(str, command)], **options)
        job.assign(process.pid)
        process._reprolab_job = job
        process.stdin.write(b'\x01')
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        return process
    except BaseException:
        job.close()
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait()
        raise


def terminate_tree(process):
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        job = getattr(process, '_reprolab_job', None)
        if job is not None:
            job.close()
        elif process.poll() is None:
            process.kill()
    return process.wait()
