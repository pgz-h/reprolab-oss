"""Model onboarding uses synthetic credentials and local fixtures only."""
import json
import time

import pytest
pytest.importorskip('PySide6')

_APP = None
from test_ai_api import api_server


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.delenv('REPROLAB_API_KEY', raising=False)
    from PySide6.QtWidgets import QApplication
    from reprolab.desktop_models import ModelConnectionDialog
    global _APP
    _APP = app = QApplication.instance() or QApplication([])
    window = ModelConnectionDialog(output_dir=tmp_path / 'profiles')
    window.show()
    app.processEvents()
    yield window
    window.close()
    wait(window)
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def wait(window):
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + 5
    while window.task is not None and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.005)
    assert window.task is None
    QApplication.processEvents()


def fill(window, url):
    window.base_url_edit.setText(url)
    window.api_key_edit.setText('synthetic-onboarding-key')
    window.model_edit.setText('fixture-chat-model')


@pytest.mark.parametrize('identity,protocol', [('deepseek', 'openai'), ('openai', 'responses'), ('anthropic', 'anthropic')])
def test_preset_supplies_address_without_network_or_invented_models(dialog, identity, protocol, monkeypatch):
    from reprolab import desktop_models
    monkeypatch.setattr(desktop_models, 'discover_models', lambda *a, **kw: pytest.fail('Unexpected discovery'))
    monkeypatch.setattr(desktop_models, '_test_profile', lambda *a, **kw: pytest.fail('Unexpected generation'))
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData(identity))
    assert dialog.protocol_combo.currentData() == protocol
    assert dialog.base_url_edit.text().startswith('https://api.')
    assert not dialog.base_url_edit.isVisible()
    assert dialog.destination_label.isVisible()
    assert not dialog.api_key_edit.text()
    assert dialog.model_combo.count() == 0
    dialog.advanced_button.setChecked(True)
    assert dialog.base_url_edit.isVisible() and dialog.protocol_combo.isVisible()


def test_switching_preset_never_carries_previous_key_model_or_provider_identity(dialog):
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData('deepseek'))
    fill(dialog, 'https://api.deepseek.com/v1')
    dialog.add_model()
    previous_identity = dialog.provider_id_edit.text()
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData('anthropic'))
    assert not dialog.api_key_edit.text()
    assert not dialog.api_key_env_edit.text()
    assert not dialog._selected_model()
    assert not dialog._models()
    assert dialog.provider_id_edit.text() != previous_identity
    dialog.test_and_save()
    assert dialog.task is None and dialog.result_data is None
    assert '缺少' in dialog.status_label.text() or 'API Key' in dialog.status_label.text()


def test_custom_endpoint_edit_reveals_actual_protocol_and_retains_key_consent(dialog):
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData('deepseek'))
    fill(dialog, 'https://api.deepseek.com/v1')
    dialog.advanced_button.setChecked(True)
    dialog.base_url_edit.setText('https://gateway.example/v1')
    assert dialog.preset_combo.currentData() == 'custom'
    assert dialog.reuse_key_check.isVisible()
    with pytest.raises(ValueError, match='服务地址'):
        dialog._connection_data()


def test_test_and_save_uses_one_public_request_and_reopens_secret_free_summary(dialog):
    from reprolab.desktop_models import ModelConnectionDialog, connection_summary
    body = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
    with api_server(body) as (url, requests):
        fill(dialog, url)
        dialog.test_save_button.click()
        assert not dialog.form_widget.isEnabled()
        wait(dialog)
    assert len(requests) == 1
    assert 'public connection test' in json.dumps(requests[0]['data'])
    assert dialog.result_data is not None
    path = dialog.result_data['config']
    saved = json.loads(__import__('pathlib').Path(path).read_text())
    assert saved['models'] == [{'id': 'fixture-chat-model', 'name': 'fixture-chat-model'}]
    assert saved['api_key'] == 'synthetic-onboarding-key'
    summary = connection_summary(path)
    assert 'fixture-chat-model' in summary and 'synthetic-onboarding-key' not in summary
    assert str(dialog.output_dir) not in summary
    reopened = ModelConnectionDialog(path, dialog.output_dir)
    assert not reopened.api_key_edit.text()
    assert reopened._connection_data()['model'] == 'fixture-chat-model'
    reopened.close()


def test_test_failure_preserves_input_without_creating_profile(dialog):
    with api_server({'error': 'not authorized'}, 401) as (url, requests):
        fill(dialog, url)
        dialog.test_save_button.click()
        wait(dialog)
    assert len(requests) == 1 and dialog.result_data is None
    assert '401' in dialog.status_label.text()
    assert dialog.api_key_edit.text() == 'synthetic-onboarding-key'
    assert not list(dialog.output_dir.glob('model-*.json'))
    assert dialog.test_save_button.isEnabled()


def test_cancel_during_successful_test_does_not_save(dialog, monkeypatch):
    from reprolab import desktop_models
    import threading
    event = threading.Event()
    monkeypatch.setattr(desktop_models, '_test_profile', lambda data: event.wait(2))
    fill(dialog, 'https://example.invalid/v1')
    dialog.test_and_save()
    dialog.reject()
    event.set()
    wait(dialog)
    assert dialog.result_data is None
    assert not list(dialog.output_dir.glob('model-*.json'))


def test_successful_test_with_write_failure_stays_editable(dialog, monkeypatch):
    from reprolab import desktop_models
    monkeypatch.setattr(desktop_models, '_test_profile', lambda data: True)
    def fail(*args):
        raise OSError('synthetic write failure')
    monkeypatch.setattr(desktop_models, '_write_profile', fail)
    fill(dialog, 'https://example.invalid/v1')
    dialog.test_and_save()
    wait(dialog)
    assert dialog.result_data is None and '无法保存' in dialog.status_label.text()
    assert dialog.form_widget.isEnabled() and dialog.test_save_button.isEnabled()


def test_direct_save_manual_model_does_not_call_service(dialog, monkeypatch):
    from reprolab import desktop_models
    monkeypatch.setattr(desktop_models, '_test_profile', lambda *a: pytest.fail('Unexpected request'))
    fill(dialog, 'https://example.invalid/v1')
    dialog.save_button.click()
    assert dialog.result_data['model'] == 'fixture-chat-model'


def test_invalid_connection_summary_does_not_expose_path_or_secret(tmp_path):
    from reprolab.desktop_models import connection_summary
    path = tmp_path / 'private-file.json'
    path.write_text('{"api_key":"synthetic-summary-secret"}')
    assert connection_summary(None) == '未连接 · AI 可选'
    assert connection_summary(path) == '连接配置不可用 · 请重新连接或导入'


def model(mid):
    return {'id': mid, 'name': mid}


def test_catalog_multiselect_search_preserves_hidden_checks_and_cancel(dialog):
    from PySide6.QtCore import Qt
    dialog._choose_models({'models': [model('alpha'), model('beta'), model('gamma')], 'partial': False}, dialog._current_catalog_context())
    picker = dialog.catalog_picker
    assert picker is not None and len(picker.selected_models()) == 3
    picker.clear_button.click()
    picker.search_edit.setText('alpha')
    picker.select_all_button.click()
    picker.search_edit.setText('beta')
    picker.select_all_button.click()
    assert {m['id'] for m in picker.selected_models()} == {'alpha', 'beta'}
    assert '1 项被搜索隐藏' in picker.status_label.text()
    picker.clear_button.click()
    assert picker.selected_models() == [model('alpha')]
    picker.reject()
    assert dialog._models() == [] and dialog.catalog_picker is None
    assert dialog.result_data is None


def test_catalog_selection_removes_unchecked_models_retains_unlisted_and_default(dialog):
    from PySide6.QtCore import Qt
    fill(dialog, 'https://example.invalid/v1')
    dialog.add_model()
    context = dialog._current_catalog_context()
    dialog._choose_models({'models': [model('alpha'), model('beta')], 'partial': False}, context)
    picker = dialog.catalog_picker
    assert picker.selected_models() == [model('fixture-chat-model')]
    picker.select_all_button.click()
    picker.apply_button.click()
    assert len(dialog._models()) == 3
    assert dialog._selected_model() == 'fixture-chat-model'
    dialog.manage_models()
    picker = dialog.catalog_picker
    for i in range(picker.tree.topLevelItemCount()):
        item = picker.tree.topLevelItem(i)
        if item.data(0, Qt.ItemDataRole.UserRole)['id'] == 'fixture-chat-model':
            item.setCheckState(0, Qt.CheckState.Unchecked)
    picker.apply_button.click()
    dialog.save_connection()
    saved = json.loads(__import__('pathlib').Path(dialog.result_data['config']).read_text())
    assert {m['id'] for m in saved['models']} == {'alpha', 'beta'}
    assert saved['model'] in {'alpha', 'beta'}


def test_fetch_cancel_preserves_saved_catalog_and_never_calls_model(dialog):
    from test_model_catalog import catalog_server
    with catalog_server() as (url, requests):
        fill(dialog, url + '/v1')
        dialog.add_model()
        original = dialog._models()
        dialog.fetch_models()
        wait(dialog)
        assert dialog.catalog_picker is not None
        assert dialog._models() == original
        dialog.catalog_picker.select_all_button.click()
        dialog.catalog_picker.reject()
    assert dialog._models() == original
    assert [r['method'] for r in requests] == ['GET']


def test_new_service_catalog_does_not_preselect_previous_service_models(dialog):
    fill(dialog, 'https://first.invalid/v1')
    dialog.add_model()
    dialog.base_url_edit.setText('https://second.invalid/v1')
    dialog.api_key_edit.setText('different-synthetic-key')
    dialog._choose_models({'models': [model('new-model')], 'partial': False}, dialog._current_catalog_context())
    picker = dialog.catalog_picker
    assert picker.selected_models() == [model('new-model')]
    assert picker.tree.topLevelItemCount() == 1
    picker.select_all_button.click()
    picker.apply_button.click()
    assert dialog._models() == [model('new-model')]
    assert dialog._selected_model() == 'new-model'


def test_empty_selection_is_not_silently_repopulated(dialog):
    fill(dialog, 'https://example.invalid/v1')
    dialog.add_model()
    dialog.manage_models()
    picker = dialog.catalog_picker
    picker.clear_button.click()
    picker.apply_button.click()
    assert dialog._models() == [] and dialog._selected_model() == ''
    dialog.save_connection()
    assert dialog.result_data is None


def test_catalog_limit_blocks_update_without_silently_dropping_models(dialog):
    from reprolab.desktop_models import ModelCatalogDialog
    picker = ModelCatalogDialog([model(f'model-{i}') for i in range(1000)], [model('saved-unlisted')], parent=dialog)
    picker.select_all_button.click()
    assert len(picker.selected_models()) == 1001
    assert not picker.apply_button.isEnabled()
    picker.accept()
    assert picker.result() == 0
    picker.reject()


def test_saved_model_switch_updates_client_input_without_mutating_old_profile(dialog, tmp_path):
    from PySide6.QtWidgets import QLineEdit
    from reprolab.desktop_models import SavedModelSelector
    from reprolab.ai import load_api_config
    fill(dialog, 'https://example.invalid/v1')
    dialog.add_model()
    dialog.model_edit.setText('second-model')
    dialog.add_model()
    dialog.save_connection()
    original = __import__('pathlib').Path(dialog.result_data['config'])
    before = original.read_bytes()
    edit = QLineEdit(str(original))
    selector = SavedModelSelector(edit, tmp_path / 'switched')
    assert selector.combo.count() == 2
    index = selector.combo.findData('fixture-chat-model')
    selector.combo.setCurrentIndex(index)
    selector.combo.activated.emit(index)
    assert edit.text() != str(original)
    assert load_api_config(edit.text())['model'] == 'fixture-chat-model'
    assert original.read_bytes() == before
    assert 'synthetic-onboarding-key' not in selector.combo.currentText()
    selector.close()


def test_new_preset_never_inherits_ambient_key(dialog, monkeypatch):
    monkeypatch.setenv('REPROLAB_API_KEY', 'unrelated-environment-key')
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData('deepseek'))
    dialog.model_edit.setText('manual-model')
    with pytest.raises(ValueError, match='API Key'):
        dialog._connection_data()


@pytest.mark.parametrize('entry', ['settings', 'suite'])
def test_model_switch_is_used_by_real_ai_entry_points(dialog, tmp_path, entry):
    from PySide6.QtWidgets import QApplication
    from reprolab.desktop import MainWindow
    from reprolab.desktop_store import DesktopStore
    from reprolab.desktop_suite import AISuiteDialog
    from reprolab.ai import load_api_config
    fill(dialog, 'https://example.invalid/v1')
    dialog.add_model()
    dialog.model_edit.setText('second-model')
    dialog.add_model()
    dialog.save_connection()
    store = DesktopStore(tmp_path / 'state')
    store.settings.update({key: str(tmp_path / key) for key in ('cases', 'drafts', 'reports', 'imports')})
    store.settings['model_config'] = dialog.result_data['config']
    window = MainWindow(store) if entry == 'settings' else AISuiteDialog(store)
    index = window.model_selector.combo.findData('fixture-chat-model')
    window.model_selector.combo.setCurrentIndex(index)
    window.model_selector.combo.activated.emit(index)
    assert load_api_config(store.settings['model_config'])['model'] == 'fixture-chat-model'
    saved = json.loads((store.directory / 'settings.json').read_text())
    assert saved['model_config'] == store.settings['model_config']
    window.close()
    QApplication.processEvents()
