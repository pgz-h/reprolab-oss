"""Locate added or edited test definitions in the two frozen source trees."""
import ast
from collections import defaultdict

from .case import parse_test_target


def _definitions(path, wanted):
    """Return a syntax signature and candidate line for each unambiguous definition."""
    try:
        with path.open('rb') as stream:
            source = stream.read(512 * 1024 + 1)
        if len(source) > 512 * 1024:
            return None
        tree = ast.parse(source)
        definitions = {}
        class_names = set()
        ambiguous_classes = set()
        pending = [(node, '', True) for node in tree.body]
        while pending:
            node, prefix, unconditional = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                if name in wanted:
                    definitions[name] = ((ast.dump(node), node.lineno)
                                         if unconditional and name not in definitions else None)
            elif isinstance(node, ast.ClassDef):
                class_prefix = prefix + node.name + '::'
                if class_prefix in class_names:
                    ambiguous_classes.add(class_prefix)
                class_names.add(class_prefix)
                pending.extend((child, class_prefix, unconditional) for child in node.body)
            elif isinstance(node, (ast.stmt, ast.excepthandler, ast.match_case)):
                # Conditional/alternative definitions can shadow each other; they provide
                # evidence of a name but cannot identify the function collected at runtime.
                pending.extend((child, prefix, False) for child in ast.iter_child_nodes(node))
        if ambiguous_classes:
            for name in wanted:
                # A later definition of the same class can expose entirely
                # different methods, including methods inherited from a base.
                parts = name.split('::')
                if any('::'.join(parts[:end]) + '::' in ambiguous_classes for end in range(1, len(parts))):
                    definitions[name] = None
        return definitions
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None


def changed_test_definitions(changes, tests, before, after, *, control=None):
    """Describe source definitions only; broader file/fixture hints remain in recommendations."""
    kinds = {row['path']: row['kind'] for row in changes}
    by_file = defaultdict(list)
    for identity in tests:
        file = parse_test_target(identity)
        if file in kinds:
            name = identity.partition('::')[2].partition('[')[0]
            by_file[file].append((identity, name))
    result = {}
    for file, targets in by_file.items():
        if control:
            control.check()
        wanted = {name for _, name in targets}
        candidate = _definitions(after / file, wanted)
        baseline = {} if kinds[file] == 'added' else _definitions(before / file, wanted)
        if candidate is None or baseline is None:
            continue
        for identity, name in targets:
            definition = candidate.get(name)
            if definition is None:
                continue
            status = 'added'
            if name in baseline:
                old = baseline[name]
                if old is None or old[0] == definition[0]:
                    continue
                status = 'modified'
            result[identity] = {'status': status, 'path': file, 'line': definition[1]}
    return result
