"""A saved AI response can be recovered without another request or any test run."""
import json
from pathlib import Path

import pytest

from reprolab import repair_suite
from test_git_source import prepare_repo
from test_repair_suite import _plan_response


@pytest.fixture
def prepared_suite(tmp_path):
    repo, before, after, _ = prepare_repo(tmp_path)
    state = repair_suite.prepare_suite(repo, before, after, '修复 compute', tmp_path / 'suites')
    response = tmp_path / 'response.json'
    plan = _plan_response()
    plan['checks'] = plan['checks'][:1]
    response.write_text(json.dumps(plan), encoding='utf-8')
    return Path(state['directory']), response


def fail_publication(monkeypatch):
    save = repair_suite.save_json

    def failed(path, data):
        if Path(path).name == 'plan.json':
            raise OSError('teaching disk failure')
        return save(path, data)

    monkeypatch.setattr(repair_suite, 'save_json', failed)


def test_complete_response_survives_publication_failure_and_recovers_offline(prepared_suite, monkeypatch):
    folder, response = prepared_suite
    with monkeypatch.context() as failure:
        fail_publication(failure)
        with pytest.raises(OSError, match='disk failure'):
            repair_suite.generate_suite(folder, response_file=response, model='审阅的外部模型')
    assert not any((folder / name).exists() for name in ('plan.json', 'cases', 'tests'))
    response.unlink()
    monkeypatch.setattr(repair_suite, 'generate_api', lambda *a, **k: pytest.fail('恢复不应调用模型'))
    recover = getattr(repair_suite, 'recover_suite', None)
    assert callable(recover), '计划缺少离线响应恢复入口'
    state = recover(folder)
    assert state['status'] == 'planned'
    assert state['plan']['origin']['model'] == '审阅的外部模型'
    assert state['plan']['origin']['recovered_at']
    assert repair_suite.read_suite(folder)['plan_sha256'] == state['plan_sha256']
    assert not (folder / 'reports').exists()
    original = (folder / 'plan.json').read_bytes()
    with pytest.raises(ValueError, match='已经存在'):
        recover(folder)
    assert (folder / 'plan.json').read_bytes() == original


@pytest.mark.parametrize('changed', ['response', 'prompt', 'source', 'mode', 'origin', 'runtime'])
def test_recovery_refuses_changed_saved_response_or_inputs(prepared_suite, monkeypatch, changed):
    folder, response = prepared_suite
    with monkeypatch.context() as failure:
        fail_publication(failure)
        with pytest.raises(OSError):
            repair_suite.generate_suite(folder, response_file=response)
    recover = getattr(repair_suite, 'recover_suite', None)
    assert callable(recover), '计划缺少离线响应恢复入口'
    if changed == 'response':
        (folder / 'response.json').write_text('{}')
    elif changed == 'prompt':
        (folder / 'prompt.txt').write_text('changed prompt')
    elif changed == 'source':
        (folder / 'inputs/after/sample.py').write_text('changed source')
    elif changed == 'mode':
        path = folder / 'inputs/after/sample.py'
        path.chmod(path.stat().st_mode ^ 0o100)
    elif changed == 'origin':
        path = folder / 'response-record.json'
        record = json.loads(path.read_text())
        record['origin'] = {}
        path.write_text(json.dumps(record))
    else:
        path = folder / 'suite.json'
        record = json.loads(path.read_text())
        record['runtime']['executable'] = '/another/python'
        path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        recover(folder)
    assert not (folder / 'plan.json').exists()


def test_unrecorded_legacy_response_requires_explicit_import(prepared_suite):
    folder, response = prepared_suite
    (folder / 'response.json').write_bytes(response.read_bytes())
    recover = getattr(repair_suite, 'recover_suite', None)
    assert callable(recover), '计划缺少离线响应恢复入口'
    with pytest.raises(ValueError, match='导入|import'):
        recover(folder)
    assert repair_suite.generate_suite(folder, response_file=response)['status'] == 'planned'


def test_recovery_preserves_partial_publication_left_by_an_interruption(prepared_suite, monkeypatch):
    folder, response = prepared_suite
    with monkeypatch.context() as failure:
        fail_publication(failure)
        with pytest.raises(OSError):
            repair_suite.generate_suite(folder, response_file=response)
    (folder / 'tests').mkdir()
    (folder / 'tests/partial.py').write_text('preserve unfinished test')
    (folder / 'cases').mkdir()
    (folder / 'cases/partial.json').write_text('preserve unfinished case')
    (folder / 'plan.json').write_text('incomplete plan marker')
    state = repair_suite.recover_suite(folder)
    assert state['status'] == 'planned'
    preserved = folder / state['recovery_artifacts']
    assert (preserved / 'tests/partial.py').read_text() == 'preserve unfinished test'
    assert (preserved / 'cases/partial.json').read_text() == 'preserve unfinished case'
    assert (preserved / 'plan.json').read_text() == 'incomplete plan marker'
    assert repair_suite.read_suite(folder)['plan']['checks'][0]['id'] == 'repair'


def test_failed_recovery_restores_interrupted_artifacts_byte_for_byte(prepared_suite, monkeypatch):
    folder, response = prepared_suite
    with monkeypatch.context() as failure:
        fail_publication(failure)
        with pytest.raises(OSError):
            repair_suite.generate_suite(folder, response_file=response)
    originals = {'tests/test_partial.py': b'unfinished test\n', 'cases/partial.json': b'{unfinished',
                 'plan.json': b'incomplete plan'}
    for name, content in originals.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    with monkeypatch.context() as failure:
        fail_publication(failure)
        with pytest.raises(OSError, match='disk failure'):
            repair_suite.recover_suite(folder)
    assert {name: (folder / name).read_bytes() for name in originals} == originals
    assert not list(folder.glob('.incomplete-*'))


@pytest.mark.parametrize('change_mode', [False, True])
def test_inputs_changed_during_model_request_never_publish_a_plan(prepared_suite, monkeypatch, change_mode):
    folder, response = prepared_suite

    def changed(*args, **kwargs):
        path = folder / 'inputs/after/sample.py'
        if change_mode:
            path.chmod(path.stat().st_mode ^ 0o100)
        else:
            path.write_text('def compute(): return 100\n')
        return response.read_text(), {'provider': 'external', 'model': 'teaching'}

    monkeypatch.setattr(repair_suite, 'generate_api', changed)
    with pytest.raises(ValueError, match='变化|一致'):
        repair_suite.generate_suite(folder)
    assert not any((folder / name).exists() for name in ('plan.json', 'cases', 'tests'))
