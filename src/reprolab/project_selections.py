"""Portable pytest selections; these files contain no execution verdict."""
import json
from pathlib import Path

from .case import parse_test_target
from .persistence import atomic_write

MAX_SELECTION_BYTES = 8 * 1024 * 1024
MAX_SELECTED = 2000


def validate_selection(values, *, allow_empty=False):
    if not isinstance(values, list) or not (0 if allow_empty else 1) <= len(values) <= MAX_SELECTED:
        raise ValueError('测试选择清单需要包含 1–2000 个完整测试 ID。')
    selected = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 4000 or '::' not in value:
            raise ValueError('选择清单包含无效的完整测试 ID。')
        parse_test_target(value)
        if value not in selected:
            selected.append(value)
    return selected


def reconcile_selection(selected, available, *, collection_complete=True):
    """Preserve unspecified, empty, and pending ranges as distinct states."""
    requested = None if selected is None else validate_selection(selected, allow_empty=True)
    available = set(available)
    matched = [identity for identity in requested or [] if identity in available]
    missing = [identity for identity in requested or [] if identity not in available]
    pending = bool(requested) and (bool(missing) or collection_complete is not True)
    state = 'unspecified' if requested is None else 'empty' if not requested else 'pending' if pending else 'matched'
    return {'requested_tests': requested, 'matched_tests': matched, 'missing_tests': missing,
            'requested_count': len(requested or []), 'matched_count': len(matched),
            'missing_count': len(missing), 'available_count': len(available),
            'other_count': len(available) - len(matched), 'range_state': state,
            'status': 'missing' if pending else state, 'requires_confirmation': pending,
            'collection_complete': collection_complete,
            'notice': '只按完整测试 ID 核对；本次未找到不代表已删除。发现不完整时需审阅范围，不自动替换目标或产生验收结论。'}


def requested_selection(state):
    request = state.get('selection_request')
    if request is None:
        return None
    if (not isinstance(request, dict) or request.get('schema_version') != 1
            or request.get('kind') != 'project_selection_request'):
        raise ValueError('继承的测试选择记录无效，请明确提供新的选择清单。')
    return validate_selection(request.get('selected_tests'), allow_empty=True)


def read_selection(path, *, allow_empty=False):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SELECTION_BYTES:
        raise ValueError('选择清单须为不超过 8 MiB 的普通 UTF-8 文件。')
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_SELECTION_BYTES + 1)
        if len(raw) > MAX_SELECTION_BYTES:
            raise ValueError('选择清单超过大小限制。')
        text = raw.decode('utf-8-sig')
        if path.suffix.lower() != '.txt' and text.lstrip().startswith(('[', '{')):
            value = json.loads(text)
            values = value.get('selected_tests') if isinstance(value, dict) else value
        else:
            values = [line for line in text.splitlines() if line.strip()]
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('无法读取选择清单；请使用 JSON 列表或每行一个完整测试 ID 的文本。') from None
    return validate_selection(values, allow_empty=allow_empty)


def write_selection(path, selected, *, title='', allow_empty=False):
    selected = validate_selection(selected, allow_empty=allow_empty)
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError('选择清单不能写入符号链接。')
    data = {'schema_version': 1, 'kind': 'project_test_selection', 'title': title,
            'selected_tests': selected,
            'notice': '仅记录测试目标，不包含源码、环境或执行结论；使用时需在当前项目中重新发现并核对。'}
    content = ('\n'.join(selected) + '\n' if path.suffix.lower() == '.txt'
               else json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    if len(content.encode('utf-8')) > MAX_SELECTION_BYTES:
        raise ValueError('选择清单超过 8 MiB，请减少选择后导出。')
    atomic_write(path, content)
