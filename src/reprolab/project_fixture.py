"""Freeze explicit fixture files for a pytest diagnostic replay."""
from pathlib import PurePosixPath
import re

from .case import parse_test_target
from .project_collection import normalize_collection_path


_CONFIG = {'pytest.ini', '.pytest.ini', 'pytest.toml', '.pytest.toml', 'pyproject.toml', 'tox.ini', 'setup.cfg'}


def changed_conftests(snapshots, selected, digest):
    changes = []
    files = {parent / 'conftest.py' for identity in selected
             for parent in PurePosixPath(parse_test_target(identity)).parents}
    for file in sorted(files):
        paths = [snapshots / side / file for side in ('before', 'after')]
        if any(path.is_file() for path in paths):
            before, after = (digest(path) if path.is_file() else None for path in paths)
            if before != after:
                changes.append({'path': file.as_posix(), 'before_sha256': before, 'after_sha256': after})
    return changes


def test_uses_changed_conftest(identity, changes):
    parents = PurePosixPath(parse_test_target(identity)).parents
    return any(PurePosixPath(item['path']).parent in parents for item in changes)


def fixture_record(snapshots, paths, source_digest, selected, digest):
    if (not isinstance(source_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', source_digest)
            or not isinstance(paths, list) or not 1 <= len(paths) <= 100):
        raise ValueError('夹具对照需要有效来源和 1–100 个基线文件。')
    targets = {parse_test_target(identity) for identity in selected}
    entries = []
    for name in sorted(set(normalize_collection_path(path) for path in paths)):
        relative = PurePosixPath(name)
        before, after = (snapshots / side / relative for side in ('before', 'after'))
        if (not name or name in targets or relative.name in _CONFIG or not before.is_file()
                or after.is_dir() or any((snapshots / 'after' / parent).is_file() for parent in relative.parents)):
            raise ValueError('请选择真实基线夹具或辅助文件，不能替换所选测试或 pytest 配置：' + name)
        entries.append({'path': name, 'before_sha256': digest(before),
                        'after_sha256': digest(after) if after.is_file() else None})
    return {'source_report_sha256': source_digest, 'files': entries}


def check_fixture_record(snapshots, record, selected, digest):
    paths = [item['path'] for item in record['files']]
    if record != fixture_record(snapshots, paths, record['source_report_sha256'], selected, digest):
        raise ValueError('夹具对照与固定源文件不一致。')
