import json
import pytest
from reprolab.model_definitions import parse_capacity,format_capacity,validate_catalog,changed_level_value,next_level
from reprolab.ai import generate_api
from test_ai_api import api_server

@pytest.mark.parametrize('text,number',[('1M',1000000),('384K',384000),('2.3M',2300000),('128000',128000),('',None)])
def test_decimal_capacity_roundtrip(text,number):
    assert parse_capacity(text)==number
    assert parse_capacity(format_capacity(number))==number

@pytest.mark.parametrize('bad',['0','-1','1.2','NaN','1GB','2.5e6','9999999999999999M'])
def test_bad_capacity_is_rejected(bad):
    with pytest.raises(ValueError):parse_capacity(bad)

@pytest.mark.parametrize('before,wire,after,expected',[('low','low','high','high'),('low','custom-medium','high','custom-medium'),('off',None,'low','low'),('high','custom-high','off',None)])
def test_upstream_level_value_transition(before,wire,after,expected):
    assert changed_level_value(before,wire,after)==expected


def test_upstream_add_level_order():
    used=['off','low','medium','high']
    for expected in ('xhigh','max','minimal'):
        assert next_level(used)==expected;used.append(expected)
    assert next_level(used) is None

@pytest.mark.parametrize('protocol,field,params,body',[
 ('responses','max_output_tokens',{'reasoning':{'effort':'gateway-deep'}},{'status':'completed','output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'{"ok":true}'}]}]}),
 ('openai','max_completion_tokens',{'reasoning_effort':'gateway-deep'},{'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}),
 ('anthropic','max_tokens',{'output_config':{'effort':'gateway-deep'}},{'type':'message','role':'assistant','stop_reason':'end_turn','content':[{'type':'text','text':'{"ok":true}'}]})])
def test_custom_alias_mapping_and_capacity_reach_actual_request(tmp_path,protocol,field,params,body):
    definition={'id':'gateway/custom-model','name':'My model','context_window':1000000,'max_tokens':384000,'input':['text','image'],'reasoning_levels':{'off':None,'high':'gateway-deep'}}
    with api_server(body) as (url,requests):
        p=tmp_path/'profile.json';p.write_text(json.dumps({'base_url':url,'api_key':'synthetic-declaration-key','protocol':protocol,'model':definition['id'],'models':[definition],'model_reasoning':{definition['id']:{'adapter':'catalog','effort':'high'}}}))
        _,origin=generate_api('Public text only',tmp_path,config_file=p)
    for key,value in params.items():assert requests[0]['data'][key]==value
    assert requests[0]['data'][field]==384000
    assert origin['model_definition']==definition
    assert origin['reasoning_request']['source']=='model_declaration'
    assert origin['reasoning_request']['wire_value']=='gateway-deep'

@pytest.mark.parametrize('bad',[{'reasoning_levels':{'off':None}}, {'reasoning_levels':{'high':''}}, {'context_window':'1M'}, {'input':[]}, {'reasoning_levels':{'off':'none','high':'high'}}])
def test_invalid_declarations_are_named(bad):
    with pytest.raises(ValueError,match='模型 1'):
        validate_catalog([{'id':'m','name':'M',**bad}])

def test_duplicates_stop_save():
    with pytest.raises(ValueError,match='重复'):validate_catalog([{'id':'m','name':'M'},{'id':'m','name':'M2'}])
