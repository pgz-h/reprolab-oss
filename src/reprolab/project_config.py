"""Reviewable, reversible edits to the original repository's pytest profile."""
from .platform_support import exclusive_lock, open_nofollow, preserve_permissions
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import tomllib
import uuid

import tomlkit

from .persistence import atomic_write, save_json
from .pytest_profile import MAX_CONFIG_BYTES, _validate_profile
from .recipes import repository_root
from .runner import RunControl


@dataclass(frozen=True)
class ConfigPreview:
    repository: str
    original: bytes | None
    updated: bytes | None
    identity: tuple | None
    write_mode: int
    plugins: tuple = ()
    import_mode: str | None = None
    undo_record: str | None = None
    environment: tuple = ()
    pass_env: tuple = ()

    @property
    def changed(self):
        return self.original != self.updated

    @property
    def path(self):
        return Path(self.repository) / 'pyproject.toml'

    @property
    def diff(self):
        lines = difflib.unified_diff(
            (self.original or b'').decode('utf-8-sig').splitlines(),
            (self.updated or b'').decode('utf-8-sig').splitlines(),
            fromfile='pyproject.toml（当前）', tofile='pyproject.toml（撤销后）' if self.undo_record else 'pyproject.toml（应用后）', lineterm='')
        return '\n'.join(lines)


def _root(repository):
    root = Path(repository).expanduser().resolve()
    if root != repository_root(root):
        raise ValueError('配置修改需要选择原 Git 项目的根目录，不接受冻结快照或子目录。')
    return root


def _read(path):
    try:
        fd = open_nofollow(path, os.O_RDONLY)
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES or info.st_nlink != 1:
            raise ValueError('配置需要是不超过 256 KiB 的普通文件，不能是链接或共享硬链接。')
        content = stream.read(MAX_CONFIG_BYTES + 1)
        after = os.fstat(stream.fileno())
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode))
    if len(content) > MAX_CONFIG_BYTES or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, stat.S_IMODE(after.st_mode)):
        raise ValueError('读取时配置发生变化，请重新预览。')
    return content, identity


def _document(content):
    try:
        return tomllib.loads((content or b'').decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('pyproject.toml 格式无效；请先修正原文件再预览。') from exc


def _profile(data):
    current = data
    for key in ('tool', 'reprolab', 'pytest'):
        current = current.get(key, {})
        if not isinstance(current, dict):
            raise ValueError('tool.reprolab.pytest 需要为配置表，请先核对原文件。')
    return _validate_profile(current)


def _unrelated(data):
    result = deepcopy(data)
    tool = result.get('tool', {})
    reprolab = tool.get('reprolab', {})
    reprolab.pop('pytest', None)
    if not reprolab:
        tool.pop('reprolab', None)
    if not tool:
        result.pop('tool', None)
    return result


def _updated(original, profile):
    data = _document(original)
    if _profile(data) == profile:
        return original
    text = (original or b'').decode('utf-8-sig')
    document = tomlkit.parse(text)
    current = document
    for key in ('tool', 'reprolab', 'pytest'):
        if key not in current:
            current[key] = tomlkit.table()
        current = current[key]
    current['plugins'] = profile['plugins']
    if profile['import_mode'] is None:
        current.pop('import_mode', None)
    else:
        current['import_mode'] = profile['import_mode']
    if profile.get('environment'):
        if 'environment' not in current:
            current['environment'] = tomlkit.table()
        for name in list(current['environment']):
            if name not in profile['environment']:
                del current['environment'][name]
        for name, value in profile['environment'].items():
            if current['environment'].get(name) != value:
                current['environment'][name] = value
    else:
        current.pop('environment', None)
    if profile.get('pass_env'):
        if list(current.get('pass_env', [])) != profile['pass_env']:
            current['pass_env'] = profile['pass_env']
    else:
        current.pop('pass_env', None)
    rendered = tomlkit.dumps(document)
    if original and b'\r\n' in original and b'\n' not in original.replace(b'\r\n', b''):
        rendered = rendered.replace('\r\n', '\n').replace('\n', '\r\n')
    updated = (b'\xef\xbb\xbf' if original and original.startswith(b'\xef\xbb\xbf') else b'') + rendered.encode('utf-8')
    if len(updated) > MAX_CONFIG_BYTES:
        raise ValueError('修改后的配置超过 256 KiB，未写入。')
    parsed = _document(updated)
    if _unrelated(parsed) != _unrelated(data) or _profile(parsed) != profile:
        raise ValueError('无法在保留其他配置的前提下生成修改，请手工核对。')
    return updated


def configuration_requires_refresh(snapshot, repository):
    return _read(Path(snapshot) / 'pyproject.toml')[0] != _read(Path(repository) / 'pyproject.toml')[0]


def preview_configuration(repository, plugins, import_mode=None, *, environment=None, pass_env=None):
    root = _root(repository)
    original, identity = _read(root / 'pyproject.toml')
    previous = _profile(_document(original))
    profile = _validate_profile({'plugins': list(plugins), 'import_mode': import_mode,
        'environment': previous.get('environment', {}) if environment is None else environment,
        'pass_env': previous.get('pass_env', []) if pass_env is None else pass_env})
    return ConfigPreview(str(root), original, _updated(original, profile), identity,
                         identity[-1] if identity else 0o644, tuple(profile['plugins']), import_mode,
                         environment=tuple(profile.get('environment', {}).items()), pass_env=tuple(profile.get('pass_env', [])))


def _history(repository, history_root):
    root = Path(history_root).expanduser().resolve()
    repository = Path(repository)
    if root.is_relative_to(repository):
        raise ValueError('配置撤销记录须保存在原项目之外。')
    directory = root / hashlib.sha256(str(repository).encode()).hexdigest()[:24]
    if directory.is_symlink():
        raise ValueError('撤销记录目录不能是符号链接。')
    return directory


@contextmanager
def _edit_lock(directory):
    with exclusive_lock(Path(directory) / '.edit.lock', '这个项目的配置正在保存或撤销，请稍后重新预览。'):
        yield

def _replace(plan, control):
    current, identity = _read(plan.path)
    if current != plan.original or identity != plan.identity:
        raise ValueError('原配置或权限已变化，未覆盖；请重新预览。')
    if identity is not None and not identity[-1] & 0o222:
        raise ValueError('原配置为只读文件，未写入。')
    temporary = None
    try:
        if plan.updated is not None:
            fd, name = tempfile.mkstemp(prefix='.reprolab-config-', dir=plan.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'wb') as stream:
                preserve_permissions(stream.fileno(), plan.path, plan.write_mode)
                stream.write(plan.updated)
                stream.flush()
                os.fsync(stream.fileno())
        if _read(plan.path) != (plan.original, plan.identity):
            raise ValueError('保存前原配置再次变化，未覆盖；请重新预览。')
        control.commit()
        if plan.updated is None:
            plan.path.unlink()
        else:
            os.replace(temporary, plan.path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _hash(content):
    return hashlib.sha256(content or b'').hexdigest()


def apply_configuration(plan, history_root, *, control=None):
    control = control or RunControl()
    control.check()
    if plan.undo_record:
        raise ValueError('撤销预览须使用撤销入口。')
    fresh = preview_configuration(plan.repository, plan.plugins, plan.import_mode,
                                  environment=dict(plan.environment), pass_env=list(plan.pass_env))
    if fresh != plan:
        raise ValueError('预览后的原配置或权限已变化，未覆盖；请重新预览。')
    if not plan.changed:
        return {'status': 'unchanged', 'repository': plan.repository, 'path': str(plan.path)}
    parent = _history(plan.repository, history_root)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _edit_lock(parent):
        record_dir = parent / (str(time.time_ns()) + '-' + uuid.uuid4().hex[:8])
        record_dir.mkdir(mode=0o700)
        atomic_write(record_dir / 'before.toml', plan.original or b'')
        atomic_write(record_dir / 'after.toml', plan.updated)
        record = {'schema_version': 1, 'kind': 'project_config_edit', 'repository': plan.repository,
                  'before_exists': plan.original is not None, 'before_sha256': _hash(plan.original),
                  'after_sha256': _hash(plan.updated), 'mode': plan.write_mode, 'state': 'prepared'}
        save_json(record_dir / 'record.json', record)
        control.check()
        _replace(plan, control)
        record['state'] = 'applied'
        warning = ''
        try:
            save_json(record_dir / 'record.json', record)
        except OSError:
            warning = '配置已保存；撤销记录状态未能更新，原始备份仍保留，可重新打开窗口核对并撤销。'
        return {'status': 'applied', 'repository': plan.repository, 'path': str(plan.path),
                'record': str(record_dir), 'warning': warning}


def _record(directory, repository):
    directory = Path(directory)
    for path in (directory, directory / 'record.json', directory / 'before.toml', directory / 'after.toml'):
        if path.is_symlink():
            raise ValueError('撤销记录不能是符号链接。')
    raw, _ = _read(directory / 'record.json')
    if raw is None:
        raise ValueError('撤销记录缺失。')
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get('kind') != 'project_config_edit' or record.get('schema_version') != 1
            or record.get('repository') != str(repository) or type(record.get('before_exists')) is not bool
            or type(record.get('mode')) is not int or not 0 <= record['mode'] <= 0o777
            or record.get('state') not in ('prepared', 'applied', 'undone')):
        raise ValueError('撤销记录与原项目不一致。')
    before, _ = _read(directory / 'before.toml')
    after, _ = _read(directory / 'after.toml')
    if before is None or after is None or _hash(before) != record.get('before_sha256') or _hash(after) != record.get('after_sha256'):
        raise ValueError('撤销备份内容不一致，未修改项目。')
    if not record['before_exists'] and before != b'':
        raise ValueError('撤销备份的原文件状态不一致。')
    _profile(_document(before)); _profile(_document(after))
    if _unrelated(_document(before)) != _unrelated(_document(after)):
        raise ValueError('撤销记录包含插件配置范围之外的改动。')
    return record, before if record['before_exists'] else None, after


def latest_configuration_edit(repository, history_root):
    repository = _root(repository)
    parent = _history(repository, history_root)
    if not parent.exists():
        return None
    for directory in sorted((item for item in parent.iterdir() if item.is_dir()), reverse=True):
        if not (directory / 'record.json').exists():
            continue
        record, _, after = _record(directory, repository)
        if record['state'] == 'applied' or (record['state'] == 'prepared' and _read(repository / 'pyproject.toml')[0] == after):
            return str(directory)
    return None


def preview_configuration_undo(repository, record_directory):
    repository = _root(repository)
    record, original, written = _record(record_directory, repository)
    current, identity = _read(repository / 'pyproject.toml')
    if record['state'] == 'undone' or current != written or identity is None or identity[-1] != record['mode']:
        raise ValueError('应用配置后文件或权限又有变化，未撤销；请先核对原项目。')
    return ConfigPreview(str(repository), current, original, identity, record['mode'], undo_record=str(record_directory))


def undo_configuration(plan, *, control=None):
    control = control or RunControl()
    control.check()
    if not plan.undo_record or preview_configuration_undo(plan.repository, plan.undo_record) != plan:
        raise ValueError('撤销预览已变化，请重新预览。')
    with _edit_lock(Path(plan.undo_record).parent):
        record, _, _ = _record(plan.undo_record, plan.repository)
        _replace(plan, control)
        record['state'] = 'undone'
        warning = ''
        try:
            save_json(Path(plan.undo_record) / 'record.json', record)
        except OSError:
            warning = '原配置已恢复，但撤销记录状态未能更新；请核对原项目，勿重复撤销。'
        return {'status': 'undone', 'repository': plan.repository, 'path': str(plan.path),
                'record': plan.undo_record, 'warning': warning}
