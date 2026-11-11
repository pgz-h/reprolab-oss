import json
from pathlib import Path
from reprolab.runner import run_case
from test_runner import make_case


def test_regression_checks_compare_the_old_baseline_instead_of_requiring_a_bug(tmp_path):
    for index, (before, after, status) in enumerate([
        ('return 42', 'return 42', 'regression_passed'),
        ('return 42', 'return 0', 'regression_detected'),
        ('return 0', 'return 0', 'baseline_failure'),
        ('return 0', 'return 42', 'improved'),
    ]):
        folder = tmp_path / str(index)
        folder.mkdir()
        case = make_case(folder, before, after)
        data = json.loads(case.read_text())
        data.update(check_mode='regression', expected_failure=None)
        case.write_text(json.dumps(data))
        result = run_case(case, tmp_path / 'reports')
        assert result['status'] == status
        assert result['check_mode'] == 'regression'
        assert result['expected_failure'] is None
        assert Path(result['report_dir'], 'report.html').exists()


def _plan_response():
    checks = []
    for identity, role, code, expected in [
        ('repair', 'repair', 'from sample import compute\ndef test_fix(): assert compute() == 42\n', {'type':'ValueError','message_contains':'reported bug'}),
        ('guard', 'regression', 'from sample import legacy\ndef test_legacy(): assert legacy() == 1\n', None),
    ]:
        checks.append({'id':identity,'title':identity,'role':role,'test_code':code,'test_target':None,
                       'expected_failure':expected,'purpose':'检查真实行为','expected_behavior':'满足断言','assumptions':['需要用户审阅']})
    return {'title':'修复与回归保护','overview':'同时确认目标修复及相关行为','checks':checks}


def test_deeply_nested_imported_ai_plan_is_a_readable_error(tmp_path):
    import pytest
    from test_git_source import prepare_repo
    from reprolab.repair_suite import prepare_suite, generate_suite

    repo, before, after, _ = prepare_repo(tmp_path)
    state = prepare_suite(repo, before, after, '修复 compute', tmp_path / 'suites')
    response = tmp_path / 'nested.json'
    response.write_text('[' * 10000 + '0' + ']' * 10000)
    with pytest.raises(ValueError, match='JSON|响应|计划'):
        generate_suite(state['directory'], response_file=response)
    assert not (Path(state['directory']) / 'plan.json').exists()


def test_ai_suite_keeps_a_new_regression_visible_and_replays_each_evidence(tmp_path):
    from test_git_source import prepare_repo, git
    from reprolab.repair_suite import prepare_suite, generate_suite, run_suite, prepare_explanation, explain_suite
    from reprolab.evidence import check_report
    from reprolab.recipes import run_saved_case
    from reprolab.workspace import WORKTREE
    import pytest
    repo, before, _, _ = prepare_repo(tmp_path)
    (repo / 'sample.py').write_text('def compute(): raise ValueError("reported bug")\ndef legacy(): return 1\n')
    git(repo,'add','.');git(repo,'commit','-m','known baseline')
    before = git(repo,'rev-parse','HEAD')
    (repo / 'sample.py').write_text('def compute(): return 42\ndef legacy(): return 0\n')
    state = prepare_suite(repo,before,WORKTREE,'修复compute且保留legacy',tmp_path/'suites')
    response = tmp_path/'response.json';response.write_text(json.dumps(_plan_response()))
    generated = generate_suite(state['directory'],response_file=response)
    assert generated['status']=='planned'
    assert not (tmp_path/'reports').exists()
    with pytest.raises(ValueError,match='审阅'):
        run_suite(state['directory'],tmp_path/'reports')
    result = run_suite(state['directory'],tmp_path/'reports',confirmed=True)
    assert result['status']=='attention' and result['kind']=='suite'
    assert result['counts']['verified']==1 and result['counts']['regression_detected']==1
    assert '新增失败' in Path(result['summary_dir'],'summary.html').read_text()
    guard = result['entries'][1]
    report_dir = (Path(result['summary_dir'])/guard['report']).parent
    assert check_report(report_dir)['status']=='consistent'
    replay = run_saved_case(report_dir/'evidence/case.json',tmp_path/'replay')
    assert replay['status']=='regression_detected'
    source = Path(result['summary_dir'],'summary.json');original=source.read_bytes()
    explanation=prepare_explanation(source)
    response.write_text(json.dumps({'based_on_status':'verified','summary':'bad','findings':[],'next_steps':[]}))
    with pytest.raises(ValueError,match='状态不一致'):
        explain_suite(explanation['directory'],response_file=response)
    response.write_text('```json\n' + json.dumps({'based_on_status':'attention','summary':'目标修复通过，相关功能新增失败','findings':[{'id':'2','comment':'先查看legacy的变化'}],'next_steps':['修复新增失败再重跑']}) + '\n```')
    explained=explain_suite(explanation['directory'],response_file=response)
    assert explained['based_on_status']=='attention' and source.read_bytes()==original


def test_moved_suite_opens_its_current_folder_and_saved_python(tmp_path, monkeypatch):
    from test_git_source import prepare_repo
    from test_workflow import make_environment
    from reprolab.repair_suite import prepare_suite, generate_suite, read_suite
    from test_desktop_creation import QApplication
    from reprolab.desktop_store import DesktopStore
    from reprolab.desktop_suite import AISuiteDialog
    repo,before,after,_=prepare_repo(tmp_path)
    python,_=make_environment(tmp_path,True)
    state=prepare_suite(repo,before,after,'修复compute',tmp_path/'suites',python_executable=str(python))
    response=tmp_path/'response.json'
    data=_plan_response();data['checks']=data['checks'][:1]
    response.write_text(json.dumps(data))
    generate_suite(state['directory'],response_file=response)
    moved=tmp_path/'moved'
    Path(state['directory']).rename(moved)
    assert read_suite(moved)['directory']==str(moved)
    app=QApplication.instance() or QApplication([])
    store=DesktopStore(tmp_path/'desktop')
    store.settings['python']=''
    dialog=AISuiteDialog(store)
    dialog.load_plan(moved)
    assert dialog.directory==str(moved)
    assert dialog.python_edit.text()==str(python)
    inspected=[]
    class EnvironmentPreview:
        def __init__(self,project,*args):
            inspected.append(Path(project))
        def exec(self):
            return 0
    monkeypatch.setattr('reprolab.desktop_suite.ProjectEnvironmentDialog',EnvironmentPreview)
    dialog.inspect_environment()
    assert inspected==[moved/'inputs/after']
    dialog.close();app.processEvents()


def test_explanation_rechecks_evidence_after_model_returns(tmp_path,monkeypatch):
    import pytest
    from reprolab import repair_suite
    summary=tmp_path/'summary.json'
    summary.write_text(json.dumps({'kind':'batch','status':'attention','counts':{'inconclusive':1},
        'entries':[{'title':'one','status':'inconclusive','explanation':'环境问题'}]}))
    prepared=repair_suite.prepare_explanation(summary)
    def changed(*args,**kwargs):
        data=json.loads(summary.read_text());data['status']='verified';summary.write_text(json.dumps(data))
        return json.dumps({'based_on_status':'attention','summary':'旧解释','findings':[],'next_steps':[]}),{'provider':'teaching','model':'teaching'}
    monkeypatch.setattr(repair_suite,'generate_api',changed)
    with pytest.raises(ValueError,match='变化'):
        repair_suite.explain_suite(prepared['directory'])
    assert not Path(prepared['directory'],'explanation.json').exists()
