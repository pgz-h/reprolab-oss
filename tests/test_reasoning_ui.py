"""Model-card workflows, including real saved selectors; synthetic credentials only."""
import json
from pathlib import Path
import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QLineEdit
from test_model_onboarding import dialog,fill,wait
from reprolab.desktop_models import ModelConnectionDialog,SavedModelSelector
from reprolab.ai import load_api_config


def setup_card(dialog,model='gateway/custom-model'):
    fill(dialog,'https://gateway.example/v1');dialog.model_edit.setText(model);dialog.add_model()
    card=dialog.cards.cards[-1];card.expand_button.setChecked(True);return card


def test_custom_card_defines_levels_without_official_model_gate(dialog,tmp_path):
    card=setup_card(dialog)
    card.name_edit.setText('自定义模型');card.context_edit.setText('1M');card.output_edit.setText('384K')
    card.image_check.setChecked(True);card.reasoning_check.setChecked(True)
    for _ in range(3):card.add_level_button.click()
    assert [r.level.currentData() for r in card.rows]==['off','low','medium','high','xhigh','max','minimal']
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    assert all(row.wire.height()>=34 and row.level.height()>=34 for row in card.rows)
    next(row for row in card.rows if row.level.currentData()=='high').wire.setText('gateway-deep')
    dialog.save_connection();path=Path(dialog.result_data['config']);before=path.read_bytes()
    data=json.loads(before);m=data['models'][0]
    assert m['context_window']==1000000 and m['max_tokens']==384000 and m['input']==['text','image']
    selector=SavedModelSelector(QLineEdit(str(path)),tmp_path/'selected')
    assert not selector.reasoning_editor.settings_button.isVisible()
    index=selector.reasoning_editor.effort_combo.findData('high');assert index>=0
    selector.reasoning_editor.effort_combo.setCurrentIndex(index)
    assert load_api_config(selector.path_edit.text())['reasoning']['parameters']=={'reasoning_effort':'gateway-deep'}
    assert path.read_bytes()==before
    reopened=ModelConnectionDialog(selector.path_edit.text(),dialog.output_dir)
    assert reopened.cards.cards[0].value()['reasoning_levels']['high']=='gateway-deep'
    reopened.close();selector.close()


def test_refresh_keeps_tuned_metadata_and_model_order(dialog):
    card=setup_card(dialog,'B');card.output_edit.setText('64K');card.reasoning_check.setChecked(True)
    dialog.model_edit.setText('A');dialog.add_model()
    original=dialog._models()
    dialog._choose_models({'models':[{'id':'A','name':'server-a','max_tokens':2000},{'id':'B','name':'server-b'},{'id':'C','name':'server-c'}],'partial':False},dialog._current_catalog_context())
    dialog.catalog_picker.select_all_button.click();dialog.catalog_picker.apply_button.click()
    assert [m['id'] for m in dialog._models()]==['B','A','C']
    assert dialog._models()[0]==original[0]
    assert dialog._models()[1]['name']=='A'


def test_card_reorder_and_delete_are_saved(dialog):
    setup_card(dialog,'first');dialog.model_edit.setText('second');dialog.add_model()
    assert dialog._selected_model()=='second'
    cards=dialog.cards;cards.move(cards.cards[1].identity,-1)
    assert dialog._selected_model()=='second'
    assert [m['id'] for m in dialog._models()]==['second','first']
    cards.cards[1].remove_button.click();dialog.save_connection()
    assert [m['id'] for m in json.loads(Path(dialog.result_data['config']).read_text())['models']]==['second']


def test_invalid_or_duplicate_card_keeps_draft_without_saving(dialog):
    card=setup_card(dialog);card.output_edit.setText('0');dialog.save_connection()
    assert dialog.result_data is None and '输出' in dialog.status_label.text()
    card.output_edit.setText('8K');dialog.cards.add_blank();dialog.cards.cards[-1].id_edit.setText('gateway/custom-model')
    dialog.save_connection();assert dialog.result_data is None and '重复' in dialog.status_label.text()


def test_incomplete_manual_row_does_not_block_fetch(dialog):
    from test_model_catalog import catalog_server
    with catalog_server() as (url,requests):
        fill(dialog,url+'/v1');dialog.model_edit.clear();dialog.cards.add_blank();dialog.fetch_models();wait(dialog)
        assert len(requests)==1 and dialog.catalog_picker is not None
        dialog.catalog_picker.reject()
        assert len(dialog.cards.cards)==1 and not dialog.cards.cards[0].id_edit.text()


def test_saved_legacy_effort_migrates_to_card_without_rewriting_original(dialog,tmp_path):
    p=tmp_path/'legacy.json';p.write_text(json.dumps({'base_url':'https://gateway.example/v1','model':'m','api_key':'synthetic-legacy-key','models':[{'id':'m','name':'M'}],'model_reasoning':{'m':{'adapter':'openai','effort':'high'}}}))
    raw=p.read_bytes();d=ModelConnectionDialog(p,tmp_path/'models')
    assert d.cards.cards[0].reasoning_check.isChecked()
    assert 'high' in d.cards.cards[0].value()['reasoning_levels']
    d.reject();assert p.read_bytes()==raw


def test_remove_active_level_resets_choice_explicitly(dialog,tmp_path):
    card=setup_card(dialog);card.reasoning_check.setChecked(True)
    dialog._reasoning_by_model['gateway/custom-model']={'adapter':'catalog','effort':'high'}
    row=next(r for r in card.rows if r.level.currentData()=='high');row.remove.click()
    assert not dialog._reasoning_by_model
    assert '恢复服务默认' in dialog.status_label.text()


def test_level_change_preserves_custom_value_and_duplicate_choices_are_disabled(dialog):
    card=setup_card(dialog);card.reasoning_check.setChecked(True)
    row=next(r for r in card.rows if r.level.currentData()=='medium')
    row.wire.setText('vendor-medium');row.level.setCurrentIndex(row.level.findData('xhigh'))
    assert row.wire.text()=='vendor-medium'
    assert not row.level.model().item(row.level.findData('high')).isEnabled()
