import hashlib
import json
from pathlib import Path
import sys

import pytest

from reprolab.runtime_environment import resolve_variables, variable_record, validate_variables
from reprolab.runner import RunControl, run_case
from reprolab.project_sessions import (prepare_project, collect_project, run_project, check_project_report,
    pack_project_report, unpack_project_report, replay_project_report)
from reprolab.pytest_profile import read_pytest_profile
from reprolab.workspace import WORKTREE
from test_pytest_profile import make_project

SECRET = 'local-only-value-for-runtime-tests'


def configured(tmp_path, *, local=True):
    repo = make_project(tmp_path / 'project')
    with (repo / 'pyproject.toml').open('a') as stream:
        stream.write(('pass_env = ["DATABASE_TOKEN"]\n' if local else '') +
                     '[tool.reprolab.pytest.environment]\nAPP_MODE = "testing"\n')
    path = repo / 'tests/test_calc.py'
    text = 'import os,hashlib\nassert os.environ["APP_MODE"] == "testing"\n'
    if local:
        text += f'assert hashlib.sha256(os.environ["DATABASE_TOKEN"].encode()).hexdigest() == "{hashlib.sha256(SECRET.encode()).hexdigest()}"\n'
    text += 'assert "UNDECLARED_TOKEN" not in os.environ\n'
    path.write_text(text + path.read_text())
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions', python_executable=sys.executable)
    return repo, state


def control():
    return RunControl(runtime_values={'DATABASE_TOKEN': SECRET})


@pytest.mark.parametrize('name', ['PATH', 'PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'DYLD_INSERT_LIBRARIES', 'LD_PRELOAD', 'REPROLAB_TEST', 'not-valid', '1BAD'])
def test_reserved_or_invalid_names_cannot_override_execution_controls(name):
    with pytest.raises(ValueError): validate_variables({name: 'x'})
    with pytest.raises(ValueError): validate_variables({}, [name])


@pytest.mark.parametrize('value', [None, 2, 'x\0y', 'x' * 8193, '中' * 3000])
def test_invalid_values_are_rejected_without_echo(value):
    with pytest.raises(ValueError, match='未显示变量值') as caught:
        validate_variables({'NAME': value})
    assert 'x\0y' not in str(caught.value)


def test_empty_values_and_undeclared_inputs_have_explicit_semantics(monkeypatch):
    monkeypatch.setenv('HOST_VALUE', '')
    monkeypatch.setenv('UNDECLARED_TOKEN', SECRET)
    env, record = resolve_variables({'environment': {'EMPTY': ''}, 'pass_env': ['HOST_VALUE']}, {'EXTRA': SECRET})
    assert env['EMPTY'] == env['HOST_VALUE'] == ''
    assert 'EXTRA' not in env and 'UNDECLARED_TOKEN' not in env
    assert record == {'schema_version': 1, 'values': {'EMPTY': ''}, 'local_names': ['HOST_VALUE']}
    with pytest.raises(ValueError): validate_variables({'SAME': 'x'}, ['SAME'])


def test_required_values_stop_before_collection_without_secret_echo(tmp_path, monkeypatch):
    repo, state = configured(tmp_path)
    monkeypatch.delenv('DATABASE_TOKEN', raising=False)
    with pytest.raises(ValueError, match='DATABASE_TOKEN'):
        collect_project(state['directory'])
    assert not (Path(state['directory']) / 'collections').exists()
    assert not (tmp_path / 'reports').exists()


def test_project_freezes_values_across_discovery_and_both_versions_and_replays(tmp_path, monkeypatch):
    repo, state = configured(tmp_path)
    monkeypatch.setenv('UNDECLARED_TOKEN', 'must-not-forward')
    monkeypatch.delenv('DATABASE_TOKEN', raising=False)
    op = control()
    def change(phase, payload):
        if phase == 'project_after':
            op.runtime_values['DATABASE_TOKEN'] = 'changed midway'
            monkeypatch.setenv('DATABASE_TOKEN', 'changed host midway')
    op.progress = change
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True, control=op)
    assert report['status'] == 'regression_passed' and report['entries'][0]['status'] == 'improved'
    expected = variable_record(read_pytest_profile(repo))
    assert report['runtime_environment'] == report['before']['runtime_environment'] == report['after']['runtime_environment'] == expected
    assert check_project_report(report['report_dir'])['status'] == 'consistent'
    for path in Path(report['report_dir']).rglob('*'):
        if path.is_file(): assert SECRET.encode() not in path.read_bytes(), path
    bundle = tmp_path / 'evidence.zip'
    pack_project_report(report['report_dir'], bundle)
    imported = unpack_project_report(bundle, tmp_path / 'imported')
    preview = replay_project_report(imported['report_dir'], tmp_path / 'replays')
    assert preview['required_local_variables'] == ['DATABASE_TOKEN'] and SECRET not in json.dumps(preview)
    monkeypatch.delenv('DATABASE_TOKEN')
    with pytest.raises(ValueError, match='DATABASE_TOKEN'):
        replay_project_report(imported['report_dir'], tmp_path / 'missing', confirmed=True)
    replay = replay_project_report(imported['report_dir'], tmp_path / 'replays', confirmed=True, control=control())
    assert replay['status'] == 'regression_passed' and replay['run_id'] != report['run_id']
    assert check_project_report(replay['report_dir'])['status'] == 'consistent'
    path = Path(report['report_dir']) / 'report.json'
    raw = json.loads(path.read_text()); raw['runtime_environment']['values']['APP_MODE'] = 'changed'
    path.write_text(json.dumps(raw))
    assert check_project_report(path.parent)['status'] == 'inconsistent'


def test_cli_uses_only_declared_host_variables_and_json_does_not_store_values(tmp_path, monkeypatch, capsys):
    from reprolab.project_cli import main
    _, state = configured(tmp_path)
    monkeypatch.setenv('DATABASE_TOKEN', SECRET)
    monkeypatch.setenv('UNDECLARED_TOKEN', 'must-not-forward')
    code = main(['run', state['directory'], '--output', str(tmp_path / 'reports'), '--progress'])
    streams = capsys.readouterr()
    assert code == 0 and json.loads(streams.out)['status'] == 'regression_passed'
    assert SECRET not in streams.out + streams.err


def test_strict_case_preflight_and_evidence_use_the_same_variables(tmp_path, monkeypatch):
    from reprolab.preflight import check_case
    from reprolab.evidence import check_report
    _, state = configured(tmp_path)
    monkeypatch.delenv('DATABASE_TOKEN', raising=False)
    folder = Path(state['directory'])
    case = tmp_path / 'case.json'
    case.write_text(json.dumps({'title': 'variables', 'before': str(folder / 'inputs/before'),
        'after': str(folder / 'inputs/after'), 'test': str(folder / 'inputs/after/tests/test_calc.py'),
        'test_target': 'tests/test_calc.py::test_answer', 'expected_failure': {'type': 'AssertionError', 'message_contains': '42'}}))
    rejected = check_case(case, tmp_path / 'reports')
    assert rejected['status'] != 'ready' and any(row['id'] == 'runtime_variables' and row['status'] == 'error' for row in rejected['checks'])
    report = run_case(case, tmp_path / 'reports', control=control())
    assert report['status'] == 'verified' and check_report(report['report_dir'])['status'] == 'consistent'
    assert report['before']['runtime_environment'] == report['after']['runtime_environment']
    with pytest.raises(ValueError, match='DATABASE_TOKEN'):
        run_case(Path(report['report_dir']) / 'evidence/case.json', tmp_path / 'missing')
    replay = run_case(Path(report['report_dir']) / 'evidence/case.json', tmp_path / 'replayed', control=control())
    assert replay['status'] == 'verified'


def test_public_values_are_portable_and_plugin_edits_preserve_them(tmp_path):
    from reprolab.project_config import preview_configuration, apply_configuration, preview_configuration_undo, undo_configuration
    repo, state = configured(tmp_path, local=False)
    original = (repo / 'pyproject.toml').read_bytes()
    plan = preview_configuration(repo, ['test_support'], 'prepend')
    applied = apply_configuration(plan, tmp_path / 'undo')
    assert read_pytest_profile(repo)['environment'] == {'APP_MODE': 'testing'}
    undo_configuration(preview_configuration_undo(repo, applied['record']))
    assert (repo / 'pyproject.toml').read_bytes() == original
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert report['status'] == 'regression_passed'
    replay = replay_project_report(report['report_dir'], tmp_path / 'replayed', confirmed=True)
    assert replay['status'] == 'regression_passed'


def test_history_never_treats_unrecorded_local_values_as_same_conditions(monkeypatch):
    from test_project_conditions import pair
    from test_project_recheck import compare
    before, after = pair()
    for report in (before, after):
        report['pytest_profile'] = {'pass_env': ['DATABASE_TOKEN']}
        report['runtime_environment'] = variable_record(report['pytest_profile'])
    result = compare(monkeypatch, before, after)
    assert result['entries'][0]['conditions']['runtime_variables'] is None
    assert result['same_check_resolved'] == 0 and result['entries'][0]['state_change'] == 'became_passed'
    for report, value in ((before, 'old'), (after, 'new')):
        report['pytest_profile'] = {'environment': {'APP_MODE': value}}
        report['runtime_environment'] = variable_record(report['pytest_profile'])
    result = compare(monkeypatch, before, after)
    assert result['entries'][0]['conditions']['runtime_variables'] is False
    assert result['entries'][0]['change'] == 'conditions_changed'
