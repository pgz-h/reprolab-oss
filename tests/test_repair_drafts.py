import json
from copy import deepcopy
from pathlib import Path

import pytest
from reprolab import repair_drafts as drafts
from reprolab.repair_suggestions import message_suggestions
from test_project_failure import material


def state():
    inputs = ['problem', 'expected', 'allowed', '', ['test']]
    return {'inputs': inputs, 'preview_inputs': deepcopy(inputs), 'preview': 'hand edited', 'generated': 'generated'}


def test_roundtrip_private_file_and_allowlist(tmp_path):
    data=material(); data['runtime_values']={'TOKEN':'DO_NOT_SAVE'}; data['model_config']={'key':'DO_NOT_SAVE'}
    saved=drafts.save_draft(tmp_path, data, state())
    assert saved['state']==state()
    path=Path(saved['_path']); assert path.stat().st_mode & 0o777 == 0o600
    assert 'DO_NOT_SAVE' not in path.read_text()
    assert drafts.read_draft(path)==saved


def test_identity_isolates_project_report_and_test(tmp_path):
    original=material(); keys=[]
    for field in ('same', 'project', 'run', 'hash', 'test'):
        data=deepcopy(original)
        if field=='project': data['path_roots']['repository']='/different'
        if field=='run': data['reference']['run_id']='different'
        if field=='hash': data['reference']['report_sha256']='e'*64
        if field=='test': data['test_id']='test_other.py::test_other'
        keys.append(drafts.save_draft(tmp_path, data, state())['_path'])
    assert len(set(keys))==5


def test_conflicting_writer_and_discard_never_overwrite_or_delete_newer_work(tmp_path):
    data=material(); first=drafts.save_draft(tmp_path,data,state())
    changed=state(); changed['preview']='new edit'
    second=drafts.save_draft(tmp_path,data,changed,expected=first['_sha256'])
    with pytest.raises(ValueError,match='其他'):
        drafts.save_draft(tmp_path,data,state(),expected=first['_sha256'])
    with pytest.raises(ValueError,match='其他'):
        drafts.discard_draft(first['_path'],expected=first['_sha256'])
    drafts.discard_draft(second['_path'],expected=second['_sha256'])
    assert drafts.read_draft(second['_path']) is None


def test_failure_and_future_schema_preserve_previous_bytes(tmp_path,monkeypatch):
    saved=drafts.save_draft(tmp_path,material(),state()); path=Path(saved['_path']); before=path.read_bytes()
    def fail(*args): raise OSError('controlled disk failure')
    monkeypatch.setattr(drafts,'atomic_write',fail)
    with pytest.raises(OSError): drafts.save_draft(tmp_path,material(),state(),expected=saved['_sha256'])
    assert path.read_bytes()==before
    data=json.loads(before); data['schema_version']=99; path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='版本'): drafts.read_draft(path)
    assert json.loads(path.read_text())['schema_version']==99


def test_links_and_oversize_are_rejected_without_following(tmp_path):
    target=tmp_path/'outside'; target.write_text('keep')
    link=drafts.draft_path(tmp_path, drafts.identity(material())); link.symlink_to(target)
    with pytest.raises(ValueError,match='链接'): drafts.save_draft(tmp_path,material(),state())
    assert target.read_text()=='keep'
    link.unlink(); too_big=state(); too_big['preview']='x'*(drafts.MAX_DRAFT_BYTES+1)
    with pytest.raises(ValueError,match='1 MiB'): drafts.save_draft(tmp_path,material(),too_big)
    assert not link.exists()


@pytest.mark.parametrize('message', ['/tmp/run-83932/output', 'failed at 2026-11-05T10:21:33', 'object at 0x123abc', 'request 998877665544', 'job 550e8400-e29b-41d4-a716-446655440000'])
def test_unstable_messages_are_not_automatically_proposed(message):
    assert message_suggestions(message)['candidates']==[]


def test_assertion_literals_are_only_suggestions():
    result=message_suggestions("assert '12.0' == '12.00'\n  /tmp/random-927389/test.py")
    assert result['candidates'][0]=='12.00'
    assert '建议' in result['notice']
