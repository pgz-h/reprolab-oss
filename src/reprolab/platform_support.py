"""User paths, non-following opens and interprocess locks on supported hosts."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import sys


def application_directories():
    home = Path.home()
    if sys.platform == 'win32':
        work = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData/Local') / 'ReproLab'
    elif sys.platform == 'darwin':
        work = home / 'Library/Application Support/ReproLab'
    else:
        work = Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share') / 'ReproLab'
    return work, home / 'Documents/ReproLab'


def open_nofollow(path, flags, mode=0o600):
    if os.name != 'nt':
        return os.open(path, flags | os.O_NOFOLLOW, mode)
    import msvcrt
    import win32con
    import win32file
    import pywintypes
    access = win32con.GENERIC_READ
    if flags & os.O_RDWR:
        access |= win32con.GENERIC_WRITE
    elif flags & os.O_WRONLY:
        access = win32con.GENERIC_WRITE
    # New private files also need WRITE_DAC before their content is written.
    if flags & os.O_CREAT:
        access |= win32con.WRITE_DAC
    disposition = win32con.OPEN_EXISTING
    if flags & os.O_CREAT:
        disposition = win32con.CREATE_NEW if flags & os.O_EXCL else win32con.OPEN_ALWAYS
    try:
        handle = win32file.CreateFile(str(path), access,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, disposition, win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
    except pywintypes.error as exc:
        if exc.winerror in (2, 3):
            raise FileNotFoundError(errno.ENOENT, str(exc), str(path)) from exc
        raise OSError(exc.winerror, str(exc), str(path)) from exc
    try:
        if win32file.GetFileInformationByHandle(handle)[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('文件不能是符号链接或重解析点。')
        native = handle.Detach()
        handle = None
        descriptor = msvcrt.open_osfhandle(native,
            (flags & (os.O_RDONLY | os.O_RDWR | os.O_WRONLY | os.O_APPEND)) | os.O_BINARY)
        return descriptor
    finally:
        if handle is not None:
            handle.Close()


def private_permissions(fd):
    if os.name != 'nt':
        os.fchmod(fd, 0o600)
        return
    import msvcrt
    import win32api
    import win32con
    import win32file
    import win32security
    import ntsecuritycon
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    acl = win32security.ACL()
    for sid in (user, system):
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    path = win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(fd), 0)
    win32security.SetNamedSecurityInfo(path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)


def preserve_permissions(fd, original, mode):
    if os.name != 'nt':
        os.fchmod(fd, mode)
        return
    if not Path(original).exists():
        private_permissions(fd)
        return
    import msvcrt
    import win32file
    import win32security
    security = win32security.GetNamedSecurityInfo(str(original), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION)
    protected = bool(security.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED)
    flag = (win32security.PROTECTED_DACL_SECURITY_INFORMATION if protected
            else win32security.UNPROTECTED_DACL_SECURITY_INFORMATION)
    destination = win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(fd), 0)
    win32security.SetNamedSecurityInfo(destination, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | flag, None, None,
        security.GetSecurityDescriptorDacl(), None)


@contextmanager
def exclusive_lock(path, message):
    fd = open_nofollow(path, os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise BlockingIOError() from exc
                    raise
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            raise ValueError(message) from None
        yield
    finally:
        try:
            if locked and os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
