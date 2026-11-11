"""Small local editor drafts. Never store application settings or runtime values."""
from .platform_support import exclusive_lock
from datetime import datetime, timezone
from contextlib import contextmanager
import os
import hashlib
import json
from pathlib import Path

from .persistence import atomic_write

MAX_DRAFT_BYTES = 1024 * 1024


def identity(material):
    ref = material['reference']
    return {'repository': str(material.get('path_roots', {}).get('repository') or ''),
            'project_id': ref['project_id'], 'run_id': ref['run_id'],
            'report_sha256': ref['report_sha256'], 'test_id': material['test_id'],
            'test_sha256': material['test_sha256']}


def draft_path(root, binding):
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return Path(root) / (key + '.json')


def _bytes(path):
    if path.is_symlink():
        raise ValueError('草稿不能是链接文件。')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_DRAFT_BYTES:
        raise ValueError('草稿不是普通文件或超过 1 MiB。')
    data = path.read_bytes()
    if len(data) > MAX_DRAFT_BYTES:
        raise ValueError('草稿超过 1 MiB。')
    return data


def _input(value):
    if (not isinstance(value, list) or len(value) != 5 or
            any(not isinstance(x, str) for x in value[:4]) or
            not isinstance(value[4], list) or any(not isinstance(x, str) for x in value[4])):
        raise ValueError('草稿表单状态无效。')


def read_draft(path):
    path = Path(path)
    raw = _bytes(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if data['schema_version'] != 1 or data['kind'] != 'repair_brief_draft':
            raise ValueError('草稿版本不受支持；原文件保留。')
        binding = data['identity']
        if set(binding) != {'repository', 'project_id', 'run_id', 'report_sha256', 'test_id', 'test_sha256'} or any(not isinstance(v, str) for v in binding.values()):
            raise ValueError('草稿来源无效。')
        if path.name != draft_path(path.parent, binding).name:
            raise ValueError('草稿文件名与来源不一致。')
        state = data['state']
        if set(state) != {'inputs', 'preview_inputs', 'preview', 'generated'}:
            raise ValueError('草稿编辑状态无效。')
        _input(state['inputs']); _input(state['preview_inputs'])
        if any(not isinstance(state[k], str) for k in ('preview', 'generated')):
            raise ValueError('草稿预览无效。')
        if not isinstance(data['report_dir'], str) or not isinstance(data['saved_at'], str):
            raise ValueError('草稿来源路径或时间无效。')
        if not isinstance(data['block_titles'], dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in data['block_titles'].items()):
            raise ValueError('草稿材料清单无效。')
    except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('无法读取草稿；原文件保留。') from exc
    return {**data, '_sha256': hashlib.sha256(raw).hexdigest(), '_path': str(path)}


@contextmanager
def _locked(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('草稿目录不能是链接。')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_lock(root / '.write.lock', '其他进程正在保存草稿，请稍后重试。'):
        yield

def save_draft(root, material, state, *, expected=None):
    with _locked(root):
        return _save_draft(root, material, state, expected=expected)


def _save_draft(root, material, state, *, expected=None):
    # Explicit allowlist: never serialize the caller's complete material/settings.
    binding = identity(material)
    path = draft_path(root, binding)
    if path.parent.is_symlink():
        raise ValueError('草稿目录不能是链接。')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = _bytes(path)
    actual = hashlib.sha256(previous).hexdigest() if previous is not None else None
    if actual != expected:
        raise ValueError('草稿已被其他窗口或进程修改；未覆盖，请重开核对。')
    payload = {'schema_version': 1, 'kind': 'repair_brief_draft', 'identity': binding,
               'report_dir': material.get('reference', {}).get('report_dir', ''),
               'saved_at': datetime.now(timezone.utc).isoformat(),
               'block_titles': {k: v['title'] for k,v in material['blocks'].items()},
               'state': {k: state[k] for k in ('inputs', 'preview_inputs', 'preview', 'generated')}}
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    if len(raw) > MAX_DRAFT_BYTES:
        raise ValueError('草稿超过 1 MiB，未保存；请精简内容。')
    atomic_write(path, raw)  # mkstemp uses 0600; complete bytes are fsynced before replace.
    return read_draft(path)


def discard_draft(path, *, expected):
    with _locked(Path(path).parent):
        _discard_draft(path, expected=expected)


def _discard_draft(path, *, expected):
    path = Path(path)
    raw = _bytes(path)
    if raw is not None:
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('草稿已被其他窗口或进程修改；未删除，请重开核对。')
        path.unlink()


def recovery_material(draft):
    binding = draft['identity']
    return {'reference': {k: binding[k] for k in ('project_id', 'run_id', 'report_sha256')},
            'test_id': binding['test_id'], 'test_sha256': binding['test_sha256'],
            'path_roots': {'repository': binding['repository']},
            'blocks': {k: {'title': v, 'text': ''} for k,v in draft['block_titles'].items()}}
