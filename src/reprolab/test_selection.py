"""Rank existing tests by local file changes without importing the project."""
from pathlib import PurePosixPath

from .git_source import _git, resolve_commit
from .runner import EXCLUDED
from .workspace import WORKTREE


def recommend_tests(repository, before_ref, after_ref, tests):
    before = resolve_commit(repository, before_ref)
    if after_ref == WORKTREE:
        raw = _git(repository, 'diff', '--name-only', '-z', before, '--')
        raw += _git(repository, 'ls-files', '--others', '--exclude-standard', '-z')
    else:
        after = resolve_commit(repository, after_ref)
        raw = _git(repository, 'diff', '--name-only', '-z', before, after, '--')
    changed = set()
    for name in raw.split(b'\0'):
        if not name:
            continue
        path = PurePosixPath(name.decode('utf-8'))
        if not any(part in EXCLUDED or part.startswith('.env') for part in path.parts):
            changed.add(path)
    modules = set()
    fixture_dirs = set()
    for path in changed:
        if path.name == 'conftest.py':
            fixture_dirs.add(path.parent)
        elif path.suffix == '.py' and not path.name.startswith('test_') and not path.name.endswith('_test.py'):
            modules.add(path.parent.name if path.stem == '__init__' else path.stem)
    priorities, reasons = {}, {}
    for target in tests:
        path = PurePosixPath(target.split('::', 1)[0])
        stem = path.stem.removeprefix('test_').removesuffix('_test')
        if path in changed:
            priorities[target], reasons[target] = 100, '本次修改了这个测试文件'
        elif stem in modules:
            priorities[target], reasons[target] = 80, f'测试文件与修改模块 {stem} 对应'
        elif any(path.is_relative_to(directory) for directory in fixture_dirs):
            priorities[target], reasons[target] = 60, '本次修改了该目录的测试准备配置'
    return {'tests': sorted(tests, key=lambda target: (-priorities.get(target, 0), target)),
            'recommendations': reasons, 'changed_files': sorted(str(path) for path in changed),
            'notice': '按文件改动和命名关联排序，选择后可试运行旧版确认实际故障。'}
