"""Exercise custom-provider listing and selection with synthetic local services."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

import pytest

SECRET = 'synthetic-directory-key'
LISTING = {'data': [{'id': 'first-model', 'name': 'First Model'},
                    {'id': 'gateway/second', 'display_name': 'Second Model'}]}


@contextmanager
def catalog_server(body=LISTING, status=200, delay=0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, method):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if method == 'POST' else None
            requests.append({'method': method, 'path': self.path, 'data': data,
                             'authorization': self.headers.get('Authorization'),
                             'api_key': self.headers.get('x-api-key'),
                             'version': self.headers.get('anthropic-version')})
            time.sleep(delay)
            answer = body if method == 'GET' else {
                'status': 'completed', 'model': data['model'],
                'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}],
            }
            self.send_response(status if method == 'GET' else 200)
            if status == 302:
                self.send_header('Location', '/redirected-with-key')
            self.end_headers()
            try:
                self.wfile.write(json.dumps(answer).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.respond('GET')

        def do_POST(self):
            self.respond('POST')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def connection(url, **changes):
    data = {'base_url': url, 'api_key': SECRET, 'protocol': 'responses',
            'api_key_source': 'profile'}
    data.update(changes)
    return data


@pytest.mark.parametrize('protocol,path,auth,expected', [
    ('openai', '/tenant/v1', 'bearer', '/tenant/v1/models'),
    ('openai', '/tenant/v1/chat/completions/', 'bearer', '/tenant/v1/models'),
    ('responses', '/v1/responses', 'bearer', '/v1/models'),
    ('anthropic', '/tenant', 'api-key', '/tenant/v1/models?limit=1000'),
    ('anthropic', '/tenant/v1/messages/', 'bearer', '/tenant/v1/models?limit=1000'),
])
def test_discovery_uses_protocol_path_and_credentials_before_a_model_is_selected(protocol, path, auth, expected):
    from reprolab.model_catalog import discover_models
    with catalog_server() as (url, requests):
        result = discover_models(connection(url + path, protocol=protocol, auth_type=auth))
    assert result['models'] == [{'id': 'first-model', 'name': 'First Model'},
                                {'id': 'gateway/second', 'name': 'Second Model'}]
    assert result['partial'] is False
    assert requests == [{'method': 'GET', 'path': expected, 'data': None,
                         'authorization': 'Bearer ' + SECRET if auth == 'bearer' else None,
                         'api_key': SECRET if auth == 'api-key' else None,
                         'version': '2023-06-01' if protocol == 'anthropic' else None}]
    assert SECRET not in json.dumps(result)


def test_directory_uses_gateway_alias_deduplicates_and_hides_secret_rows():
    from reprolab.model_catalog import discover_models
    body = {'models': {'route-alias': {'id': 'canonical-model', 'displayName': 'Gateway alias'},
                       'count': 3, 'bad': None, 'secret': {'name': SECRET},
                       'plain': {}}}
    with catalog_server(body) as (url, _):
        result = discover_models(connection(url + '/v1'))
    assert result['models'] == [{'id': 'route-alias', 'name': 'Gateway alias'},
                                {'id': 'plain', 'name': 'plain'}]
    with catalog_server({'data': [{'id': 'same'}, {'id': 'same'}, {'id': ''}, None], 'has_more': True}) as (url, _):
        result = discover_models(connection(url + '/v1'))
    assert result['models'] == [{'id': 'same', 'name': 'same'}]
    assert result['partial'] is True


@pytest.mark.parametrize('status', [401, 404, 302])
def test_listing_failure_has_no_error_body_or_credential_and_does_not_follow_redirect(status):
    from reprolab.model_catalog import discover_models
    with catalog_server({'error': SECRET, 'private': 'hidden-provider-detail'}, status) as (url, requests):
        with pytest.raises(ValueError) as exc:
            discover_models(connection(url + '/v1'))
    assert str(status) in str(exc.value)
    assert SECRET not in str(exc.value) and 'hidden-provider-detail' not in str(exc.value)
    assert len(requests) == 1


def test_empty_listing_is_distinct_from_malformed_and_oversized_response():
    from reprolab.model_catalog import discover_models
    with catalog_server({'data': []}) as (url, _):
        assert discover_models(connection(url + '/v1'))['models'] == []
    for body in [{'error': 'unrecognized'}, {'data': [], 'padding': 'x' * (4 * 1024 * 1024)}]:
        with catalog_server(body) as (url, _):
            with pytest.raises(ValueError):
                discover_models(connection(url + '/v1'))


@pytest.fixture
def app():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait_task(app, dialog):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + 10
    while dialog.task is not None and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert dialog.task is None
    if dialog.catalog_picker is not None:
        dialog.catalog_picker.select_all_button.click()
        dialog.catalog_picker.apply_button.click()
        app.processEvents()


def test_form_fetch_select_test_save_and_reopen_use_typed_key_and_model_id(app, tmp_path, monkeypatch):
    from reprolab.desktop_models import ModelConnectionDialog
    from reprolab.ai import generate_api
    monkeypatch.setenv('REPROLAB_API_KEY', 'unrelated-environment-key')
    with catalog_server() as (url, requests):
        dialog = ModelConnectionDialog(output_dir=tmp_path / 'profiles')
        dialog.display_name_edit.setText('教学网关')
        dialog.base_url_edit.setText(url + '/v1')
        dialog.protocol_combo.setCurrentIndex(dialog.protocol_combo.findData('responses'))
        dialog.api_key_edit.setText(SECRET)
        dialog.fetch_button.click()
        assert dialog.task is not None and not dialog.form_widget.isEnabled()
        wait_task(app, dialog)
        assert dialog.model_combo.count() == 2
        dialog.model_combo.setCurrentIndex(1)
        dialog.test_button.click()
        wait_task(app, dialog)
        assert '成功' in dialog.status_label.text()
        dialog.model_combo.setCurrentIndex(0)
        assert '连接成功' not in dialog.status_label.text()
        dialog.model_combo.setCurrentIndex(1)
        dialog.save_button.click()
        path = Path(dialog.result_data['config'])
        saved = json.loads(path.read_text())
        assert saved['model'] == 'gateway/second'
        assert saved['display_name'] == '教学网关' and saved['provider_id'].startswith('custom-')
        assert len(saved['models']) == 2 and path.stat().st_mode & 0o777 == 0o600
        assert requests[-1]['data']['model'] == 'gateway/second'
        assert all(item['authorization'] == 'Bearer ' + SECRET for item in requests)
        # Saving a directory must remain consumable through the actual model client.
        generate_api('Fixed public connection test', tmp_path, config_file=path)
        assert requests[-1]['data']['model'] == 'gateway/second'
        assert requests[-1]['authorization'] == 'Bearer ' + SECRET
    reopened = ModelConnectionDialog(path, tmp_path / 'profiles')
    assert reopened.model_combo.count() == 2
    assert reopened._connection_data()['model'] == 'gateway/second'
    assert reopened.display_name_edit.text() == '教学网关'
    assert not reopened.api_key_edit.text()
    assert reopened.service_combo.count() == 2
    reopened.close()
    dialog.close()
    app.processEvents()


def test_failed_refresh_preserves_configured_model_and_manual_fallback(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog
    with catalog_server({'error': SECRET}, 404) as (url, _):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(url + '/v1', model='manual/old')))
        original = path.read_bytes()
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert '404' in dialog.status_label.text()
        assert dialog._connection_data()['model'] == 'manual/old'
        dialog.model_edit.setText('manual/new')
        dialog.add_model_button.click()
        dialog.save_button.click()
        saved = json.loads(Path(dialog.result_data['config']).read_text())
        assert saved['model'] == 'manual/new'
        assert {model['id'] for model in saved['models']} == {'manual/old', 'manual/new'}
        assert path.read_bytes() == original
        dialog.close()
    app.processEvents()


def test_closing_discovery_does_not_apply_pending_models(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog
    with catalog_server(delay=0.2) as (url, requests):
        dialog = ModelConnectionDialog(output_dir=tmp_path / 'profiles')
        dialog.base_url_edit.setText(url + '/v1')
        dialog.api_key_edit.setText(SECRET)
        dialog.fetch_button.click()
        dialog.reject()
        wait_task(app, dialog)
        assert dialog.model_combo.count() == 0
        assert dialog.result_data is None and not (tmp_path / 'profiles').exists()
        assert len(requests) <= 1
    dialog.close()
    app.processEvents()


def test_repeated_service_snapshots_do_not_hide_another_saved_service(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog
    earlier = tmp_path / 'model-earlier.json'
    earlier.write_text(json.dumps(connection('https://example.invalid/v1', model='old-model',
                                             provider_id='service-b', display_name='Service B')))
    os.utime(earlier, (1, 1))
    for index in range(100):
        current = tmp_path / f'model-latest-{index}.json'
        current.write_text(json.dumps(connection('https://example.invalid/v1', model='new-model',
                                                 provider_id='service-a', display_name='Service A')))
    dialog = ModelConnectionDialog(current, tmp_path)
    assert dialog.service_combo.count() == 3
    older_index = next(index for index in range(dialog.service_combo.count())
                       if 'Service B' in dialog.service_combo.itemText(index))
    dialog.service_combo.setCurrentIndex(older_index)
    assert dialog._connection_data()['model'] == 'old-model'
    assert dialog.display_name_edit.text() == 'Service B'
    dialog.close()
    app.processEvents()


def test_changing_provider_origin_requires_explicit_consent_before_reusing_saved_key(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-only',
                                              models=[{'id': 'old-only', 'name': 'Old service model'}])))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.show()
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not requests, 'The old credential reached another service without confirmation'
        assert '确认' in dialog.status_label.text()
        assert dialog.reuse_key_check.isVisible()
        dialog.test_button.click()
        assert dialog.task is None and not requests
        dialog.save_button.click()
        assert dialog.result_data is None and not (tmp_path / 'profiles').exists()

        dialog.reuse_key_check.setChecked(True)
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer ' + SECRET
        assert {entry['id'] for entry in dialog._models()} == {'first-model', 'gateway/second'}
        assert dialog._selected_model() != 'old-only'
        dialog.close()
    app.processEvents()


def test_a_new_key_is_used_for_the_changed_service_without_carrying_its_old_catalog(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-only',
                                              models=[{'id': 'old-only', 'name': 'Old service model'}])))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.api_key_edit.setText('synthetic-new-key')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer synthetic-new-key'
        assert {entry['id'] for entry in dialog._models()} == {'first-model', 'gateway/second'}
        dialog.save_button.click()
        saved = json.loads(Path(dialog.result_data['config']).read_text())
        assert saved['api_key'] == 'synthetic-new-key' and SECRET not in json.dumps(saved)
        dialog.close()
    app.processEvents()


def test_a_new_environment_key_is_used_for_the_changed_service(app, tmp_path, monkeypatch):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_OTHER_SERVICE_KEY', 'synthetic-other-env-key')
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-only')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.api_key_env_edit.setText('REPROLAB_OTHER_SERVICE_KEY')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer synthetic-other-env-key'
        dialog.save_button.click()
        saved = json.loads(Path(dialog.result_data['config']).read_text())
        assert saved['api_key_source'] == 'environment'
        assert saved['api_key_env'] == 'REPROLAB_OTHER_SERVICE_KEY'
        assert 'api_key' not in saved
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('credential', ['typed', 'environment'])
def test_new_credentials_on_same_service_do_not_merge_previous_model_catalog(app, tmp_path, monkeypatch, credential):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_OTHER_SERVICE_KEY', 'synthetic-other-env-key')
    with catalog_server() as (url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(url + '/v1', model='old-only',
                                              models=[{'id': 'old-only', 'name': 'Previous account model'}])))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        if credential == 'typed':
            dialog.api_key_edit.setText('synthetic-new-key')
        else:
            dialog.api_key_env_edit.setText('REPROLAB_OTHER_SERVICE_KEY')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        expected_key = 'synthetic-new-key' if credential == 'typed' else 'synthetic-other-env-key'
        assert requests[0]['authorization'] == 'Bearer ' + expected_key
        assert {entry['id'] for entry in dialog._models()} == {'first-model', 'gateway/second'}
        dialog.close()
    app.processEvents()


def test_new_key_on_same_service_requires_model_confirmation_before_testing(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(url + '/v1', model='account-a-only')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.api_key_edit.setText('synthetic-new-key')
        dialog.test_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not requests, 'An old account model was tested with a new key without confirmation'
        assert '模型' in dialog.status_label.text() and '添加' in dialog.status_label.text()
        dialog.add_model_button.click()
        dialog.test_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer synthetic-new-key'
        assert requests[0]['data']['model'] == 'account-a-only'
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('credential', ['typed', 'environment'])
def test_new_credentials_are_bound_to_the_service_where_they_were_entered(app, tmp_path, monkeypatch, credential):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_SERVICE_B_TEST_KEY', 'synthetic-service-b-key')
    with catalog_server() as (old_url, _), catalog_server() as (new_url, new_requests), catalog_server() as (third_url, third_requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-only')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.show()
        dialog.base_url_edit.setText(new_url + '/v1')
        if credential == 'typed':
            dialog.api_key_edit.setText('synthetic-service-b-key')
        else:
            dialog.api_key_env_edit.setText('REPROLAB_SERVICE_B_TEST_KEY')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert new_requests[0]['authorization'] == 'Bearer synthetic-service-b-key'

        dialog.base_url_edit.setText(third_url + '/v1')
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not third_requests, 'The second service credential reached a third service without confirmation'
        assert dialog.reuse_key_check.isVisible() and not dialog.reuse_key_check.isChecked()
        dialog.reuse_key_check.setChecked(True)
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert third_requests[0]['authorization'] == 'Bearer synthetic-service-b-key'
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('edit', ['trailing_space', 'clear_and_retype'])
def test_reediting_the_same_saved_environment_key_does_not_approve_a_new_origin(app, tmp_path, monkeypatch, edit):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_SAVED_ENV_KEY', 'synthetic-saved-env-key')
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        data = connection(old_url + '/v1', model='old-model', api_key_source='environment',
                          api_key_env='REPROLAB_SAVED_ENV_KEY')
        data.pop('api_key')
        path.write_text(json.dumps(data))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        if edit == 'trailing_space':
            dialog.api_key_env_edit.setText('REPROLAB_SAVED_ENV_KEY ')
        else:
            dialog.api_key_env_edit.clear()
            dialog.api_key_env_edit.setText('REPROLAB_SAVED_ENV_KEY')
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not requests, 'Re-entering the same environment key bypassed confirmation'
        assert '确认' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


def test_adding_whitespace_does_not_rebind_a_previously_entered_key(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (first_url, first_requests), catalog_server() as (second_url, second_requests):
        dialog = ModelConnectionDialog(output_dir=tmp_path / 'profiles')
        dialog.base_url_edit.setText(first_url + '/v1')
        dialog.api_key_edit.setText('synthetic-first-key')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert first_requests[0]['authorization'] == 'Bearer synthetic-first-key'
        dialog.base_url_edit.setText(second_url + '/v1')
        dialog.api_key_edit.setText('synthetic-first-key ')
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not second_requests, 'Whitespace allowed the same key to be sent to a different service'
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('reuse', ['typed_again', 'different_environment_name'])
def test_same_secret_in_another_field_still_requires_consent_for_a_new_service(app, tmp_path, monkeypatch, reuse):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_ALIAS_TEST_KEY', SECRET)
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        if reuse == 'typed_again':
            dialog.api_key_edit.setText(SECRET)
        else:
            dialog.api_key_env_edit.setText('REPROLAB_ALIAS_TEST_KEY')
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not requests, 'The same secret was sent under a different input source without confirmation'
        assert '确认' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


def test_switching_saved_services_does_not_forget_a_previously_seen_key(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        old = profiles / 'model-old.json'
        old.write_text(json.dumps(connection(old_url + '/v1', model='old-model',
                                             provider_id='old-service', display_name='Old service')))
        new = profiles / 'model-new.json'
        new.write_text(json.dumps(connection(new_url + '/v1', model='new-model', api_key='synthetic-new-key',
                                             provider_id='new-service', display_name='New service')))
        dialog = ModelConnectionDialog(old, profiles)
        index = next(i for i in range(dialog.service_combo.count()) if 'New service' in dialog.service_combo.itemText(i))
        dialog.service_combo.setCurrentIndex(index)
        dialog.api_key_edit.setText(SECRET)
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not requests, 'Switching profiles forgot that this key belonged to another service'
        assert '确认' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


def test_a_saved_service_with_the_same_key_remains_usable(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        old = profiles / 'model-old.json'
        old.write_text(json.dumps(connection(old_url + '/v1', model='old-model',
                                             provider_id='old-service', display_name='Old service')))
        new = profiles / 'model-new.json'
        new.write_text(json.dumps(connection(new_url + '/v1', model='new-model',
                                             provider_id='new-service', display_name='New service')))
        dialog = ModelConnectionDialog(old, profiles)
        index = next(i for i in range(dialog.service_combo.count()) if 'New service' in dialog.service_combo.itemText(i))
        dialog.service_combo.setCurrentIndex(index)
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer ' + SECRET
        dialog.close()
    app.processEvents()


def test_legacy_service_does_not_implicitly_approve_another_services_environment_key(app, tmp_path, monkeypatch):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_API_KEY', SECRET)
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        old = profiles / 'model-old.json'
        old.write_text(json.dumps(connection(old_url + '/v1', model='old-model',
                                             provider_id='old-service', display_name='Old service')))
        new = profiles / 'model-new.json'
        new_data = connection(new_url + '/v1', model='new-model', api_key='synthetic-other-key',
                              provider_id='new-service', display_name='New service')
        new_data.pop('api_key_source')
        new.write_text(json.dumps(new_data))
        dialog = ModelConnectionDialog(old, profiles)
        index = next(i for i in range(dialog.service_combo.count()) if 'New service' in dialog.service_combo.itemText(i))
        dialog.service_combo.setCurrentIndex(index)
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not requests, 'A legacy fallback implicitly approved a previously seen key for a new service'
        assert '确认' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


def test_legacy_default_environment_key_is_not_approved_with_another_environment_key(app, tmp_path, monkeypatch):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_ORIGINAL_ENV', 'synthetic-original-env')
    monkeypatch.setenv('REPROLAB_API_KEY', 'synthetic-default-env')
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model', api_key_source='legacy',
                                              api_key_env='REPROLAB_ORIGINAL_ENV')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.reuse_key_check.setChecked(True)
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer synthetic-original-env'
        dialog.api_key_env_edit.clear()
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert len(requests) == 1, 'A different default environment key was sent without confirmation'
        assert '确认' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


def test_corrupted_profile_during_switch_never_reuses_another_services_key(app, tmp_path, monkeypatch):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_ORIGINAL_TEST_KEY', 'synthetic-original-env-key')
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        old = profiles / 'model-old.json'
        old_data = connection(old_url + '/v1', model='old-model', provider_id='old-service',
                              display_name='Old service', api_key_source='environment',
                              api_key_env='REPROLAB_ORIGINAL_TEST_KEY')
        old_data.pop('api_key')
        old.write_text(json.dumps(old_data))
        new = profiles / 'model-new.json'
        new_data = connection(new_url + '/v1', model='new-model', provider_id='new-service',
                              display_name='New service')
        new.write_text(json.dumps(new_data))
        dialog = ModelConnectionDialog(old, profiles)
        new_index = next(i for i in range(dialog.service_combo.count()) if 'New service' in dialog.service_combo.itemText(i))
        new_data['api_key_env'] = 0
        new.write_text(json.dumps(new_data))
        dialog.service_combo.setCurrentIndex(new_index)
        assert '无法读取' in dialog.status_label.text()
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not requests, 'A malformed new profile forwarded the previous service credential'
        assert not dialog.api_key_env_edit.text() and not dialog.base_url_edit.text()
        dialog.close()
    app.processEvents()


def test_unreadable_profiles_do_not_prevent_other_services_from_opening(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    (tmp_path / 'model-dangling.json').symlink_to(tmp_path / 'deleted.json')
    (tmp_path / 'model-too-deep.json').write_text('[' * 10000 + '0' + ']' * 10000)
    valid = tmp_path / 'model-valid.json'
    valid.write_text(json.dumps(connection('https://example.invalid/v1', model='usable',
                                           provider_id='usable-service', display_name='Usable')))
    try:
        dialog = ModelConnectionDialog(valid, tmp_path)
    except (OSError, RecursionError) as exc:
        pytest.fail(f'An invalid profile prevented the connection dialog from opening: {type(exc).__name__}')
    assert dialog._connection_data()['model'] == 'usable'
    assert dialog.service_combo.count() == 2
    dialog.close()
    app.processEvents()


def test_unreadable_current_profile_is_reported_in_the_dialog(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    current = tmp_path / 'model-corrupt.json'
    current.write_text('{broken json')
    dialog = ModelConnectionDialog(current, tmp_path)
    assert '无法读取' in dialog.status_label.text()
    assert not dialog.api_key_edit.text()
    dialog.close()
    app.processEvents()


def test_legacy_profile_with_null_optional_labels_is_still_usable(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    path = tmp_path / 'old.json'
    path.write_text(json.dumps(connection('https://example.invalid/v1', model='old-model',
                                          provider_id=None, display_name=None)))
    dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
    assert dialog._connection_data()['model'] == 'old-model'
    dialog.save_button.click()
    saved = json.loads(Path(dialog.result_data['config']).read_text())
    assert saved['model'] == 'old-model' and saved['provider_id'].startswith('custom-')
    dialog.close()
    app.processEvents()


def test_approval_does_not_follow_a_further_change_of_service_origin(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server() as (new_url, _), catalog_server() as (third_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.reuse_key_check.setChecked(True)
        dialog.base_url_edit.setText(third_url + '/v1')
        assert not dialog.reuse_key_check.isChecked()
        dialog.fetch_button.click()
        assert dialog.task is None and not requests
        dialog.close()
    app.processEvents()


def test_approved_key_can_use_another_path_on_the_same_new_service(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.reuse_key_check.setChecked(True)
        dialog.fetch_button.click()
        wait_task(app, dialog)
        dialog.base_url_edit.setText(new_url + '/tenant/v1')
        assert dialog.reuse_key_check.isHidden()
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert len(requests) == 2 and requests[-1]['path'] == '/tenant/v1/models'
        dialog.close()
    app.processEvents()


def test_approved_saved_environment_key_can_be_used_again_on_the_same_service(app, tmp_path, monkeypatch):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_SAVED_ENV_KEY', 'synthetic-saved-env-key')
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        data = connection(old_url + '/v1', model='old-model', api_key_source='environment',
                          api_key_env='REPROLAB_SAVED_ENV_KEY')
        data.pop('api_key')
        path.write_text(json.dumps(data))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.reuse_key_check.setChecked(True)
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer synthetic-saved-env-key'
        dialog.test_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert len(requests) == 2 and '成功' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('present', [True, False])
@pytest.mark.parametrize('has_fallback', [True, False])
def test_saved_environment_source_without_name_uses_only_the_default_variable(app, tmp_path, monkeypatch, present, has_fallback):
    from reprolab.desktop_models import ModelConnectionDialog

    if present:
        monkeypatch.setenv('REPROLAB_API_KEY', 'synthetic-default-env-key')
    else:
        monkeypatch.delenv('REPROLAB_API_KEY', raising=False)
    with catalog_server() as (url, requests):
        path = tmp_path / 'old.json'
        data = connection(url + '/v1', model='old-model', api_key_source='environment')
        if not has_fallback:
            data.pop('api_key')
        path.write_text(json.dumps(data))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.test_button.click()
        if dialog.task:
            wait_task(app, dialog)
        if present:
            assert requests[0]['authorization'] == 'Bearer synthetic-default-env-key'
            assert '成功' in dialog.status_label.text()
        else:
            assert not requests and 'REPROLAB_API_KEY' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('present', [True, False])
def test_legacy_profile_without_a_key_uses_the_default_environment_variable(app, tmp_path, monkeypatch, present):
    from reprolab.desktop_models import ModelConnectionDialog

    if present:
        monkeypatch.setenv('REPROLAB_API_KEY', 'synthetic-default-env-key')
    else:
        monkeypatch.delenv('REPROLAB_API_KEY', raising=False)
    with catalog_server() as (url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps({'base_url': url + '/v1', 'model': 'old-model', 'protocol': 'responses'}))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.test_button.click()
        if dialog.task:
            wait_task(app, dialog)
        if present:
            assert requests[0]['authorization'] == 'Bearer synthetic-default-env-key'
        else:
            assert not requests and 'REPROLAB_API_KEY' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('source', ['environment', 'legacy'])
def test_approving_an_environment_key_does_not_approve_the_saved_fallback_key(app, tmp_path, monkeypatch, source):
    from reprolab.desktop_models import ModelConnectionDialog

    monkeypatch.setenv('REPROLAB_SAVED_ENV_KEY', 'synthetic-saved-env-key')
    monkeypatch.delenv('REPROLAB_API_KEY', raising=False)
    with catalog_server() as (old_url, _), catalog_server() as (new_url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model', api_key_source=source,
                                              api_key_env='REPROLAB_SAVED_ENV_KEY')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.reuse_key_check.setChecked(True)
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer synthetic-saved-env-key'
        dialog.api_key_env_edit.clear()
        dialog.test_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert len(requests) == 1, 'Approving an environment key also sent a different saved key'
        assert '确认' in dialog.status_label.text()
        dialog.reuse_key_check.setChecked(True)
        dialog.add_model_button.click()
        dialog.test_button.click()
        wait_task(app, dialog)
        assert requests[-1]['authorization'] == 'Bearer ' + SECRET
        dialog.close()
    app.processEvents()


def test_key_entered_before_url_is_bound_to_the_first_service_used(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (first_url, first_requests), catalog_server() as (second_url, second_requests):
        dialog = ModelConnectionDialog(output_dir=tmp_path / 'profiles')
        dialog.api_key_edit.setText('synthetic-key-entered-first')
        dialog.base_url_edit.setText(first_url + '/v1')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert first_requests[0]['authorization'] == 'Bearer synthetic-key-entered-first'
        dialog.base_url_edit.setText(second_url + '/v1')
        dialog.fetch_button.click()
        if dialog.task:
            wait_task(app, dialog)
        assert not second_requests
        assert '确认' in dialog.status_label.text()
        dialog.close()
    app.processEvents()


def test_editing_only_the_path_on_the_same_service_does_not_require_key_consent(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (url, requests):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(url + '/v1', model='old-model')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(url + '/tenant/v1')
        assert dialog.reuse_key_check.isHidden()
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert requests[0]['authorization'] == 'Bearer ' + SECRET
        assert requests[0]['path'] == '/tenant/v1/models'
        dialog.close()
    app.processEvents()


def test_changed_service_marks_previous_catalog_as_stale_even_if_refresh_fails(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server({'error': 'unavailable'}, 404) as (new_url, _):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.api_key_edit.setText('synthetic-new-key')
        assert '先前' in dialog.catalog_status.text() or '旧' in dialog.catalog_status.text()
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert '404' in dialog.status_label.text()
        assert '先前' in dialog.catalog_status.text() or '旧' in dialog.catalog_status.text()
        dialog.close()
    app.processEvents()


def test_old_model_must_be_explicitly_added_before_saving_to_a_new_service(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server({'error': 'unavailable'}, 404) as (new_url, _):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-only')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.api_key_edit.setText('synthetic-new-key')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        dialog.save_button.click()
        assert dialog.result_data is None
        assert '模型' in dialog.status_label.text() and '添加' in dialog.status_label.text()
        dialog.add_model_button.click()
        dialog.save_button.click()
        saved = json.loads(Path(dialog.result_data['config']).read_text())
        assert saved['model'] == 'old-only' and saved['models'] == [{'id': 'old-only', 'name': 'old-only'}]
        dialog.close()
    app.processEvents()


def test_manual_model_typed_for_old_service_is_not_saved_as_new_service_model(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server() as (new_url, _):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.model_edit.setText('old-manual-unsaved')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.api_key_edit.setText('synthetic-new-key')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        dialog.save_button.click()
        saved = json.loads(Path(dialog.result_data['config']).read_text())
        assert saved['model'] != 'old-manual-unsaved'
        assert 'old-manual-unsaved' not in {model['id'] for model in saved['models']}
        dialog.close()
    app.processEvents()


@pytest.mark.parametrize('order', ['key_then_model', 'model_then_key'])
def test_manual_model_entered_for_new_service_survives_directory_fetch(app, tmp_path, order):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server() as (new_url, _):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-model')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        if order == 'key_then_model':
            dialog.api_key_edit.setText('synthetic-new-key')
            dialog.model_edit.setText('manual-for-new-service')
        else:
            dialog.model_edit.setText('manual-for-new-service')
            dialog.api_key_edit.setText('synthetic-new-key')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert dialog._selected_model() == 'manual-for-new-service'
        dialog.save_button.click()
        saved = json.loads(Path(dialog.result_data['config']).read_text())
        assert saved['model'] == 'manual-for-new-service'
        dialog.close()
    app.processEvents()


def test_empty_catalog_on_a_different_service_does_not_retain_old_models(app, tmp_path):
    from reprolab.desktop_models import ModelConnectionDialog

    with catalog_server() as (old_url, _), catalog_server({'data': []}) as (new_url, _):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps(connection(old_url + '/v1', model='old-only')))
        dialog = ModelConnectionDialog(path, tmp_path / 'profiles')
        dialog.base_url_edit.setText(new_url + '/v1')
        dialog.api_key_edit.setText('synthetic-new-key')
        dialog.fetch_button.click()
        wait_task(app, dialog)
        assert dialog.model_combo.count() == 0
        assert dialog._selected_model() != 'old-only'
        assert '空目录' in dialog.catalog_status.text()
        dialog.close()
    app.processEvents()
