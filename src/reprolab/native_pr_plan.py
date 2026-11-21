"""Bind a target-branch native test policy to a locally verified PR event."""
import hashlib
import json
from pathlib import Path

from .git_source import _git
from .native_project import _definitions, _parse_plan_json, _validate_review_plan, review_native_plan
from .persistence import atomic_write
from .project_pr_context import github_pr_context


_POLICY_LIMIT = 2 * 1024 * 1024


def _resolve_native_pr_plan(repository, event, mode, policy_path, *, merge_commit):
    path, = _definitions([policy_path])
    repo = Path(repository).expanduser().resolve()
    context = github_pr_context(repo, event, mode, merge_commit=merge_commit, local_only=True)
    listing = _git(repo, 'ls-tree', '-lz', '--full-tree', context['base'], '--',
                   ':(literal)' + path, local_only=True)
    entries = listing.rstrip(b'\0').split(b'\0') if listing else []
    if len(entries) != 1:
        raise ValueError('PR 目标提交中缺少指定的原生测试策略文件。')
    attributes, name = entries[0].split(b'\t', 1)
    mode_bits, kind, oid, size = attributes.split()
    if (name != path.encode('utf-8') or kind != b'blob' or mode_bits not in (b'100644', b'100755')
            or not size.isdigit() or int(size) > _POLICY_LIMIT):
        raise ValueError('PR 目标策略须为不超过 2 MiB 的普通 Git 文件。')
    policy = _git(repo, 'cat-file', 'blob', oid.decode('ascii'), local_only=True)
    if len(policy) != int(size):
        raise ValueError('PR 目标策略文件内容不完整。')
    fields = _parse_plan_json(policy)
    if not isinstance(fields, dict) or fields.keys() & {'commits', 'pr_context'}:
        raise ValueError('原生 PR 策略由目标提交提供测试范围，提交与 PR 上下文由事件确定。')
    plan = _validate_review_plan({
        **fields,
        'commits': {side: context[side] for side in ('before', 'after')},
        'pr_context': {key: context[key] for key in ('mode', 'base', 'head')},
    })
    raw = (json.dumps(plan, ensure_ascii=True, indent=2, sort_keys=True) + '\n').encode()
    return plan, raw, {
        'plan_sha256': hashlib.sha256(raw).hexdigest(),
        'policy': {'commit': context['base'], 'path': path, 'sha256': hashlib.sha256(policy).hexdigest()},
        'pr_context': context,
    }


def plan_native_pr(repository, event, mode, policy_path, output, *, merge_commit=None):
    _, raw, receipt = _resolve_native_pr_plan(repository, event, mode, policy_path, merge_commit=merge_commit)
    destination = Path(output).expanduser().absolute()
    atomic_write(destination, raw, replace=False)
    return {'status': 'planned', 'plan_file': str(destination), **receipt}


def review_native_pr(location, repository, event, mode, policy_path, *, merge_commit=None, control):
    evidence = Path(location).expanduser().resolve()
    if any(Path(path).expanduser().resolve().is_relative_to(evidence) for path in (repository, event)):
        raise ValueError('Git 仓库和 PR 事件须位于证据目录或 ZIP 之外，由接收方提供。')
    plan, _, receipt = _resolve_native_pr_plan(repository, event, mode, policy_path, merge_commit=merge_commit)
    return review_native_plan(location, plan, receipt['plan_sha256'], git_repository=repository, control=control)
