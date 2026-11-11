"""Bounded, lazy resolution of explicitly selected static dependency groups."""
import re

_MAX_GROUPS = 128
_MAX_EXPANSION = 4000


def _name(value):
    if (not isinstance(value, str) or len(value) > 128
            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?', value)):
        raise ValueError('依赖组名称无效。')
    return re.sub(r'[-_.]+', '-', value).lower()


def resolve_dependency_groups(data, selected=None):
    """Resolve selected extras/groups only; requirement validation stays with the caller."""
    selected = [] if selected is None else selected
    if not isinstance(selected, (list, tuple)) or len(selected) > _MAX_GROUPS:
        raise ValueError('请选择最多 128 个依赖组。')
    requested = []
    for value in selected:
        if not isinstance(value, str) or ':' not in value:
            raise ValueError('依赖组应使用 group:名称 或 extra:名称。')
        kind, name = value.split(':', 1)
        if kind not in ('group', 'extra'):
            raise ValueError('依赖组应使用 group:名称 或 extra:名称。')
        identity = kind + ':' + _name(name)
        if identity not in requested:
            requested.append(identity)
    groups, available, issues = {}, [], []
    project = data.get('project', {})
    if not isinstance(project, dict):
        raise ValueError('project 必须为 TOML 表。')
    for kind, table in (('group', data.get('dependency-groups', {})),
                        ('extra', project.get('optional-dependencies', {}))):
        if not isinstance(table, dict) or len(table) > _MAX_GROUPS:
            issues.append('依赖组声明无效或超过 128 组，请检查 pyproject.toml。')
            continue
        for name, values in table.items():
            try:
                normalized = _name(name)
            except ValueError:
                issues.append('依赖组包含无效名称，请检查 pyproject.toml。')
                continue
            identity = kind + ':' + normalized
            if identity in groups:
                issues.append('存在规范化后重名的依赖组：' + identity)
                continue
            groups[identity] = values
            available.append({'id': identity, 'name': name, 'kind': kind,
                              'count': len(values) if isinstance(values, list) else None,
                              'recommended': normalized in ('test', 'tests', 'testing')})
    expanded, steps = [], 0

    def expand(identity, ancestors):
        nonlocal steps
        if identity in ancestors:
            raise ValueError('依赖组存在循环引用：' + identity)
        if len(ancestors) >= 32:
            raise ValueError('依赖组引用超过 32 层。')
        if identity not in groups:
            raise ValueError('依赖组不存在：' + identity)
        values = groups[identity]
        if not isinstance(values, list):
            raise ValueError('依赖组需要列表：' + identity)
        for value in values:
            steps += 1
            if steps > _MAX_EXPANSION:
                raise ValueError('依赖组展开超过 4000 个条目，请缩小选择。')
            if isinstance(value, str):
                expanded.append(value)
            elif (identity.startswith('group:') and isinstance(value, dict)
                  and set(value) == {'include-group'}):
                expand('group:' + _name(value['include-group']), (*ancestors, identity))
            else:
                raise ValueError('所选依赖组包含暂不支持的条目：' + identity)

    for identity in requested:
        start = len(expanded)
        try:
            expand(identity, ())
        except ValueError as exc:
            del expanded[start:]
            issues.append(str(exc))
    for identity in requested:
        if identity not in groups:
            kind, name = identity.split(':', 1)
            available.append({'id': identity, 'name': name, 'kind': kind,
                              'count': None, 'recommended': False, 'missing': True})
    return {'available': available, 'selected': requested, 'requirements': expanded,
            'issues': list(dict.fromkeys(issues))}
