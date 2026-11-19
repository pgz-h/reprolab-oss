"""Source-only identity for pytest assertion observations."""
import ast
import hashlib
import json


def assertions_by_scope(tree):
    scopes = {}
    pending = [(tree, tree, '<module>')]
    while pending:
        node, scope, name = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(scope, ast.Module):
                name = node.name
            else:
                separator = '.<locals>.' if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else '.'
                name += separator + node.name
            scope = node
        if isinstance(node, ast.Assert):
            scopes.setdefault((scope, name), []).append(node)
        pending.extend((child, scope, name) for child in ast.iter_child_nodes(node))
    return scopes


def assertion_site_digest(path, qualified_name, predicate):
    identity = json.dumps([path, qualified_name, predicate], ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(identity.encode()).hexdigest()
