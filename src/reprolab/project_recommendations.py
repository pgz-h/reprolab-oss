"""Static, bounded impact suggestions over frozen pytest project files."""
import ast
from collections import defaultdict, deque
from importlib.util import resolve_name
from pathlib import PurePosixPath
import symtable

from .case import parse_test_target


_MAX_SOURCE_BYTES = 512 * 1024
_MAX_IMPORT_HOPS = 8


def _module_address(path):
    parts = path.with_suffix('').parts
    root = 'src' if len(parts) > 1 and parts[0] == 'src' else ''
    if root:
        parts = parts[1:]
    return root, parts[:-1] if parts[-1] == '__init__' else parts


def _import_names(node, package):
    if isinstance(node, ast.Import):
        return [(alias.asname or alias.name.split('.')[0], (alias.name,)) for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    module = node.module or ''
    if node.level:
        try:
            module = resolve_name('.' * node.level + module, package)
        except ImportError:
            return []
    return [(alias.asname or alias.name, (module,) if alias.name == '*' else (module, module + '.' + alias.name))
            for alias in node.names]


def _body_nodes(function):
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield node
            stack.extend(ast.iter_child_nodes(node))


class _SnapshotModules:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.files = defaultdict(list)
        self.resolved = {}
        for path in snapshot.rglob('*.py'):
            relative = PurePosixPath(path.relative_to(snapshot).as_posix())
            root, parts = _module_address(relative)
            if parts and all(part.isidentifier() for part in parts) and path.is_file():
                self.files[parts].append((root, relative))
        self.max_depth = max(map(len, self.files), default=0)

    def resolve(self, name):
        """Resolve modules and implicit package initializers without importing them."""
        if name in self.resolved:
            return self.resolved[name]
        parts = tuple(name.split('.'))
        paths = []
        package_root = None  # Namespace portions may span the two roots until a regular package is found.
        for depth in range(1, min(len(parts), self.max_depth) + 1):
            candidates = [(root, path) for root, path in self.files.get(parts[:depth], ())
                          if package_root is None or package_root == root]
            if len(candidates) > 1:
                break
            if not candidates:
                continue
            root, path = candidates[0]
            if depth < len(parts) and path.name != '__init__.py':
                break
            paths.append(path)
            package_root = root
        self.resolved[name] = paths
        return paths

    def _source(self, name):
        path = self.snapshot / name
        try:
            if path.stat().st_size > _MAX_SOURCE_BYTES:
                return None
            with path.open('rb') as source:
                content = source.read(_MAX_SOURCE_BYTES + 1)
            return (content, ast.parse(content)) if len(content) <= _MAX_SOURCE_BYTES else None
        except (OSError, SyntaxError, ValueError, RecursionError):
            return None

    def _package(self, name):
        _, parts = _module_address(name)
        return '.'.join(parts if name.name == '__init__.py' else parts[:-1])

    def _links(self, statements, source):
        imports = {}
        for line, names in statements:
            for target in {target for qualified in names for target in self.resolve(qualified)} - {source}:
                imports[target] = min(line, imports.get(target, line))
        return imports

    def imports(self, name):
        saved = self._source(name)
        if saved is None:
            return {}
        package = self._package(name)
        return self._links(((node.lineno, names) for node in ast.walk(saved[1])
                            for _, names in _import_names(node, package)), name)

    def fixture_imports(self, definitions):
        by_path = defaultdict(list)
        for index, fixture in enumerate(definitions):
            by_path[PurePosixPath(fixture['path'])].append((index, fixture['line']))
        links = {}
        for path, fixtures in by_path.items():
            saved = self._source(path)
            if saved is None:
                continue
            content, tree = saved
            try:
                module = symtable.symtable(content, path.as_posix(), 'exec')
            except (SyntaxError, ValueError, RecursionError):
                continue
            package = self._package(path)
            aliases = defaultdict(list)
            for node in tree.body:
                for alias, names in _import_names(node, package):
                    aliases[alias].append((node.lineno, names))
            # A top-level fixture or a class fixture has no enclosing function variables.
            functions = {node.lineno: node for node in ast.walk(tree)
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            scopes = list(module.get_children())
            by_line = {}
            while scopes:
                scope = scopes.pop()
                if scope.get_type() == 'class':
                    scopes.extend(scope.get_children())
                elif scope.get_type() == 'function' and (node := functions.get(scope.get_lineno())) is not None:
                    first_line = min((decorator.lineno for decorator in node.decorator_list), default=node.lineno)
                    by_line[first_line] = (node, scope)
            for index, line in fixtures:
                if line not in by_line:
                    continue
                node, scope = by_line[line]
                statements = [(inner.lineno, names) for inner in _body_nodes(node)
                              for _, names in _import_names(inner, package)]
                for symbol in scope.get_symbols():
                    alias = symbol.get_name()
                    if (symbol.is_global() and symbol.is_referenced() and not symbol.is_assigned()
                            and len(aliases.get(alias, ())) == 1):
                        declaration = module.lookup(alias)
                        if not declaration.is_assigned() and not declaration.is_namespace():
                            statements.extend(aliases[alias])
                if linked := self._links(statements, path):
                    links[index] = linked
        return links


def _chain(path, steps):
    chain = []
    while path in steps:
        target, line = steps[path]
        chain.append(f'{path.as_posix()}:{line}')
        path = target
    return [*chain, path.as_posix()]


def _import_reasons(snapshot, tested, changed, *, control, definitions=()):
    if not changed or not (tested or definitions):
        return {}, {}
    module_index = _SnapshotModules(snapshot)
    fixture_links = module_index.fixture_imports(definitions)
    fixture_targets = {target for links in fixture_links.values() for target in links}
    visited = tested | (fixture_targets - changed)
    waiting = deque(sorted(visited))
    importers = defaultdict(list)
    reasons = {}
    while waiting:
        if control:
            control.check()
        name = waiting.popleft()
        links = module_index.imports(name)
        if name in tested and (direct := changed & links.keys()):
            modules = sorted({'.'.join(_module_address(path)[1]) for path in direct})
            reasons[name] = '直接静态导入关联到改动模块 · ' + '、'.join(modules[:3])
        for target, line in links.items():
            importers[target].append((name, line))
            if target not in visited and target not in changed:
                visited.add(target)
                waiting.append(target)

    # One reverse walk finds the shortest witness for every reachable test, even through cycles.
    affected = sorted(changed & (importers.keys() | fixture_targets))
    waiting = deque((name, 0) for name in affected)
    visited = set(affected)
    steps = {}
    while waiting:
        target, depth = waiting.popleft()
        if depth == _MAX_IMPORT_HOPS:
            continue
        for name, line in sorted(importers.get(target, ())):
            if name in visited:
                continue
            visited.add(name)
            steps[name] = (target, line)
            waiting.append((name, depth + 1))
    for name in (tested - reasons.keys()) & steps.keys():
        reasons[name] = '间接静态导入推断 · ' + ' → '.join(_chain(name, steps))
    fixture_reasons = {}
    for index, links in fixture_links.items():
        paths = [(_chain(target, steps), line) for target, line in links.items() if target in visited]
        paths = [(path, line) for path, line in paths if len(path) <= _MAX_IMPORT_HOPS]
        if paths:
            fixture = definitions[index]
            chain, line = min(paths, key=lambda pair: (len(pair[0]), pair[0], pair[1]))
            origin = f'{fixture["name"]}（{fixture["path"]}:{fixture["line"]}）'
            trace = ' → '.join([f'{fixture["path"]}:{line}', *chain])
            fixture_reasons[index] = f'收集绑定的 fixture 影响推断 · {origin} → {trace}'
    return reasons, fixture_reasons


def recommend_project_tests(changed_files, tests, before, after, *, control=None, fixture_bindings=None):
    """Return local change hints without modifying the discovered or selected scope."""
    changed = {PurePosixPath(item['path']) for item in changed_files}
    fixtures = {path.parent for path in changed if path.name == 'conftest.py'}
    test_files = {identity: PurePosixPath(parse_test_target(identity)) for identity in tests}
    reasons = {}
    for path in set(test_files.values()):
        if path in changed:
            reasons[path] = '本次修改了这个测试文件'
        elif any(parent in fixtures for parent in path.parents):
            reasons[path] = '该目录的 fixture 或测试配置发生变化'
    modules = {path for path in changed if path.suffix == '.py'}
    bindings = fixture_bindings or {}
    after_reasons, fixture_reasons = _import_reasons(after, set(test_files.values()) - reasons.keys(), modules,
                                                     control=control, definitions=bindings.get('definitions', ()))
    reasons.update(after_reasons)
    result = {}
    for identity, path in test_files.items():
        reason = reasons.get(path) or next((fixture_reasons[index] for index in bindings.get('tests', {}).get(identity, ())
                                           if index in fixture_reasons), None)
        if reason:
            result[identity] = reason
    before_reasons, _ = _import_reasons(before, {path for identity, path in test_files.items() if identity not in result},
                                       modules, control=control)
    return {identity: result[identity] if identity in result else before_reasons[path] + '（基线快照）'
            for identity, path in test_files.items() if identity in result or path in before_reasons}
