"""Wire contracts use synthetic keys and local servers, never real providers."""
import json
import pytest
from reprolab.model_reasoning import reasoning_settings, reasoning_choices, validate_reasoning_map
from reprolab.ai import generate_api, load_api_config
from reprolab.desktop_store import generation_preview, check_preview
from test_ai_api import api_server


def config(tmp_path, url='https://gateway.example/v1', protocol='responses', model='model-a', option=None):
    data={'base_url':url,'protocol':protocol,'model':model,'api_key':'synthetic-reasoning-key','api_key_source':'profile'}
    if option is not None:data['model_reasoning']={model:option}
    p=tmp_path/'connection.json';p.write_text(json.dumps(data));return p


@pytest.mark.parametrize('protocol,adapter,effort,expected', [
    ('openai','openai','high',{'reasoning_effort':'high'}),
    ('responses','openai','max',{'reasoning':{'effort':'max'}}),
    ('anthropic','anthropic','medium',{'output_config':{'effort':'medium'}}),
    ('openai','deepseek','high',{'thinking':{'type':'enabled'},'reasoning_effort':'high'}),
    ('openai','deepseek','none',{'thinking':{'type':'disabled'}}),
    ('responses','deepseek','none',{'reasoning':{'effort':'none'}}),
    ('anthropic','deepseek','max',{'thinking':{'type':'enabled'},'output_config':{'effort':'max'}}),
    ('anthropic','deepseek','none',{'thinking':{'type':'disabled'}}),
    ('openai','glm','low',{'thinking':{'type':'enabled'},'reasoning_effort':'low'}),
    ('openai','thinking','enabled',{'thinking':{'type':'enabled'}}),
    ('openai','thinking','none',{'thinking':{'type':'disabled'}}),
])
def test_real_request_and_saved_origin_contain_exact_protocol_fields(tmp_path, protocol, adapter, effort, expected):
    if protocol=='responses':
        body={'status':'completed','output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'{"ok":true}'}]}]}
    elif protocol=='anthropic':
        body={'type':'message','role':'assistant','stop_reason':'end_turn','content':[{'type':'text','text':'{"ok":true}'}]}
    else:
        body={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
    with api_server(body) as (url,requests):
        path=config(tmp_path,url,protocol,option={'adapter':adapter,'effort':effort})
        content,origin=generate_api('Public test',tmp_path,config_file=path)
    assert json.loads(content)=={'ok':True}
    fields={key:requests[0]['data'][key] for key in ('reasoning','reasoning_effort','thinking','output_config') if key in requests[0]['data']}
    assert fields==expected
    assert origin['reasoning_request']['parameters']==expected
    assert origin['reasoning_request']['source']=='user_declared'
    assert json.loads((tmp_path/'provider.json').read_text())['reasoning_request']==origin['reasoning_request']
    assert 'synthetic-reasoning-key' not in (tmp_path/'provider.json').read_text()


@pytest.mark.parametrize('protocol',['openai','responses','anthropic'])
def test_default_and_legacy_configuration_omit_parameters(tmp_path,protocol):
    old=config(tmp_path,protocol=protocol)
    settings=load_api_config(old)
    assert settings['reasoning']['parameters']=={}
    assert settings['reasoning']['source']=='service_default'


@pytest.mark.parametrize('url,protocol,model,adapter,levels', [
 ('https://api.deepseek.com/v1','openai','deepseek-flash','deepseek',('none','low','high','max')),
 ('https://api.openai.com/v1','responses','gpt-6-astra','openai',('low','medium','high','xhigh','max')),
 ('https://api.openai.com/v1','openai','gpt-6-luna','openai',('none','low','medium','high','xhigh','max')),
 ('https://api.openai.com/v1','responses','gpt-5.2','openai',('none','low','medium','high','xhigh')),
 ('https://api.anthropic.com/v1','anthropic','claude-opus-4-6','anthropic',('low','medium','high','max')),
 ('https://api.anthropic.com/v1','anthropic','claude-opus-5-5','anthropic',('low','medium','high','xhigh','max')),
 ('https://open.bigmodel.cn/api/paas/v4','openai','glm-5.3','glm',('low','high','max')),
 ('https://api.z.ai/api/coding/paas/v4','openai','glm-5.2','glm',('none','high','max')),
 ('https://open.bigmodel.cn/api/paas/v4','openai','glm-4.7','thinking',('none','enabled')),
])
def test_documented_models_have_specific_levels(url,protocol,model,adapter,levels):
    assert reasoning_choices(url,protocol,model)[0]==levels
    selected=reasoning_settings(url,protocol,model,{model:{'adapter':'auto','effort':levels[-1]}})
    assert selected['adapter']==adapter and selected['source']=='documented_model'


@pytest.mark.parametrize('url,protocol,model,option', [
 ('https://proxy.example/v1','responses','gpt-6-astra',{'adapter':'auto','effort':'high'}),
 ('https://api.openai.com.proxy.example/v1','responses','gpt-6-astra',{'adapter':'auto','effort':'high'}),
 ('https://api.openai.com/v1','responses','gpt-6-astra',{'adapter':'auto','effort':'none'}),
 ('https://api.anthropic.com/v1','anthropic','claude-opus-4-6',{'adapter':'auto','effort':'xhigh'}),
 ('https://api.deepseek.com/v1','openai','deepseek-flash',{'adapter':'auto','effort':'medium'}),
 ('https://open.bigmodel.cn/api/paas/v4','openai','glm-5.3',{'adapter':'auto','effort':'none'}),
 ('https://proxy.example/v1','anthropic','alias',{'adapter':'openai','effort':'high'}),
])
def test_unsupported_or_unknown_capabilities_stop_before_request(url,protocol,model,option):
    with pytest.raises(ValueError,match='思考强度'):
        reasoning_settings(url,protocol,model,{model:option})


@pytest.mark.parametrize('bad',[None,[],{'a':{}},{'a':{'adapter':[],'effort':'high'}}, {'a':{'adapter':'openai','effort':True}}, {'a':{'adapter':'openai','effort':'ultra'}},{'a':{'adapter':'openai','effort':'high','extra':True}}, {'a\n':{'adapter':'auto','effort':'default'}}])
def test_invalid_profile_shape_is_rejected(bad):
    with pytest.raises(ValueError):validate_reasoning_map(bad)


def test_model_override_selects_only_its_own_settings(tmp_path):
    p=config(tmp_path,option={'adapter':'openai','effort':'high'})
    data=json.loads(p.read_text());data['model_reasoning']['model-b']={'adapter':'openai','effort':'low'};p.write_text(json.dumps(data))
    assert load_api_config(p)['reasoning']['effort']=='high'
    assert load_api_config(p,model='model-b')['reasoning']['effort']=='low'
    assert load_api_config(p,model='model-c')['reasoning']['effort']=='default'


def test_preview_shows_effort_and_change_requires_new_consent(tmp_path):
    (tmp_path/'prompt.txt').write_text('Public prompt')
    p=config(tmp_path,option={'adapter':'openai','effort':'high'})
    preview=generation_preview(tmp_path,p)
    assert 'high' in preview['reasoning_description']
    assert preview['reasoning']['parameters']=={'reasoning':{'effort':'high'}}
    data=json.loads(p.read_text());data['model_reasoning']['model-a']['effort']='low';p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='重新预览'):check_preview(tmp_path,p,preview)


def test_rejected_effort_does_not_retry_without_it_or_save_success(tmp_path):
    with api_server({'error':'synthetic-reasoning-key'},400) as (url,requests):
        p=config(tmp_path,url,option={'adapter':'openai','effort':'high'})
        with pytest.raises(ValueError,match='思考参数') as exc:generate_api('Public test',tmp_path,config_file=p)
    assert len(requests)==1 and 'synthetic-reasoning-key' not in str(exc.value)
    assert not (tmp_path/'provider.json').exists()


@pytest.mark.parametrize('model,levels', [
 ('gpt-6.1-sol', ('low','medium','high','xhigh','max')),
 ('gpt-6-sol', ('none','low','medium','high','xhigh','max')),
 ('gpt-5.6-luna', ('none','low','medium','high','xhigh','max')),
 ('gpt-5.4', ('none','low','medium','high','xhigh')),
 ('gpt-5.4-2026-03-05', ('none','low','medium','high','xhigh')),
])
def test_additional_documented_gpt_aliases(model,levels):
    assert reasoning_choices('https://api.openai.com/v1','responses',model)[0]==levels
