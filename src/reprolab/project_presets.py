"""Named test selections shared by sessions of the same local repository."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from .persistence import save_json
from .project_selections import MAX_SELECTION_BYTES, validate_selection


def _repository(value):
    return str(Path(value).expanduser().resolve())


def _folder(root, repository):
    key = hashlib.sha256(_repository(repository).encode('utf-8')).hexdigest()
    return Path(root).expanduser() / key


def _path(root, repository, preset_id):
    if not isinstance(preset_id, str) or not re.fullmatch(r'[0-9a-f]{32}', preset_id):
        raise ValueError('选择方案标识无效。')
    return _folder(root, repository) / (preset_id + '.json')


def read_preset(root, repository, preset_id):
    path = _path(root, repository, preset_id)
    if path.is_symlink() or not path.is_file():
        raise ValueError('选择方案不存在或不是普通文件。')
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_SELECTION_BYTES + 1)
        if len(raw) > MAX_SELECTION_BYTES:
            raise ValueError('选择方案超过大小限制。')
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('schema_version') != 1
                or data.get('kind') != 'project_selection_preset'
                or data.get('preset_id') != preset_id or data.get('repository') != _repository(repository)
                or not isinstance(data.get('title'), str) or not 1 <= len(data['title']) <= 80):
            raise ValueError('选择方案记录无效。')
        data['selected_tests'] = validate_selection(data.get('selected_tests'))
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('选择方案内容无效或过大，请重新保存。') from None


def list_presets(root, repository):
    folder = _folder(root, repository)
    if not folder.is_dir():
        return {'presets': [], 'issues': []}
    presets, issues = [], []
    paths = sorted(folder.glob('*.json'))
    for path in paths[:100]:
        try:
            data = read_preset(root, repository, path.stem)
            presets.append({key: data.get(key) for key in ('preset_id', 'title', 'updated_at')}
                           | {'count': len(data['selected_tests'])})
        except (OSError, ValueError) as exc:
            issues.append(path.name + '：' + str(exc))
    if len(paths) > 100:
        issues.append('最多列出 100 个方案，请清理不再使用的方案。')
    return {'presets': sorted(presets, key=lambda item: item['title'].casefold()), 'issues': issues}


def save_preset(root, repository, title, selected, *, preset_id=None):
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
        raise ValueError('方案名称需要 1–80 个字符。')
    selected = validate_selection(selected)
    identity = preset_id or uuid.uuid4().hex
    path = _path(root, repository, identity)
    if preset_id:
        read_preset(root, repository, preset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'schema_version': 1, 'kind': 'project_selection_preset', 'preset_id': identity,
            'repository': _repository(repository), 'title': title.strip(), 'selected_tests': selected,
            'updated_at': datetime.now(timezone.utc).isoformat()}
    if len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > MAX_SELECTION_BYTES - 32768:
        raise ValueError('选择方案过大，请缩小范围后保存。')
    save_json(path, data)
    return data


def delete_preset(root, repository, preset_id):
    read_preset(root, repository, preset_id)
    _path(root, repository, preset_id).unlink()
