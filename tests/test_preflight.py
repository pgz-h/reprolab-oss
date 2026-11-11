import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_runner import make_case
from test_git_source import git, prepare_repo


def check(*args, **kwargs):
    from reprolab.preflight import check_case
    return check_case(*args, **kwargs)


def errors(result):
    return [item for item in result['checks'] if item['status'] == 'error']


def test_snapshot_preflight_is_read_only_and_never_imports_test(tmp_path):
    marker = tmp_path / 'must-not-execute'
    case = make_case(tmp_path, test=f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef test_one(): pass\n")
    output = tmp_path / 'not-created/reports'
    result = check(case, output)
    assert result['status'] == 'ready', result
    assert result['tests_executed'] is False
    assert not marker.exists() and not output.parent.exists()
    assert result['runtime']['executable'] == sys.executable
    assert result['environment']['status'] == 'recorded'
    assert result['environment_comparison']['status'] == 'unknown'
    assert '动态' in result['notice']
    assert all(item['status'] in {'ok', 'warning', 'error'} for item in result['checks'])


@pytest.mark.parametrize('raw', [b'[]', b'null', b'\xff', b'{"source": 2}', b'{"source":"git","repository":[]}'])
def test_invalid_config_is_attention_without_raising(tmp_path, raw):
    path = tmp_path / 'case.json'
    path.write_bytes(raw)
    result = check(path)
    assert result['status'] == 'attention'
    assert errors(result)
    assert result['tests_executed'] is False


@pytest.mark.parametrize('timeout', [True, None, 0, float('nan'), float('inf'), 301])
def test_bad_timeout_is_a_check(tmp_path, timeout):
    result = check(make_case(tmp_path), timeout=timeout)
    assert any(item['id'] == 'timeout' for item in errors(result))


@pytest.mark.parametrize('destination', ['before/reports', '.', 'regression.py', 'case.json'])
def test_output_overlap_or_file_is_rejected_without_writes(tmp_path, destination):
    case = make_case(tmp_path)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = check(case, tmp_path / destination)
    assert result['status'] == 'attention'
    assert any(item['id'] == 'output' for item in errors(result))
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_missing_pytest_venv_and_explicit_override(tmp_path):
    case = make_case(tmp_path)
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(tmp_path / 'venv')], check=True)
    data = json.loads(case.read_text())
    data['python'] = 'venv/bin/python'
    case.write_text(json.dumps(data))
    missing = check(case)
    assert missing['status'] == 'attention'
    assert missing['environment']['executable'] == str(tmp_path / 'venv/bin/python')
    assert any('pytest' in item['message'] for item in errors(missing))
    assert check(case, python_executable=sys.executable)['status'] == 'ready'


def git_case(tmp_path, target=True):
    repo, before, after, test = prepare_repo(tmp_path)
    if target:
        (repo / 'test_real.py').write_text('raise RuntimeError("do not execute")\ndef test_one(): pass\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'test')
        after = git(repo, 'rev-parse', 'HEAD')
    config = {'source': 'git', 'title': 'git preflight', 'repository': 'repo',
              'before_ref': before, 'after_ref': after, 'expected_failure': {'type': 'ValueError', 'message_contains': 'bug'},
              **({'test_target': 'test_real.py::test_one'} if target else {'test': 'regression.py'})}
    path = tmp_path / 'case.json'
    path.write_text(json.dumps(config))
    return path, repo, after


def test_git_preflight_reads_commits_without_checkout_or_export(tmp_path):
    case, repo, after = git_case(tmp_path)
    (repo / 'sample.py').write_text('dirty content')
    status = git(repo, 'status', '--porcelain')
    result = check(case, tmp_path / 'reports')
    assert result['status'] == 'ready', result
    assert git(repo, 'rev-parse', 'HEAD') == after
    assert git(repo, 'status', '--porcelain') == status
    assert not (tmp_path / 'reports').exists()


@pytest.mark.parametrize('change', ['bad_ref', 'missing_target', 'bad_expected', 'symlink_target'])
def test_git_preflight_reports_invalid_inputs(tmp_path, change):
    case, repo, after = git_case(tmp_path)
    data = json.loads(case.read_text())
    if change == 'bad_ref': data['before_ref'] = 'does-not-exist'
    elif change == 'missing_target': data['test_target'] = 'missing.py::test_one'
    elif change == 'bad_expected': data['expected_failure']['type'] = None
    else:
        (repo / 'test_link.py').symlink_to('test_real.py')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'symlink')
        data['after_ref'] = 'HEAD'
        data['test_target'] = 'test_link.py::test_one'
    case.write_text(json.dumps(data))
    assert check(case)['status'] == 'attention'


def test_syntax_error_and_source_symlink_are_checks(tmp_path):
    case = make_case(tmp_path, test='def test_bad(: pass\n')
    (tmp_path / 'before/link.py').symlink_to(tmp_path / 'after/sample.py')
    result = check(case)
    assert result['status'] == 'attention'
    assert any(item['id'] == 'test' for item in errors(result))
    assert any(item['id'] == 'source_before' for item in errors(result))


def test_cancel_is_propagated_without_creating_output(tmp_path):
    from reprolab.runner import RunCancelled, RunControl
    control = RunControl()
    control.cancel()
    with pytest.raises(RunCancelled):
        check(make_case(tmp_path), tmp_path / 'reports', control=control)
    assert not (tmp_path / 'reports').exists()


def test_invalid_python_path_and_huge_timeout_do_not_raise(tmp_path):
    case = make_case(tmp_path)
    result = check(case, python_executable=[], timeout=10 ** 1000)
    assert result['status'] == 'attention'
    assert {item['id'] for item in errors(result)} >= {'timeout', 'python_path'}
    assert result['environment']['status'] == 'unavailable'


def test_existing_file_in_output_parent_is_attention(tmp_path):
    case = make_case(tmp_path)
    destination = tmp_path / 'blocked'
    destination.write_text('keep')
    result = check(case, destination / 'nested/reports')
    assert any(item['id'] == 'output' for item in errors(result))
    assert destination.read_text() == 'keep'


def test_git_saved_relative_python_uses_case_directory(tmp_path):
    case, repo, after = git_case(tmp_path)
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(tmp_path / 'venv')], check=True)
    data = json.loads(case.read_text())
    data['python'] = 'venv/bin/python'
    case.write_text(json.dumps(data))
    result = check(case)
    assert result['environment']['executable'] == str(tmp_path / 'venv/bin/python')
    assert any(item['id'] == 'runtime' for item in errors(result))
    assert check(case, python_executable=sys.executable)['status'] == 'ready'


def test_environment_change_warns_without_blocking_explicit_selection(tmp_path):
    from reprolab.environment import snapshot_environment
    old = snapshot_environment()
    old['distributions'] = {**old['distributions'], 'old-extra': '1'}
    result = check(make_case(tmp_path), previous_environment=old)
    assert result['status'] == 'ready'
    assert result['environment_comparison']['status'] == 'changed'
    assert any(item['status'] == 'warning' for item in result['checks'])


def test_cancellation_after_runtime_probe_propagates(tmp_path, monkeypatch):
    from reprolab import preflight
    from reprolab.runner import RunCancelled, RunControl
    control = RunControl()
    real = preflight.snapshot_environment
    def cancelling_probe(executable=None):
        result = real(executable)
        control.cancel()
        return result
    monkeypatch.setattr(preflight, 'snapshot_environment', cancelling_probe)
    with pytest.raises(RunCancelled):
        check(make_case(tmp_path), control=control)


@pytest.mark.parametrize('test_path', ['bad\x00.py', 'loop.py'])
def test_invalid_git_standalone_path_is_a_check(tmp_path, test_path):
    case, repo, after = git_case(tmp_path, target=False)
    (tmp_path / 'loop.py').symlink_to('loop.py')
    data = json.loads(case.read_text())
    data['test'] = test_path
    case.write_text(json.dumps(data))
    result = check(case)
    assert result['status'] == 'attention'
    assert any(item['id'] == 'test' for item in errors(result))


@pytest.mark.parametrize('source', ['snapshot', 'git'])
def test_older_host_checks_syntax_with_selected_newer_python(tmp_path, source):
    host_name = shutil.which('python3.12')
    selected_name = sys.executable if sys.version_info[:2] == (3, 14) else shutil.which('python3.14')
    if not host_name or not selected_name:
        pytest.skip('Python 3.12 host and Python 3.14 selected runtime are required')
    host, selected = Path(host_name), Path(selected_name)
    marker = tmp_path / 'never-executed'
    test_source = f'from pathlib import Path\nPath({str(marker)!r}).touch()\nlabel = t"hello"\ndef test_one(): pass\n'
    if source == 'snapshot':
        case = make_case(tmp_path, test=test_source)
    else:
        case, _, _ = git_case(tmp_path, target=False)
        (tmp_path / 'regression.py').write_text(test_source)
    package_source = Path(__file__).resolve().parents[1] / 'src'
    code = ('import json,sys; sys.path.insert(0,sys.argv[1]); '
            'from reprolab.preflight import check_case; '
            'print(json.dumps(check_case(sys.argv[2],python_executable=sys.argv[3])))')
    process = subprocess.run([str(host), '-I', '-c', code, str(package_source), str(case), str(selected)],
                             capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(process.stdout)
    assert result['status'] == 'ready', result['checks']
    assert result['tests_executed'] is False
    assert result['environment']['executable'] == str(selected)
    assert not marker.exists()


def test_syntax_error_is_decided_by_older_selected_python(tmp_path):
    older = Path('/opt/homebrew/opt/python@3.12/bin/python3.12')
    if not older.is_file():
        pytest.skip('Python 3.12 selected runtime is required')
    case = make_case(tmp_path, test='label = t"private-source-text"\ndef test_one(): pass\n')
    result = check(case, python_executable=str(older))
    assert any(item['id'] == 'test' for item in errors(result))
    assert 'private-source-text' not in json.dumps(result)


def test_frozen_own_syntax_parse_does_not_launch_dispatcher(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('own frozen app must not be used as a Python -c interpreter')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    assert check(make_case(tmp_path))['status'] == 'ready'


@pytest.mark.parametrize('output', ['private-startup-output', 'x' * 5000])
def test_syntax_probe_never_echoes_startup_output(tmp_path, output):
    from reprolab.preflight import _test_syntax
    test = tmp_path / 'test_sample.py'
    test.write_text('def test_one(): pass\n')
    executable = tmp_path / 'pretend-python'
    executable.write_text('#!/bin/sh\nprintf %s ' + repr(output) + '\n')
    executable.chmod(0o755)
    with pytest.raises(ValueError) as failure:
        _test_syntax(test, str(executable))
    assert 'private-startup-output' not in str(failure.value)
    assert len(str(failure.value)) < 200
