"""Bounded pytest collection observations. Fixture functions are never called here."""
from pathlib import Path, PurePosixPath


MAX_DEFINITIONS = 4000
MAX_REFERENCES = 100000
MAX_PER_TEST = 64
SCOPES = ('function', 'class', 'module', 'package', 'session')


def _location(definition, source):
    code = getattr(definition.func, '__code__', None)
    if code is None or not Path(code.co_filename).is_absolute():
        return None
    try:
        path = Path(code.co_filename).resolve()
        relative = path.relative_to(source).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    if (path.suffix == '.py' and len(relative) <= 2000 and path.is_file()
            and definition.argname.isidentifier() and len(definition.argname) <= 128
            and definition.scope in SCOPES):
        return {'name': definition.argname, 'path': relative, 'line': code.co_firstlineno, 'scope': definition.scope}
    return None


def _definitions(item):
    info = getattr(item, '_fixtureinfo', None)
    if info is None:
        return [], True
    waiting = [(name, ()) for name in reversed(info.initialnames)]
    seen = set()
    result = []
    partial = False
    while waiting:
        name, ancestry = waiting.pop()
        if name == 'request':
            continue
        offset = 1 + ancestry.count(name)
        candidates = info.name2fixturedefs.get(name, ())
        if offset > len(candidates):
            partial = True
            continue
        if (name, offset) in seen:
            continue
        if len(seen) == MAX_PER_TEST:
            return result, True
        seen.add((name, offset))
        definition = candidates[-offset]
        result.append(definition)
        waiting.extend((dependency, (*ancestry, name)) for dependency in reversed(definition.argnames))
    return result, partial


def collect_fixture_bindings(items, source):
    definitions, tests, indices = [], {}, {}
    locations = {}
    references = 0
    partial = len(items) > 10000
    for item in items[:10000]:
        if references >= MAX_REFERENCES:
            partial = True
            break
        try:
            chosen, incomplete = _definitions(item)
            partial |= incomplete
            row = []
            for definition in chosen:
                if definition not in locations:
                    locations[definition] = _location(definition, source)
                location = locations[definition]
                if location is None:
                    continue
                key = tuple(location.values())
                if key not in indices:
                    if len(definitions) >= MAX_DEFINITIONS:
                        partial = True
                        continue
                    indices[key] = len(definitions)
                    definitions.append(location)
                row.append(indices[key])
            row = list(dict.fromkeys(row))
        except (AttributeError, OSError, TypeError, ValueError):
            # A runner or plugin without compatible collection metadata can still collect tests.
            partial = True
            continue
        if references + len(row) > MAX_REFERENCES:
            partial = True
            break
        if row:
            tests[item.nodeid] = row
            references += len(row)
    return {'schema_version': 1, 'status': 'partial' if partial else 'recorded',
            'definitions': definitions, 'tests': tests}


def checked_fixture_bindings(value, tests, snapshot):
    """Validate optional observations before using any location as a source path."""
    if not isinstance(value, dict) or value.keys() != {'schema_version', 'status', 'definitions', 'tests'}:
        return None
    definitions, by_test = value['definitions'], value['tests']
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['status'] not in ('recorded', 'partial')
            or not isinstance(definitions, list) or len(definitions) > MAX_DEFINITIONS
            or not isinstance(by_test, dict) or not by_test.keys() <= set(tests)):
        return None
    for item in definitions:
        if (not isinstance(item, dict) or item.keys() != {'name', 'path', 'line', 'scope'}
                or not isinstance(item['name'], str) or len(item['name']) > 128 or not item['name'].isidentifier()
                or not isinstance(item['path'], str) or len(item['path']) > 2000
                or any(char in ':\\' or ord(char) < 32 for char in item['path'])
                or type(item['line']) is not int or item['line'] < 1 or item['scope'] not in SCOPES):
            return None
        relative = PurePosixPath(item['path'])
        if (relative.is_absolute() or '..' in relative.parts or relative.suffix != '.py'
                or not (snapshot / relative).is_file()):
            return None
    references = 0
    for indices in by_test.values():
        if (not isinstance(indices, list) or len(indices) > MAX_PER_TEST
                or any(type(index) is not int or not 0 <= index < len(definitions) for index in indices)):
            return None
        references += len(indices)
        if references > MAX_REFERENCES:
            return None
    return value
