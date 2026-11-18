from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from reprolab.project_sessions import prepare_project, collect_project, run_project
from reprolab.workspace import WORKTREE
import pytest
from reprolab.pytest_profile import read_pytest_profile
from reprolab.project_sessions import check_project_report, pack_project_report, unpack_project_report, replay_project_report


def make_project(root):
    root.mkdir()
    (root / 'tests').mkdir()
    (root / 'calc.py').write_text('def answer():\n    return 0\n')
    (root / 'test_support.py').write_text('import pytest\n@pytest.fixture\ndef expected():\n    return 42\n')
    (root / 'tests/test_calc.py').write_text('from calc import answer\ndef test_answer(expected):\n    assert answer() == expected\n')
    def git(*args):
        return subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c',
            'commit.gpgsign=false', '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid',
            *args], check=True, capture_output=True)
    git('init', '-q')
    git('add', '.')
    git('commit', '-qm', 'baseline')
    (root / 'calc.py').write_text('def answer():\n    return 42\n')
    (root / 'pyproject.toml').write_text('[tool.reprolab.pytest]\nplugins = ["test_support"]\nimport_mode = "importlib"\n')
    return root


def test_explicit_plugin_runs_both_frozen_versions(tmp_path):
    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions', python_executable=sys.executable)
    collection = collect_project(state['directory'])
    assert collection['status'] == 'collected'
    result = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert result['entries'][0]['status'] == 'improved', result['after']['log_excerpt']



def test_profile_is_preserved_in_strict_case_and_export_replay(tmp_path):
    from reprolab.runner import run_case
    from reprolab.evidence import check_report
    from reprolab.evidence_bundle import pack_report, unpack_report
    repo = make_project(tmp_path / 'project')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    folder = Path(state['directory'])
    case = tmp_path / 'case.json'
    case.write_text(json.dumps({'title': 'plugin regression',
        'before': str(folder / 'inputs/before'), 'after': str(folder / 'inputs/after'),
        'test': str(folder / 'inputs/after/tests/test_calc.py'),
        'test_target': 'tests/test_calc.py::test_answer',
        'expected_failure': {'type': 'AssertionError', 'message_contains': '42'}}))
    result = run_case(case, tmp_path / 'strict')
    assert result['status'] == 'verified', result['before']['log_excerpt']
    assert result['before']['pytest_profile']['plugins'] == ['test_support']
    assert result['before']['pytest_profile'] == result['after']['pytest_profile']
    assert check_report(result['report_dir'])['status'] == 'consistent'
    pack_report(result['report_dir'], tmp_path / 'case.zip')
    imported = unpack_report(tmp_path / 'case.zip', tmp_path / 'imported')
    replay = run_case(Path(imported['directory']) / 'evidence/case.json', tmp_path / 'replay')
    assert replay['status'] == 'verified'


def test_profile_frozen_with_project_evidence_and_does_not_inherit_selection_flags(tmp_path):
    repo = make_project(tmp_path / 'project')
    with (repo / 'pyproject.toml').open('a') as stream:
        stream.write('\n[tool.pytest.ini_options]\naddopts = "-k no_such_test --maxfail=1"\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    # Later edits to the live project cannot change either execution.
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\nplugins=["not_installed"]\n')
    result = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert result['entries'][0]['status'] == 'improved'
    assert result['pytest_profile']['plugins'] == ['test_support']
    assert check_project_report(result['report_dir'])['status'] == 'consistent'
    assert 'test_support' in (Path(result['report_dir']) / 'report.html').read_text()
    pack_project_report(result['report_dir'], tmp_path / 'project.zip')
    imported = unpack_project_report(tmp_path / 'project.zip', tmp_path / 'imported')
    replay = replay_project_report(imported['report_dir'], tmp_path / 'replays', confirmed=True)
    assert replay['entries'][0]['status'] == 'improved'
    report_path = Path(result['report_dir']) / 'report.json'
    raw = json.loads(report_path.read_text())
    raw['pytest_profile']['plugins'] = []
    report_path.write_text(json.dumps(raw))
    assert check_project_report(report_path.parent)['status'] == 'inconsistent'


@pytest.mark.parametrize('configuration', [
    'plugins = "pytest_asyncio.plugin"', 'plugins = ["-k"]',
    'plugins = ["../../plugin"]', 'plugins = ["no:terminal"]',
    'plugins = [42]', 'import_mode = "unknown"', 'args = ["--collect-only"]',
])
def test_invalid_profiles_are_rejected_without_importing_project(tmp_path, configuration):
    (tmp_path / 'pyproject.toml').write_text('[tool.reprolab.pytest]\n' + configuration + '\n')
    (tmp_path / 'conftest.py').write_text('raise RuntimeError("must not execute")')
    with pytest.raises(ValueError):
        read_pytest_profile(tmp_path)


def test_missing_plugin_is_actionable_and_never_a_pass(tmp_path):
    repo = make_project(tmp_path / 'project')
    (repo / 'pyproject.toml').write_text('[tool.reprolab.pytest]\nplugins=["reprolab_missing_plugin"]\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    result = collect_project(state['directory'])
    assert result['status'] == 'collection_failed'
    assert 'reprolab_missing_plugin' in result['log_excerpt']
    assert result['pytest_profile']['plugins'] == ['reprolab_missing_plugin']
    assert result['diagnosis']['missing_modules'] == ['reprolab_missing_plugin']
    with pytest.raises(ValueError, match=r'插件 reprolab_missing_plugin.*collection\.log'):
        run_project(state['directory'], tmp_path / 'reports', confirmed=True)


def test_importlib_profile_supports_duplicate_test_filenames(tmp_path):
    repo = make_project(tmp_path / 'project')
    for name in ('one', 'two'):
        folder = repo / 'tests' / name
        folder.mkdir()
        (folder / 'test_same.py').write_text('def test_value(expected):\n    assert expected == 42\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    result = collect_project(state['directory'])
    assert result['status'] == 'collected' and len(result['tests']) == 3
    # New tests are overlaid into both snapshots; the plugin is also shared.
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert report['counts'] == {'regression_passed': 2, 'improved': 1}


def test_declared_plugin_cannot_add_unselected_execution(tmp_path):
    repo = make_project(tmp_path / 'project')
    with (repo / 'test_support.py').open('a') as stream:
        stream.write('\ndef pytest_collection_modifyitems(session, items):\n    if not session.config.option.collectonly and items:\n        items.append(items[0])\n')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert report['status'] == 'attention'
    assert report['after']['tests']['tests/test_calc.py::test_answer']['status'] == 'inconclusive'
    assert check_project_report(report['report_dir'])['status'] == 'consistent'


def _commit_fixture(root, *args):
    subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                    '-c', 'user.name=ReproLab test', '-c', 'user.email=test@example.invalid', *args],
                   check=True, capture_output=True)


def _freeze_fixture(repo):
    _commit_fixture(repo, 'init', '-q')
    _commit_fixture(repo, 'add', '.')
    _commit_fixture(repo, 'commit', '-qm', 'baseline')


@pytest.mark.parametrize('config', ['pytest.ini', 'pyproject.toml'])
def test_project_warning_options_remain_active_in_comparison(tmp_path, config):
    from reprolab.project_review_summary import render_review_markdown, review_summary

    repo = tmp_path / 'project'
    repo.mkdir()
    rules = {'pytest.ini': '[pytest]\naddopts = -W error::UserWarning\n',
             'pyproject.toml': '[tool.pytest.ini_options]\naddopts = ["--pythonwarnings=error::UserWarning"]\n'}
    (repo / config).write_text(rules[config], encoding='utf-8')
    module = repo / 'step.py'
    module.write_text('def step(): pass\n', encoding='utf-8')
    (repo / 'test_build.py').write_text('from step import step\ndef test_step(): step()\n', encoding='utf-8')
    _freeze_fixture(repo)
    module.write_text('import warnings\ndef step(): warnings.warn("warning regression", UserWarning)\n', encoding='utf-8')
    native = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider'],
                            cwd=repo, capture_output=True, text=True)
    assert native.returncode == 1 and 'UserWarning: warning regression' in native.stdout
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    discovered = collect_project(state['directory'])
    expected = {'config_path': config, 'warning_filters': 1, 'other_addopts_ignored': False}
    assert discovered['pytest_rules'] == expected
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert report['status'] == 'regression_detected'
    assert [report[side]['pytest_rules'] for side in ('before', 'after')] == [expected, expected]
    assert report['entries'][0]['after']['exception']['type'] == 'UserWarning'
    summary = review_summary(report['report_dir'], policy='strict')
    assert summary['gate']['exit_code'] == 1
    assert '1 warning filter(s) used' in render_review_markdown(summary)


@pytest.mark.parametrize('explicit', [False, True])
def test_snapshot_to_installed_module_blocks_a_green_run(tmp_path, explicit):
    from reprolab.project_comparison import comparison_scope, comparison_verdict
    from reprolab.project_review_summary import review_summary

    repo = tmp_path / 'project'
    repo.mkdir()
    local_module = repo / 'tomlkit.py'
    local_module.write_text('def parse(source): return {}\n', encoding='utf-8')
    (repo / 'test_parse.py').write_text(
        'from tomlkit import parse\ndef test_parse(): assert parse("") == {}\n', encoding='utf-8')
    _freeze_fixture(repo)
    local_module.unlink()
    if explicit:
        from reprolab.project_config import preview_configuration

        config = repo / 'pyproject.toml'
        config.write_text(
            '[tool.reprolab.pytest]\nsnapshot_modules = ["tomlkit", "my_app.unobserved"]\n', encoding='utf-8')
        assert b'snapshot_modules' in preview_configuration(repo, [], 'importlib').updated
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    result = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert result['counts'] == {'regression_passed': 1}
    assert result['before']['module_origins']['tomlkit'] == ['snapshot:tomlkit.py']
    assert result['after']['module_origins']['tomlkit'] == ['installed-package']
    scope = comparison_scope(result)
    assert comparison_verdict(result, scope) == 'attention'
    expected = [('tomlkit', 'snapshot_to_external')]
    if explicit:
        expected.insert(0, ('my_app.unobserved', 'required_snapshot_import'))
    assert [(item['module'], item['reason']) for item in scope['pytest_import_issues']] == expected
    summary = review_summary(result['report_dir'], policy='strict')
    assert summary['gate']['reason'] == 'pytest_import_origin' and summary['gate']['exit_code'] == 2
    folder = Path(result['report_dir'])
    assert '环境安装包' in (folder / 'report.html').read_text(encoding='utf-8')
    checked = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    del checked['after']['module_origins']
    (folder / 'report.json').write_text(json.dumps(checked), encoding='utf-8')
    assert check_project_report(folder)['status'] == 'inconsistent'


def test_report_exposes_requested_and_started_order_when_hook_reverses_tests(tmp_path):
    from reprolab.project_review_summary import render_review_markdown, review_summary

    repo = tmp_path / 'project'
    repo.mkdir()
    (repo / 'test_order.py').write_text(
        'prepared = False\n'
        'def test_prepare():\n    global prepared\n    prepared = True\n'
        'def test_consume():\n    assert prepared\n', encoding='utf-8')
    _freeze_fixture(repo)
    (repo / 'conftest.py').write_text(
        'def pytest_collection_modifyitems(items):\n    items.reverse()\n', encoding='utf-8')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    selected = ['test_order.py::test_prepare', 'test_order.py::test_consume']
    result = run_project(state['directory'], tmp_path / 'reports', selected_ids=selected, confirmed=True)
    assert result['status'] == 'regression_detected'
    assert result['selected_tests'] == result['before']['execution_order'] == selected
    assert result['after']['execution_order'] == list(reversed(selected))
    summary = review_summary(result['report_dir'], policy='strict')
    assert summary['pytest_order_difference'] == {'common_tests': 2, 'positions_changed': 2}
    assert 'Pytest execution order differs' in render_review_markdown(summary)
    html = (Path(result['report_dir']) / 'report.html').read_text(encoding='utf-8')
    assert all(label in html for label in ('请求顺序', '基线实际', '候选实际', '两版实际启动顺序不同'))


@pytest.mark.parametrize('late_reorder', [False, True])
def test_order_control_reuses_frozen_evidence_and_checks_actual_order(tmp_path, capsys, late_reorder):
    from reprolab.project_cli import main
    from reprolab.project_review_summary import review_summary

    repo = tmp_path / 'project'
    repo.mkdir()
    (repo / 'test_order.py').write_text(
        'ready = False\n'
        'def test_prepare():\n    global ready\n    ready = True\n'
        'def test_consume():\n    assert ready\n', encoding='utf-8')
    conftest = repo / 'conftest.py'
    conftest.write_text(
        'def pytest_collection_modifyitems(items):\n    items.sort(key=lambda item: item.name, reverse=True)\n',
        encoding='utf-8')
    _freeze_fixture(repo)
    normal_reorder = 'def pytest_collection_modifyitems(items):\n    items.sort(key=lambda item: item.name)\n'
    late_hook = ('import pytest\n@pytest.hookimpl(wrapper=True, trylast=True)\n'
                 'def pytest_runtestloop(session):\n    session.items.sort(key=lambda item: item.name)\n'
                 '    return (yield)\n')
    conftest.write_text(late_hook if late_reorder else normal_reorder, encoding='utf-8')
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions')
    selection = ['test_order.py::test_consume', 'test_order.py::test_prepare']
    report = run_project(state['directory'], tmp_path / 'normal', selected_ids=selection, confirmed=True)
    assert report['status'] == 'regression_detected'
    archive = tmp_path / 'report.zip'
    pack_project_report(report['report_dir'], archive)

    output = tmp_path / 'controlled'
    args = ['order-control', str(archive), '--output', str(output)]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert not output.exists()
    assert preview['selected_tests'] == selection
    assert preview['order_control']['test_ids'] == list(reversed(selection))

    assert main([*args, '--confirm-run']) == (2 if late_reorder else 0)
    run = json.loads(capsys.readouterr().out)
    assert run['order_control_status'] == ('blocked' if late_reorder else 'completed')
    assert run['selected_tests'] == selection
    assert check_project_report(run['report_dir'])['status'] == 'consistent'
    if late_reorder:
        assert '未能按对照顺序' in run['after']['log_excerpt']
        return

    assert run['status'] == 'regression_passed'
    assert run['before']['execution_order'] == run['after']['execution_order'] == list(reversed(selection))
    assert review_summary(run['report_dir'], policy='strict')['gate']['reason'] == 'pytest_order_control'
    repeated = replay_project_report(run['report_dir'], tmp_path / 'replayed', confirmed=True, verify_result=True)
    assert repeated['replay_verification']['status'] == 'verified'
    assert repeated['replay_verification']['inputs']['pytest_order'] is True
    from reprolab.replay_verification import verify_replay
    assert verify_replay(archive, run['report_dir'])['status'] == 'inputs_changed'
    assert main([*args, '--order', 'requested', '--confirm-run']) == 0
    requested = json.loads(capsys.readouterr().out)
    assert requested['before']['execution_order'] == requested['after']['execution_order'] == selection
    assert requested['counts']['baseline_failure'] == 1
