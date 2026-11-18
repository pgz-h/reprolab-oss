"""Explicit repository-relative allowlist for a bounded snapshot."""
from pathlib import PurePosixPath

from .project_collection import normalize_collection_path


def normalize_source_scope(values):
    if not values:
        return None
    if not isinstance(values, (tuple, list)) or len(values) > 200:
        raise ValueError('源码范围须为最多 200 个仓库内文件或目录。')
    paths = sorted({normalize_collection_path(value) for value in values})
    if not all(paths):
        raise ValueError('源码范围须指定非空的仓库相对路径。')
    return paths


def in_source_scope(path, scope):
    return scope is None or any(path.is_relative_to(PurePosixPath(root)) for root in scope)
