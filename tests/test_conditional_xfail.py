"""A platform-inapplicable xfail is not an executed expected failure."""
import json
from pathlib import Path
import pytest
from reprolab.runner import run_case
from test_runner import make_case
from test_git_source import git
from test_workflow import cli


@pytest.mark.parametrize('mark,blocked',[
    ('@pytest.mark.xfail(False, reason="other platform")',False),
    ('@pytest.mark.xfail(condition=False, reason="other platform")',False),
    ('@pytest.mark.xfail("False", reason="other platform")',False),
    ('@pytest.mark.xfail(True, reason="active")',True),
    ('@pytest.mark.xfail(True, reason="active", strict=True)',True),
    ('@pytest.mark.xfail(True, reason="not run", run=False)',True),
])
@pytest.mark.parametrize('flow',['strict','project'])
def test_effective_xfail_status_matches_pytest_without_turning_active_xfail_green(tmp_path,mark,blocked,flow):
    test='import pytest\nfrom sample import compute\n'+mark+'\ndef test_regression():\n    assert compute() == 42\n'
    if flow=='strict':
        case=make_case(tmp_path,test=test)
        report=run_case(case,tmp_path/'reports')
        assert report['status']==('inconclusive' if blocked else 'verified')
    else:
        repo=tmp_path/'repo';repo.mkdir()
        git(repo,'init','-q');git(repo,'config','user.name','test');git(repo,'config','user.email','test@example.invalid')
        (repo/'sample.py').write_text('def compute(): return 42\n');(repo/'test_example.py').write_text(test)
        git(repo,'add','.');git(repo,'commit','-qm','baseline')
        result=cli('project','prepare',repo,'--before','HEAD','--after','HEAD','--output',tmp_path/'sessions')
        assert result.returncode==0,result.stdout
        session=json.loads(result.stdout)['directory']
        result=cli('project','run',session,'--test','test_example.py::test_regression','--output',tmp_path/'reports')
        report=json.loads(result.stdout)
        assert report['entries'][0]['status']==('inconclusive' if blocked else 'regression_passed')
