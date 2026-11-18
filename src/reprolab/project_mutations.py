"""Measure whether the existing pytest selection detects explicit Python boolean mutations."""
import ast
import json
from pathlib import Path
import shutil

from .case import parse_test_target
from .changes import describe_changes
from .persistence import save_json
from .project_archive import source_zip_preflight
from .project_collection import normalize_collection_path
from .project_history import _checked_report
from .project_sessions import _new_id, project_replay_settings, read_project, run_project
from .replay_verification import open_replay_evidence
from .runner import source_fingerprint, source_mode_fingerprint

_KIND = 'reprolab_mutation_check'
_RESULT = 'mutation-check.json'


def _mutations(text):
    tree = ast.parse(text.decode('utf-8'))
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or type(node.value) is not bool:
            continue
        start = offsets[node.lineno - 1] + node.col_offset
        stop = offsets[node.end_lineno - 1] + node.end_col_offset
        original = str(node.value)
        if text[start:stop] == original.encode():
            found.append({'line': node.lineno, 'start': start, 'stop': stop,
                          'original': original, 'replacement': str(not node.value)})
    return sorted(found, key=lambda row: row['start'])


def _contrast(run):
    if all(run[side]['status'] == 'completed' for side in ('before', 'after')):
        if all(entry['before']['status'] == 'passed' for entry in run['entries']):
            if any(entry['after']['status'] == 'failed' for entry in run['entries']):
                return 'detected'
            if all(entry['after']['status'] == 'passed' for entry in run['entries']):
                return 'survived'
    return 'blocked'


def _replace(data, mutation):
    start, stop = mutation['start'], mutation['stop']
    if not 0 <= start < stop <= len(data) or data[start:stop] != mutation['original'].encode():
        raise ValueError('布尔变异位置不匹配。')
    return data[:start] + mutation['replacement'].encode() + data[stop:]


def _verdict(mutants, requested):
    states = {row['status'] for row in mutants}
    return ('survived' if 'survived' in states else 'blocked'
            if 'blocked' in states or len(mutants) != requested else 'detected')


def mutation_check(report, path, output, *, max_mutants, python_executable, timeout, confirmed, control):
    path = normalize_collection_path(path)
    if not path.endswith('.py') or not 1 <= max_mutants <= 50:
        raise ValueError('请选择 Python 源码 .py 文件与 1–50 个变异的预算。')
    output = Path(output).expanduser().resolve()
    with open_replay_evidence(report, control=control) as folder:
        original = _checked_report(folder, control=control)
        if (original.get('runner', 'pytest') != 'pytest' or original['after']['status'] != 'completed'
                or not all(entry['after']['status'] == 'passed' for entry in original['entries'])):
            raise ValueError('变异核对需要候选在显式 pytest 测试范围中均通过。')
        if path in {parse_test_target(target) for target in original['selected_tests']} or path.endswith('conftest.py'):
            raise ValueError('请选择应用源码文件。')
        snapshot = Path(original['report_dir']) / 'evidence/project/inputs/after'
        file = snapshot / path
        if not file.is_file() or not file.resolve().is_relative_to(snapshot):
            raise ValueError('候选快照不含指定源码。')
        data = file.read_bytes()
        mutations = _mutations(data)
        if not mutations:
            raise ValueError('该文件没有可变异的布尔字面量。')
        result = {'kind': _KIND, 'schema_version': 1, 'status': 'ready', 'path': path,
                  'source_report_sha256': original['_report_sha256'],
                  'candidate_source_sha256': original['source_sha256']['after'],
                  'selected_tests': original['selected_tests'], 'available': len(mutations),
                  'requested': min(max_mutants, len(mutations)), 'mutants': []}
        if not confirmed:
            return result, 0
        if output.exists() or output.is_symlink() or any(output.is_relative_to(Path(value).resolve())
                                                        for value in (original['report_dir'], original['repository'])):
            raise ValueError('请选择来源及被测仓库之外的新证据目录。')
        output.mkdir(parents=True)
        save_json(output / _RESULT, result)
        session = read_project(Path(original['report_dir']) / 'evidence/project')
        runtime = project_replay_settings(original, python_executable=python_executable, timeout=timeout)
        for number, mutation in enumerate(mutations[:max_mutants], 1):
            control.check()
            cell = output / 'sessions' / f'{number:02d}'
            tree = cell / 'inputs'
            tree.mkdir(parents=True)
            for side in ('before', 'after'):
                shutil.copytree(snapshot, tree / side)
            (tree / 'after' / path).write_bytes(_replace(data, mutation))
            state = {key: value for key, value in session.items() if key not in
                     ('collection', 'collection_record', 'collection_sha256', 'review_source', 'pr_context',
                      'selection_request', 'refresh_origin', 'excluded_tracked_changes')}
            state.update(project_id=_new_id('mutation'), directory=str(cell), status='prepared', runs=[],
                         before_commit=original['after_commit'], after_commit='@working-tree',
                         source_sha256={side: source_fingerprint(tree / side)[1] for side in ('before', 'after')},
                         source_mode_sha256={side: source_mode_fingerprint(tree / side) for side in ('before', 'after')},
                         changed_files=[{'path': path, 'kind': 'modified'}], recommendations={},
                         changes=describe_changes(tree / 'before', tree / 'after'),
                         archive_preflight=source_zip_preflight(tree, control=control))
            save_json(cell / 'project.json', state)
            run = run_project(cell, output / 'reports', selected_ids=original['selected_tests'], confirmed=True,
                              python_executable=runtime['runtime']['executable'], timeout=runtime['timeout'],
                              control=control.child(progress=control.progress))
            checked = _checked_report(run['report_dir'])
            result['mutants'].append({'mutation': mutation, 'status': _contrast(checked),
                                      'report': Path(run['report_dir']).relative_to(output).as_posix(),
                                      'report_sha256': checked['_report_sha256']})
            save_json(output / _RESULT, result)
            if run['status'] == 'cancelled':
                break
        result['status'] = _verdict(result['mutants'], result['requested'])
        save_json(output / _RESULT, result)
        return result, {'detected': 0, 'survived': 1, 'blocked': 2}[result['status']]


def check_mutations(directory, *, control):
    root = Path(directory).expanduser().resolve()
    file = root / _RESULT
    if file.is_symlink() or not file.is_file() or file.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('请选择完整的变异证据。')
    result = json.loads(file.read_bytes())
    if result.get('kind') != _KIND or result.get('schema_version') != 1:
        raise ValueError('变异证据格式无效。')
    path = normalize_collection_path(result['path'])
    for row in result['mutants']:
        target = (root / row['report']).resolve()
        if not target.is_relative_to(root):
            raise ValueError('变异试探报告路径越界。')
        report = _checked_report(target, control=control)
        tree = Path(report['report_dir']) / 'evidence/project/inputs'
        baseline = (tree / 'before' / path).read_bytes()
        if (report['_report_sha256'] != row['report_sha256'] or report['source_sha256']['before'] != result['candidate_source_sha256']
                or report['selected_tests'] != result['selected_tests'] or row['mutation'] not in _mutations(baseline)
                or (tree / 'after' / path).read_bytes() != _replace(baseline, row['mutation'])
                or _contrast(report) != row['status']):
            raise ValueError('变异文件或观察判定与实际报告不一致。')
    if (type(result.get('requested')) is not int or not 1 <= result['requested'] <= min(50, result['available'])
            or len(result['mutants']) > result['requested']
            or result['status'] != _verdict(result['mutants'], result['requested'])):
        raise ValueError('变异汇总与逐项结果不符。')
    return {**result, 'integrity': 'consistent'}
