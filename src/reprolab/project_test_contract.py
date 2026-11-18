"""Pin the test code associated with a successful protected-input run."""
import ast
from dataclasses import replace
import hashlib
from pathlib import Path, PurePosixPath
import re

from .case import parse_test_target
from .project_collection import normalize_collection_path


_TEST_ROOTS = {'test', 'tests', 'testing'}
_MAX_FILES = 5000


def _paths(values):
    if (not isinstance(values, list) or len(values) > _MAX_FILES
            or any(not name or normalize_collection_path(name) != name for name in values)
            or len(set(values)) != len(values)):
        raise ValueError('测试契约路径须为唯一、规范的仓库相对路径。')


def validate_test_contract(record, required):
    fields = {'source_report_sha256', 'additional_files', 'files'}
    if (not isinstance(record, dict) or record.keys() != fields
            or not isinstance(record['source_report_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', record['source_report_sha256'])
            or not isinstance(record['files'], dict)):
        raise ValueError('测试契约记录无效。')
    files = record['files']
    _paths(list(files))
    _paths(record['additional_files'])
    if not ({parse_test_target(node) for node in required} | set(record['additional_files'])) <= files.keys():
        raise ValueError('测试契约未覆盖全部保护测试或明确指定的辅助文件。')
    for value in files.values():
        if (not isinstance(value, dict) or value.keys() != {'algorithm', 'sha256'}
                or value['algorithm'] not in ('python_ast_v1', 'bytes_sha256')
                or not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])):
            raise ValueError('测试契约文件指纹无效。')


def _fingerprint(path):
    data = path.read_bytes()
    algorithm = 'bytes_sha256'
    if path.suffix == '.py':
        try:
            tree = ast.parse(data)
            # Empty optional AST fields vary across supported Python versions.

            def structure(node):
                if isinstance(node, ast.AST):
                    return (type(node).__name__, [(name, structure(value)) for name, value in ast.iter_fields(node)
                                                 if value is not None and value != []])
                if isinstance(node, list):
                    return [structure(item) for item in node]
                return (type(node).__name__, repr(node))

            nodes = structure(tree)
            data = repr(nodes).encode('utf-8')
            algorithm = 'python_ast_v1'
        except (SyntaxError, RecursionError, UnicodeError, ValueError):
            pass
    return {'algorithm': algorithm, 'sha256': hashlib.sha256(data).hexdigest()}


def _contract_files(snapshot, targets, additional, *, control=None):
    paths = {PurePosixPath(parse_test_target(node)) for node in targets}
    roots = set()
    support = {PurePosixPath(name) for name in additional}
    for target in paths:
        for parent in target.parents:
            support.add(parent / 'conftest.py')
        root = next((parent for parent in reversed(target.parents) if parent.name in _TEST_ROOTS), None)
        if root is not None:
            roots.add(root)
    for root in roots:
        for helper in (snapshot / root).rglob('*.py'):
            if control:
                control.check()
            if not helper.name.startswith('test_') and not helper.name.endswith('_test.py'):
                support.add(PurePosixPath(helper.relative_to(snapshot).as_posix()))
    files = {}
    for name in sorted(paths | support):
        if control:
            control.check()
        path = snapshot / name
        if path.is_file() and not path.is_symlink():
            files[name.as_posix()] = _fingerprint(path)
    return files


def check_test_contract(report, plan, *, control=None):
    saved = plan.test_contract
    snapshot = Path(report['report_dir']) / 'evidence/project/inputs/after'
    actual = _contract_files(snapshot, plan.required_passes, saved['additional_files'], control=control)
    expected = saved['files']
    changes = [{'path': file, 'reason': 'added' if file not in expected else 'missing' if file not in actual else 'changed',
                'expected': expected.get(file), 'actual': actual.get(file)}
               for file in sorted(expected.keys() | actual.keys()) if expected.get(file) != actual.get(file)]
    return {'status': 'blocked' if changes else 'passed', 'source_report_sha256': saved['source_report_sha256'],
            'file_count': len(expected), 'changes': changes}


def pin_test_contract(location, plan_path, destination, *, additional=(), receipt=None, stability=None, control=None):
    from .acceptance_plan import read_acceptance_plan, save_acceptance_plan
    from .project_history import _checked_report, report_reference
    from .project_review_summary import review_summary
    from .replay_verification import open_replay_evidence

    plan = read_acceptance_plan(plan_path)
    if plan.runner != 'pytest' or plan.protected_inputs is None:
        raise ValueError('请先生成有固定失败样本的 pytest 持续保护计划。')
    target = Path(destination).expanduser().absolute()
    additional = sorted(set(normalize_collection_path(name) for name in
                            [*((plan.test_contract or {}).get('additional_files', ())), *additional]))
    _paths(additional)
    with open_replay_evidence(location, control=control) as source:
        report = _checked_report(source, control=control)
        if target.resolve().is_relative_to(Path(report['report_dir'])):
            raise ValueError('请将计划保存在来源报告之外。')
        summary = review_summary(source, plan=plan, receipt=receipt, stability=stability,
                                 expected_report=report_reference(report), control=control)
        if summary['gate']['status'] != 'passed':
            return summary, summary['gate']['exit_code']
        snapshot = Path(report['report_dir']) / 'evidence/project/inputs/after'
        files = _contract_files(snapshot, plan.required_passes, additional, control=control)
        pinned = replace(plan, test_contract={'source_report_sha256': report['_report_sha256'],
                                              'additional_files': additional, 'files': files})
    if control:
        control.commit()
    save_acceptance_plan(pinned, target)
    return {'status': 'saved', 'output': str(target), 'acceptance_plan_sha256': pinned.fingerprint(),
            'source_report_sha256': report['_report_sha256'], 'files': list(files)}, 0
