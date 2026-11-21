"""Resolve an explicit GitHub PR event to verified local Git commits."""
import hashlib
import json
from pathlib import Path
import re

from .git_source import _git, resolve_commit
from .recipes import repository_root


COMPARISON_LABELS = {
    'direct': '指定版本对比',
    'pr_head': 'PR 改动检查（共同基线 → PR head）',
    'pr_merge': 'PR 合入检查（目标提交 → 已核对的合并结果）',
}


def comparison_type(report):
    context = report.get('pr_context') or {}
    return {'head': 'pr_head', 'merge': 'pr_merge'}.get(context.get('mode'), 'direct')


class PRComparisonBlocked(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _require_commit(root, sha, reason, message, *, local_only):
    try:
        if _sha(sha) and resolve_commit(root, sha, local_only=local_only) == sha:
            return
    except ValueError:
        pass
    raise PRComparisonBlocked(reason, message)


def _sha(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value))


def github_pr_context(repository, location, mode, *, merge_commit=None, local_only=False):
    if mode not in ('head', 'merge'):
        raise ValueError('GitHub PR 事件须明确选择 head 或 merge。')
    path = Path(location).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('请选择实际保存、大小不超过 2 MiB 的 GitHub PR 事件。')
    with path.open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('GitHub PR 事件超过 2 MiB。')
    event = json.loads(raw)
    pull = event.get('pull_request') if isinstance(event, dict) else None
    if not isinstance(pull, dict):
        raise ValueError('事件不包含 pull_request。')
    try:
        base, head = pull['base']['sha'], pull['head']['sha']
    except (KeyError, TypeError):
        raise ValueError('PR 事件缺少目标或候选的完整 SHA。') from None
    if not (_sha(base) and _sha(head)):
        raise ValueError('PR 事件缺少有效完整 SHA。')
    root = Path(repository).expanduser().resolve() if local_only else repository_root(repository)
    for sha in (base, head):
        _require_commit(root, sha, 'pr_parent_missing', '本地 Git 中缺少事件绑定的目标或 PR head 提交。',
                        local_only=local_only)
    if mode == 'head':
        if merge_commit is not None:
            raise ValueError('head 模式无需提供合并结果。')
        if local_only and _git(root, 'rev-parse', '--is-shallow-repository', local_only=True).strip() != b'false':
            raise PRComparisonBlocked('pr_history_shallow', '需要完整的本地提交历史才能确认唯一的 PR 共同基线。')
        bases = _git(root, 'merge-base', '--all', base, head, local_only=local_only).decode().split()
        if len(bases) != 1 or not _sha(bases[0]):
            raise ValueError('无法为 PR 唯一确定共同基线。')
        before, after = bases[0], head
        if local_only:
            _require_commit(root, before, 'pr_merge_base_missing', '本地 Git 缺少共同基线提交。',
                            local_only=True)
    else:
        after = merge_commit or pull.get('merge_commit_sha')
        _require_commit(root, after, 'pr_merge_missing', '本地 Git 中缺少事件对应的合并结果提交。',
                        local_only=local_only)
        parents = _git(root, 'rev-list', '--parents', '-n', '1', after, local_only=local_only).decode().split()
        if parents != [after, base, head]:
            raise PRComparisonBlocked('pr_merge_parent_mismatch', '指定合并结果的父提交与 PR 目标及 head 不一致。')
        before = base
    return {'mode': mode, 'event_sha256': hashlib.sha256(raw).hexdigest(), 'base': base, 'head': head,
            'before': before, 'after': after}


def validate_pr_context(value, before, after):
    if value is None:
        return
    if (not isinstance(value, dict) or value.keys() != {'mode', 'event_sha256', 'base', 'head', 'before', 'after'}
            or value['mode'] not in ('head', 'merge')
            or not all(_sha(value[key]) for key in ('base', 'head', 'before', 'after'))
            or not isinstance(value['event_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['event_sha256'])
            or value['before'] != before or value['after'] != after
            or value['mode'] == 'head' and value['after'] != value['head']
            or value['mode'] == 'merge' and value['before'] != value['base']):
        raise ValueError('保存的 PR 上下文与本次固定提交不一致。')
