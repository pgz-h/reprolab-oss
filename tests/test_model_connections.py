"""Synthetic local providers and copied profiles; never use real model credentials."""
import json
import os
from pathlib import Path
import stat
import time

import pytest

from reprolab.ai import generate_api, load_api_config
from test_ai_api import api_server

SECRET = 'synthetic-model-connection-key'


@pytest.fixture(autouse=True)
def synthetic_credentials(monkeypatch):
    monkeypatch.setenv('REPROLAB_API_KEY', SECRET)


def profile(tmp_path, url, **changes):
    data = dict(base_url=url, model='requested-model', api_key=SECRET,
                protocol='responses', auth_type='bearer', json_mode=True, max_tokens=1024)
    data.update(changes)
    path = tmp_path / 'original.json'
    path.write_text(json.dumps(data))
    return path


def response_body(**changes):
    result = {
        'id': 'resp_fixture', 'object': 'response', 'status': 'completed',
        'error': None, 'incomplete_details': None, 'model': 'fixture-model',
        'output': [
            {'id': 'rs_fixture', 'type': 'reasoning', 'summary': []},
            {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
             'content': [{'type': 'output_text', 'text': '{"ok":', 'annotations': []},
                         {'type': 'output_text', 'text': 'true}', 'annotations': []}]},
        ],
        'usage': {'input_tokens': 4, 'output_tokens': 7, 'total_tokens': 11},
    }
    result.update(changes)
    return result


@pytest.mark.parametrize('base', ['https://example.invalid', 'https://example.invalid/v1',
                                   'https://example.invalid/v1/responses/'])
def test_responses_normalizes_endpoint_and_uses_completed_text(tmp_path, base):
    cfg = profile(tmp_path, base)
    assert load_api_config(cfg)['endpoint'] == 'https://example.invalid/v1/responses'
    with api_server(response_body()) as (url, requests):
        cfg = profile(tmp_path, url)
        content, origin = generate_api('Return JSON: {"ok":true}', tmp_path, config_file=cfg,
                                       system_prompt='Return only the requested JSON acceptance plan.')
    assert json.loads(content) == {'ok': True}
    request = requests[0]
    assert request['path'] == '/v1/responses'
    assert request['authorization'] == 'Bearer ' + SECRET
    assert request['data'] == {
        'model': 'requested-model', 'stream': False, 'max_output_tokens': 1024,
        'instructions': 'Return only the requested JSON acceptance plan.',
        'input': 'Return JSON: {"ok":true}', 'text': {'format': {'type': 'json_object'}},
    }
    assert origin['model'] == 'fixture-model'
    assert origin['usage'] == {'input_tokens': 4, 'output_tokens': 7, 'total_tokens': 11}
    assert SECRET not in (tmp_path / 'provider.json').read_text()


@pytest.mark.parametrize('failure', ['incomplete', 'failed', 'refusal', 'tool_call',
                                     'message_incomplete', 'secret_echo', 'malformed'])
def test_responses_never_persists_refused_incomplete_or_secret_output(tmp_path, failure):
    body = response_body()
    if failure == 'incomplete':
        body.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
    elif failure == 'failed':
        body.update(status='failed', error={'message': SECRET})
    elif failure == 'refusal':
        body['output'][1]['content'].append({'type': 'refusal', 'refusal': SECRET})
    elif failure == 'tool_call':
        body['output'].append({'type': 'function_call', 'name': SECRET, 'arguments': '{}'})
    elif failure == 'message_incomplete':
        body['output'][1]['status'] = 'incomplete'
    elif failure == 'secret_echo':
        body['output'][1]['content'][0]['text'] = SECRET
    else:
        body['output'] = [SECRET]
    with api_server(body) as (url, requests):
        cfg = profile(tmp_path, url)
        with pytest.raises(ValueError) as exc:
            generate_api('Fixed public JSON prompt', tmp_path, config_file=cfg)
    assert requests and SECRET not in str(exc.value)
    if failure == 'incomplete':
        assert 'max_output_tokens' in str(exc.value)
    assert not (tmp_path / 'provider.json').exists()


def test_responses_http_failure_hides_entire_body_and_does_not_follow_redirect(tmp_path):
    with api_server({'error': SECRET, 'detail': 'private-provider-detail'}, 302,
                    headers={'Location': 'https://unrequested.invalid/v1/responses'}) as (url, requests):
        with pytest.raises(ValueError, match='302') as exc:
            generate_api('Fixed public JSON prompt', tmp_path, config_file=profile(tmp_path, url))
    assert len(requests) == 1
    assert SECRET not in str(exc.value) and 'private-provider-detail' not in str(exc.value)
    assert not (tmp_path / 'provider.json').exists()


def test_connection_dialog_saves_private_copy_retaining_key_and_provider_options(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit
    from reprolab.desktop_models import ModelConnectionDialog
    app = QApplication.instance() or QApplication([])
    # A profile key must survive a metadata edit even when an environment key differs.
    monkeypatch.setenv('REPROLAB_API_KEY', 'unrelated-synthetic-env-key')
    cfg = profile(tmp_path, 'https://example.invalid/v1', protocol='anthropic',
                  auth_type='api-key', json_mode=False, max_tokens=4096)
    original = cfg.read_bytes()
    dialog = ModelConnectionDialog(config_path=cfg, output_dir=tmp_path / 'profiles')
    assert dialog.api_key_edit.echoMode() == QLineEdit.EchoMode.Password
    assert not dialog.api_key_edit.text()
    dialog.model_edit.setText('edited-model')
    dialog.save_connection()
    assert dialog.result() == QDialog.DialogCode.Accepted
    path = Path(dialog.result_data['config'])
    assert path.is_absolute() and path != cfg and path.parent == tmp_path / 'profiles'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved['api_key'] == SECRET and saved['model'] == 'edited-model'
    assert saved['protocol'] == 'anthropic' and saved['auth_type'] == 'api-key'
    assert saved['json_mode'] is False and saved['max_tokens'] == 4096
    assert cfg.read_bytes() == original
    assert dialog.result_data == {'config': str(path), 'model': 'edited-model', 'protocol': 'anthropic'}
    assert SECRET not in dialog.status_label.text() + dialog.api_key_edit.placeholderText()
    dialog.close()
    app.processEvents()


@pytest.mark.parametrize('close_while_running', [False, True])
def test_connection_test_runs_in_background_with_only_public_prompt(tmp_path, close_while_running):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from reprolab.desktop_models import ModelConnectionDialog
    app = QApplication.instance() or QApplication([])
    with api_server(response_body(), delay=0.2) as (url, requests):
        cfg = profile(tmp_path, url)
        dialog = ModelConnectionDialog(config_path=cfg, output_dir=tmp_path / 'profiles')
        dialog.test_connection()
        assert dialog.task is not None
        if close_while_running:
            dialog.reject()
        deadline = time.monotonic() + 10
        while dialog.task is not None and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert dialog.task is None
    assert len(requests) == 1
    assert requests[0]['data']['input'] == 'Return only this JSON object: {"ok":true}.'
    assert SECRET not in dialog.status_label.text()
    if not close_while_running:
        assert '成功' in dialog.status_label.text()
    assert not dialog.result_data and not (tmp_path / 'profiles').exists()
    dialog.close()
    app.processEvents()


def test_connection_probe_accepts_a_single_json_code_fence(monkeypatch):
    from reprolab import desktop_models
    monkeypatch.setattr(desktop_models, 'generate_api', lambda *args, **kwargs: ('```json\n{"ok":true}\n```', {}))
    assert desktop_models._test_profile({'base_url':'http://localhost:4444/v1', 'model':'teaching-model',
                                         'api_key':'teaching-key', 'protocol':'anthropic'}) is True


def test_switching_to_an_environment_key_does_not_copy_the_old_key_into_the_new_profile(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    from reprolab.desktop_models import ModelConnectionDialog

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('REPROLAB_NEW_PROVIDER_KEY', 'synthetic-environment-key')
    original = profile(tmp_path, 'https://example.invalid/v1', api_key_source='profile')
    original_bytes = original.read_bytes()
    dialog = ModelConnectionDialog(original, tmp_path / 'profiles')
    dialog.api_key_env_edit.setText('REPROLAB_NEW_PROVIDER_KEY')
    dialog.add_model_button.click()
    dialog.save_button.click()
    saved_path = Path(dialog.result_data['config'])
    saved = json.loads(saved_path.read_text())
    assert saved['api_key_source'] == 'environment'
    assert saved['api_key_env'] == 'REPROLAB_NEW_PROVIDER_KEY'
    assert 'api_key' not in saved and SECRET not in saved_path.read_text()
    assert load_api_config(saved_path)['api_key'] == 'synthetic-environment-key'
    assert original.read_bytes() == original_bytes
    dialog.close()
    app.processEvents()


def test_unchanged_legacy_environment_profile_keeps_its_existing_fallback_key(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    from reprolab.desktop_models import ModelConnectionDialog

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('REPROLAB_OLD_PROVIDER_KEY', 'synthetic-environment-key')
    original = profile(tmp_path, 'https://example.invalid/v1', api_key_source='environment',
                       api_key_env='REPROLAB_OLD_PROVIDER_KEY')
    dialog = ModelConnectionDialog(original, tmp_path / 'profiles')
    dialog.save_button.click()
    saved_path = Path(dialog.result_data['config'])
    saved = json.loads(saved_path.read_text())
    assert saved['api_key'] == SECRET and saved['api_key_source'] == 'environment'
    reopened = ModelConnectionDialog(saved_path, tmp_path / 'profiles')
    reopened.api_key_env_edit.clear()
    reopened.add_model_button.click()
    reopened.save_button.click()
    fallback = Path(reopened.result_data['config'])
    assert load_api_config(fallback)['api_key'] == SECRET
    reopened.close()
    dialog.close()
    app.processEvents()


def test_explicitly_reselecting_the_same_environment_name_switches_the_key_source(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    from reprolab.desktop_models import ModelConnectionDialog

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('REPROLAB_SAME_PROVIDER_KEY', 'synthetic-env-key')
    original = profile(tmp_path, 'https://example.invalid/v1', api_key_source='profile',
                       api_key_env='REPROLAB_SAME_PROVIDER_KEY')
    dialog = ModelConnectionDialog(original, tmp_path / 'profiles')
    dialog.api_key_env_edit.clear()
    dialog.api_key_env_edit.setText('REPROLAB_SAME_PROVIDER_KEY')
    dialog.add_model_button.click()
    dialog.save_button.click()
    saved_path = Path(dialog.result_data['config'])
    saved = json.loads(saved_path.read_text())
    assert saved['api_key_source'] == 'environment' and 'api_key' not in saved
    assert load_api_config(saved_path)['api_key'] == 'synthetic-env-key'
    dialog.close()
    app.processEvents()
