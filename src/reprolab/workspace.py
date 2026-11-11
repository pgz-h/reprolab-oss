"""Freeze an uncommitted working tree without staging, checkout, or installation."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from .case import load_case, parse_test_target
from .git_source import _git, export_revision, resolve_commit
from .recipes import repository_root
from .runner import EXCLUDED, MAX_BYTES, MAX_ENTRIES, fingerprint_manifest, snapshot, source_fingerprint, source_mode_fingerprint
from .runtime import choose_runtime

WORKTREE = '@working-tree'


def workspace_manifest(repository, *, control=None):
    """Git-visible regular files, including untracked and excluding ignored files."""
    root = Path(repository)
    gitlinks = {record.split(b"\t", 1)[1] for record in _git(root, "ls-files", "--stage", "-z").split(b"\0")
                if record.startswith(b"160000 ")}
    entries = set()
    total = 0
    for raw in _git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0'):
        if control:
            control.check()
        if not raw:
            continue
        name = raw.decode('utf-8')
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('工作区包含不支持的路径。')
        if any(part in EXCLUDED or part.startswith('.env') for part in relative.parts):
            continue
        path = root.joinpath(*relative.parts)
        for part in (path, *path.parents):
            if part == root:
                break
            if part.is_symlink():
                raise ValueError(f'工作区不支持符号链接：{relative}')
        if not path.exists():  # A tracked deletion is part of the current tree.
            continue
        if path.is_dir() and raw not in gitlinks:
            continue  # A cached blob replaced by a directory; current children are listed separately.
        if not path.is_file():
            raise ValueError(f'工作区不支持子模块或特殊条目：{relative}')
        total += path.stat().st_size
        entries.add(Path(relative))
        entries.update(parent for parent in Path(relative).parents if parent != Path('.'))
        if total > MAX_BYTES or len(entries) > MAX_ENTRIES:
            raise ValueError('工作区超过5000个条目或100 MiB限制。')
    ordered = sorted(entries)
    manifest = [[p.as_posix(), 'directory'] if (root / p).is_dir() else
                [p.as_posix(), 'file', hashlib.sha256((root / p).read_bytes()).hexdigest()] for p in ordered]
    return ordered, fingerprint_manifest(manifest)


def copy_workspace(repository, destination, *, control=None):
    root = repository_root(repository)
    entries, original = workspace_manifest(root, control=control)
    modes = source_mode_fingerprint(root, entries)
    copied = snapshot(root, Path(destination), entries, control=control)
    current_entries, current = workspace_manifest(root, control=control)
    if (copied != original or current != original
            or source_mode_fingerprint(Path(destination)) != modes
            or source_mode_fingerprint(root, current_entries) != modes):
        raise ValueError('当前工作区在准备期间发生变化，请停止编辑后重新保存。')
    return {'sha256': copied, 'file_count': sum((Path(destination) / p).is_file() for p in entries)}


def publish_snapshot_case(path, data_root, config):
    """Publish caller-owned prepared inputs as one new portable case."""
    original = Path(path).expanduser()
    if original.exists() or original.is_symlink():
        raise ValueError(f'案例文件已经存在，未覆盖：{original}')
    path = original.resolve()
    assets = path.with_name(path.stem + '.inputs')
    if assets.exists() or assets.is_symlink():
        raise ValueError(f'快照目录已经存在，未覆盖：{assets}')
    data_root = Path(data_root)
    config = dict(config, before='inputs/before', after='inputs/after', test='inputs/regression.py')
    temporary_case = data_root.parent / 'case.json'
    temporary_case.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    load_case(temporary_case)
    config.update(before=assets.name + '/before', after=assets.name + '/after', test=assets.name + '/regression.py')
    assets.mkdir()
    owned_case = False
    try:
        for child in data_root.iterdir():
            shutil.move(str(child), str(assets / child.name))
        with path.open('x', encoding='utf-8') as stream:
            owned_case = True
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if owned_case:
            path.unlink(missing_ok=True)
        shutil.rmtree(assets)
        raise
    return path


def create_workspace_case(path, repository, before_ref, test_target, *, failure_type,
                          failure_message, title=None, python_executable=None):
    """Create a portable snapshot recipe; later edits to the repository are irrelevant."""
    original = Path(path).expanduser()
    if original.exists() or original.is_symlink():
        raise ValueError(f'案例文件已经存在，未覆盖：{original}')
    path = original.resolve()
    root = repository_root(repository)
    if path.is_relative_to(root):
        raise ValueError('工作区案例及快照必须保存在被测仓库之外。')
    assets = path.with_name(path.stem + '.inputs')
    if assets.exists() or assets.is_symlink():
        raise ValueError(f'快照目录已经存在，未覆盖：{assets}')
    if not isinstance(failure_type, str) or not re.fullmatch(r'[A-Za-z_]\w*', failure_type):
        raise ValueError('预期异常类型无效。')
    if not isinstance(failure_message, str) or not failure_message.strip():
        raise ValueError('预期故障消息不能为空。')
    target_file = parse_test_target(test_target)
    before_sha = resolve_commit(root, before_ref)
    runtime = choose_runtime(python_executable)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reprolab-create-', dir=path.parent) as temp:
        staging = Path(temp)
        data_root = staging / 'inputs'
        data_root.mkdir()
        export_revision(root, before_sha, data_root / 'before')
        current = copy_workspace(root, data_root / 'after')
        selected = data_root / 'after' / target_file
        if not selected.is_file():
            raise ValueError('当前工作区没有这个可快照的测试文件；请检查目标、Git忽略规则及排除目录。')
        shutil.copyfile(selected, data_root / 'regression.py')
        config = {'source': 'snapshot', 'title': title or f'{root.name} 当前修改验收',
                  'description': '比较固定Git基线与保存时的当前工作区快照；之后修改原仓库不会改变本案例。Git忽略文件、敏感目录和未跟踪的空目录不参与快照。',
                  'before': 'inputs/before', 'after': 'inputs/after', 'test': 'inputs/regression.py',
                  'test_target': test_target, 'python': runtime['executable'],
                  'expected_failure': {'type': failure_type, 'message_contains': failure_message},
                  'workspace_origin': {'repository': root.name, 'before_ref': before_ref,
                                       'before_commit': before_sha, 'before_sha256': source_fingerprint(data_root / 'before')[1],
                                       'after_sha256': current['sha256'],
                                       'source_mode_sha256': {label: source_mode_fingerprint(data_root / label) for label in ('before', 'after')},
                                       'test_sha256': hashlib.sha256((data_root / 'regression.py').read_bytes()).hexdigest(),
                                       'file_count': current['file_count']}}
        publish_snapshot_case(path, data_root, config)
    return {'case': str(path), 'before_commit': before_sha, 'workspace_origin': config['workspace_origin'],
            'test_target': test_target, 'runtime': runtime,
            'notice': '当前修改已冻结为独立案例；没有运行测试、修改Git仓库或发送代码。'}
