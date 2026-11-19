"""Freeze a project and propose an explicit test selection from actual discovery."""
import hashlib
from pathlib import Path
import shlex
import sys

from .persistence import atomic_write, save_json
from .project_selections import MAX_SELECTED, write_selection


def changed_definition_reason(collection, identity):
    definitions = collection.get('changed_test_definitions')
    definition = definitions.get(identity) if isinstance(definitions, dict) else None
    if not isinstance(definition, dict):
        return ''
    path, line = definition.get('path'), definition.get('line')
    label = {'added': '新增测试定义', 'modified': '修改测试定义'}.get(definition.get('status'))
    return f'{label}（{path}:{line}）' if label and isinstance(path, str) and type(line) is int else ''


def project_command(*arguments):
    launcher = [sys.executable, '--cli'] if getattr(sys, 'frozen', False) else ['reprolab']
    args = [*launcher, 'project', *map(str, arguments)]
    if sys.platform == 'win32':
        return '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in args)
    return shlex.join(args)


def suggest_project(repository, before_ref, after_ref, output_root, *, python_executable=None, title=None,
                    issue='', collection_path='', snapshot_scope=None, pr_context=None, timeout=120, control=None):
    """Use the normal snapshot and collection path. The session starts with an explicit empty selection."""
    from .project_sessions import _prepare_project, _timeout, collect_project
    from .recipes import repository_root
    from .git_source import resolve_commit
    from .workspace import WORKTREE

    _timeout(timeout)
    root = repository_root(repository)
    output = Path(output_root).expanduser().resolve()
    before = resolve_commit(root, before_ref)
    after = WORKTREE if after_ref == WORKTREE else resolve_commit(root, after_ref)
    state = _prepare_project(
        root, before, after, output / 'sessions',
        python_executable=python_executable, title=title, issue=issue, collection_path=collection_path,
        snapshot_scope=snapshot_scope, pr_context=pr_context,
        control=control.child(progress=control.progress) if control else None,
        refresh_metadata={'selection_request': {
            'kind': 'project_selection_request', 'schema_version': 1, 'selected_tests': [],
        }},
    )
    session = Path(state['directory'])
    record = collect_project(session, timeout=timeout, control=control)
    tests = record['tests']
    reasons = record['recommendations']
    rows = []
    for identity in tests:
        definition = changed_definition_reason(record, identity)
        reason = reasons.get(identity)
        if definition or reason:
            rows.append({'test_id': identity, 'reason': reason or definition,
                         **({'definition': record['changed_test_definitions'][identity],
                             'definition_reason': definition} if definition else {})})
    selected = [row['test_id'] for row in rows]
    rows.sort(key=lambda row: 'definition' not in row)
    if record['status'] != 'collected':
        status = 'blocked'
    elif record['partial'] or record['truncated']:
        status = 'incomplete'
    elif len(selected) > MAX_SELECTED:
        status = 'too_many_recommendations'
    else:
        status = 'ready' if selected else 'no_recommendations'

    available = None
    if tests:
        available = session / 'discovered-tests.txt'
        if any(len(test.splitlines()) != 1 for test in tests):
            available = available.with_suffix('.json')
            save_json(available, tests)
        else:
            atomic_write(available, '\n'.join(tests) + '\n')
    selection = None
    if status == 'ready':
        extension = '.json' if any(len(test.splitlines()) != 1 for test in selected) else '.txt'
        selection = session / ('suggested-tests' + extension)
        write_selection(selection, selected, title='本次发现的推荐测试')
    evidence = Path(record['evidence_dir']) / 'collection.json'
    with evidence.open('rb') as stream:
        collection_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    run_command = None
    if selection:
        args = ('--timeout', str(timeout)) if timeout != 120 else ()
        run_command = project_command('run', session, '--selection-file', selection, '--output', output / 'reports', *args)
    result = {
        'kind': 'project_test_suggestion', 'schema_version': 1, 'status': status,
        'directory': str(session), 'project_id': state['project_id'],
        'before_commit': state['before_commit'], 'after_commit': state['after_commit'],
        'source_sha256': state['source_sha256'], 'snapshot_scope': state.get('snapshot_scope'),
        'collection': {name: record[name] for name in (
            'status', 'collection_path', 'count', 'partial', 'truncated', 'runtime', 'diagnosis',
        )},
        'collection_record': str(evidence), 'collection_sha256': collection_sha256,
        'collection_log': str(Path(record['evidence_dir']) / 'collection.log'),
        'discovered_count': len(tests), 'recommended_count': len(selected), 'recommendations': rows,
        'discovered_file': str(available) if available else None,
        'selection_file': str(selection) if selection else None,
        'suggestion_file': str(session / 'suggestion.json'),
        'run_command': run_command,
    }
    save_json(session / 'suggestion.json', result)
    return result


_EN_REASONS = {
    '本次修改了这个测试文件': 'This test file changed',
    '该目录的 fixture 或测试配置发生变化': 'A fixture or test configuration changed in this directory',
    '直接静态导入关联到改动模块': 'Direct static import of a changed module',
    '间接静态导入推断': 'Indirect static import chain to a changed module',
    '收集绑定的 fixture 影响推断': 'Collected fixture binding leads to a changed module',
}


def _english_reason(reason):
    suffix = '（基线快照）'
    baseline = reason.endswith(suffix)
    head, divider, trace = reason.removesuffix(suffix).partition(' · ')
    translated = _EN_REASONS.get(head)
    if translated is None:
        return 'Recorded recommendation · ' + reason
    return translated + divider + trace + (' (baseline snapshot)' if baseline else '')


def _suggestion_reason(row, language):
    reason = row['reason']
    definition = row.get('definition')
    if language == 'zh':
        detail = row.get('definition_reason')
        return f'{detail} · {reason}' if detail and detail != reason else reason
    details = []
    if definition:
        label = 'Added test definition' if definition['status'] == 'added' else 'Modified test definition'
        details.append(f'{label} ({definition["path"]}:{definition["line"]})')
    if not definition or reason != row['definition_reason']:
        details.append(_english_reason(reason))
    return ' · '.join(details)


def render_suggestion(result, language='zh'):
    discovery = result['collection']
    zh = language == 'zh'
    limit_labels = {'partial': ('部分发现', 'Partial discovery'), 'truncated': ('列表已截断', 'List truncated')}
    limits = [labels[not zh] for key, labels in limit_labels.items() if discovery[key]]
    limits_text = ' · ' + ('、' if zh else ', ').join(limits) if limits else ''
    status = {
        'ready': ('已保存待审阅的推荐清单', 'Recommended selection saved for review'),
        'no_recommendations': ('没有推荐项，请手动选测', 'No recommendations. Select tests explicitly.'),
        'blocked': ('收集受阻，请检查收集日志', 'Collection blocked. Check the collection log.'),
        'incomplete': ('发现不完整，请核对可用范围', 'Discovery incomplete. Review the available scope.'),
        'too_many_recommendations': (f'推荐超过单次 {MAX_SELECTED} 项，请手动缩小范围',
                                     f'More than {MAX_SELECTED} recommendations. Narrow the selection.'),
    }[result['status']][not zh]
    candidate = result['after_commit']
    if candidate == '@working-tree':
        candidate = '固定工作区快照' if zh else 'frozen working tree'
    scope = discovery['collection_path'] or ('pytest 默认范围' if zh else 'pytest default scope')
    discovered, recommended = result['discovered_count'], result['recommended_count']
    lines = [status]
    if zh:
        lines.extend([
            f'已冻结会话 · {result["directory"]}',
            f'基线 {result["before_commit"]} → 候选 {candidate}',
            f'发现范围 · {scope}',
            f'已记录 {discovered} 项 · 推荐 {recommended} 项{limits_text}',
            '推荐来自已记录的测试定义变更、源码关联或 fixture 绑定。未推荐项仍可手动选择。',
            '此次只实际收集。pytest 会加载可信项目、测试文件和插件。',
        ])
    else:
        lines.extend([
            f'Frozen session · {result["directory"]}',
            f'Baseline {result["before_commit"]} → Candidate {candidate}',
            f'Discovery scope · {scope}',
            f'Discovered {discovered} · Recommended {recommended}{limits_text}',
            'Recommendations come from recorded test definition changes, source links, or fixture bindings. '
            'You can also select other discovered IDs.',
            'This step only collects. Pytest loads the trusted project, test files, and plugins.',
        ])
    if result['snapshot_scope']:
        lines.append(('限定源码快照 · ' if zh else 'Restricted source snapshot · ') +
                     (', ' if not zh else '、').join(result['snapshot_scope']))
    for row in result['recommendations'][:30]:
        lines.append(f'  {row["test_id"]} · {_suggestion_reason(row, language)}')
    if (remaining := recommended - 30) > 0:
        path = result['suggestion_file']
        lines.append(f'  另有 {remaining} 项推荐，完整理由见 {path}' if zh else
                     f'  {remaining} more recommendations. Complete reasons are saved in {path}')
    if result['discovered_file']:
        lines.append(('可用测试 ID · ' if zh else 'Available test IDs · ') + result['discovered_file'])
    if result['run_command']:
        lines.extend([
            ('待审阅清单 · ' if zh else 'Selection to review · ') + result['selection_file'],
            '确认或编辑清单后，执行同一会话：' if zh else 'Review or edit the list, then run the same frozen session:',
            result['run_command'],
        ])
    else:
        lines.append('未生成运行命令。修复后可重新发现，或从可用 ID 明确指定本次范围。' if zh else
                     'No run command was generated. Resolve discovery issues or explicitly choose available IDs.')
    if result['status'] in ('blocked', 'incomplete'):
        if zh:
            lines.append(discovery['diagnosis']['summary'])
        elif missing := discovery['diagnosis'].get('missing_modules'):
            lines.append('Missing modules · ' + ', '.join(missing))
            if generated := discovery['diagnosis'].get('generated_version_file'):
                lines.append(f'Hatch VCS declares {generated["path"]} as a build output in pyproject.toml. '
                             'It is absent from the frozen snapshot. To check installed behavior, build each '
                             'revision as a wheel and compare them with project verify-wheels.')
        lines.append(('收集日志 · ' if zh else 'Collection log · ') + result['collection_log'])
    return '\n'.join(lines) + '\n'
