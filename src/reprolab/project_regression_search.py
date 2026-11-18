"""Locate an observed pytest regression along a bounded first-parent Git history."""
import subprocess
from pathlib import Path

from .git_source import resolve_commit
from .persistence import atomic_write, save_json
from .project_comparison import pytest_parameter_changes
from .project_input_control import input_changes
from .project_selections import validate_selection
from .recipes import repository_root
from .runner import RunCancelled


def locate_regression(repository, before, after, output, *, selected, python_executable=None,
                      timeout=120, max_commits=40, control):
    from .project_review_summary import review_summary
    from .project_sessions import prepare_project, run_project
    from .project_test_contract import _contract_files

    selected = validate_selection(selected)
    if not 2 <= max_commits <= 200:
        raise ValueError('历史定位的提交数上限须为 2–200。')
    root = repository_root(Path(repository).expanduser())
    good, bad = resolve_commit(root, before), resolve_commit(root, after)
    if good == bad:
        raise ValueError('已知通过与失败须为不同提交。')
    history = subprocess.run(
        ['git', '-C', str(root), 'rev-list', '--first-parent', f'--max-count={max_commits}', bad],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.splitlines()
    if good not in history:
        raise ValueError('通过提交须出现在失败提交的第一父提交线内，且范围须符合提交数上限。')
    revisions = list(reversed(history[:history.index(good) + 1]))
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(root):
        raise ValueError('历史定位材料须保存在被测仓库之外。')
    directory = output / 'regression-search.json'
    output.mkdir(parents=True, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        raise ValueError('请选择新的历史定位输出目录。')

    result = {'kind': 'project_regression_search', 'schema_version': 1,
              'status': 'running', 'known_good': good, 'known_bad': bad,
              'selected_tests': selected, 'planned_commits': revisions,
              'observations': [], 'transitions': [], 'first_regression': None,
              'notice': '按第一父提交顺序实际运行，保留每次测试证据。输入变化或受阻提交会限制归因。'}
    import json

    atomic_write(directory, json.dumps(result, ensure_ascii=False, indent=2), replace=False)
    shared = support = None
    for position, revision in enumerate(revisions, start=1):
        try:
            control.notify('regression_search', {'position': position, 'total': len(revisions), 'commit': revision})
            run_control = control.child(progress=control.progress)
            state = prepare_project(root, good, revision, output / 'sessions', python_executable=python_executable)
            report = run_project(state['directory'], output / 'reports', selected_ids=selected,
                                 confirmed=True, python_executable=python_executable, timeout=timeout,
                                 control=run_control)
            verifier_control = None if report['status'] == 'cancelled' else control
            summary = review_summary(report['report_dir'], policy='strict', control=verifier_control)
            candidate = Path(report['report_dir']) / 'evidence/project/inputs/after'
            current_support = _contract_files(candidate, selected, (), control=verifier_control)
        except (RunCancelled, KeyboardInterrupt):
            result['status'] = 'cancelled'
            break
        except (OSError, ValueError) as exc:
            result['observations'].append({'commit': revision, 'status': 'blocked', 'reason': str(exc)})
        else:
            gate = summary['gate']
            if shared is None and revision == good:
                shared = report['shared_tests']
                support = current_support
            status = gate['status']
            reason = gate['reason']
            if (shared is None or report['shared_tests'] != shared or current_support != support or report.get('fixture_changes')
                    or pytest_parameter_changes(report) or input_changes(report)):
                status, reason = 'blocked', 'test_inputs_changed_or_unconfirmed'
            result['observations'].append({'commit': revision, 'status': status, 'reason': reason,
                                           'report_dir': report['report_dir'], 'report_sha256': summary['report_sha256'],
                                           'gate': gate})
            if report['status'] == 'cancelled':
                result['status'] = 'cancelled'
                break
        save_json(directory, result)

    rows = result['observations']
    transitions = [{'before': left['commit'], 'after': right['commit']}
                   for left, right in zip(rows, rows[1:]) if left['status'] == 'passed' and right['status'] == 'failed']
    result['transitions'] = transitions
    endpoints = bool(rows and rows[0]['status'] == 'passed' and rows[-1]['commit'] == bad and rows[-1]['status'] == 'failed')
    if result['status'] != 'cancelled':
        if endpoints and all(row['status'] in ('passed', 'failed') for row in rows) and transitions:
            result['status'] = 'located'
            result['first_regression'] = transitions[0]
        else:
            result['status'] = 'incomplete'
    save_json(directory, result)
    return {**result, 'summary_file': str(directory)}, 0 if result['status'] == 'located' else 2
