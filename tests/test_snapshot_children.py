"""Subprocess imports and console commands must follow each frozen version."""
import json
from pathlib import Path
import sys

import pytest
from test_git_source import git
from test_workflow import make_environment, cli


@pytest.mark.parametrize('command', ['python', 'console'])
@pytest.mark.parametrize('flow', ['strict', 'project'])
def test_children_use_each_snapshot_instead_of_installed_copy(tmp_path, command, flow):
    python, site = make_environment(tmp_path, True)
    (site/'child_package.py').write_text('VALUE = 42\n')
    probe = python.parent/'reprolab-child-probe'
    code = 'import child_package,json; print(json.dumps({"value":child_package.VALUE,"file":child_package.__file__}))'
    probe.write_text('#!'+str(python)+'\n'+code+'\n'); probe.chmod(0o755)
    repo=tmp_path/'repo'; repo.mkdir(); (repo/'src').mkdir(); (repo/'tests').mkdir()
    git(repo,'init','-q'); git(repo,'config','user.name','test'); git(repo,'config','user.email','test@example.invalid')
    source=repo/'src/child_package.py'; source.write_text('VALUE = 0\n')
    expression='[sys.executable, "-c", '+repr(code)+']' if command=='python' else '["reprolab-child-probe"]'
    (repo/'tests/test_child.py').write_text('import subprocess,sys,json\ndef test_child(tmp_path):\n'
        '    result=json.loads(subprocess.check_output('+expression+',cwd=tmp_path,text=True))\n'
        '    print("CHILD_IMPORT",result["file"])\n    assert result["value"] == 42\n')
    git(repo,'add','.');git(repo,'commit','-qm','before'); before=git(repo,'rev-parse','HEAD')
    source.write_text('VALUE = 42\n');git(repo,'add','.');git(repo,'commit','-qm','after')
    if flow=='strict':
        result=cli('verify-git',repo,'--before',before,'--after','HEAD','--project-test','tests/test_child.py::test_child',
                   '--failure-type','AssertionError','--failure-message','0 == 42','--python',python,'--output',tmp_path/'reports','--json')
        assert result.returncode==0,result.stdout+result.stderr
        report=json.loads(result.stdout); assert report['status']=='verified'
    else:
        prepared=cli('project','prepare',repo,'--before',before,'--after','HEAD','--python',python,'--output',tmp_path/'sessions')
        assert prepared.returncode==0,prepared.stdout
        session=json.loads(prepared.stdout)['directory']
        result=cli('project','run',session,'--test','tests/test_child.py::test_child','--output',tmp_path/'reports')
        assert result.returncode==0,result.stdout+result.stderr
        report=json.loads(result.stdout); assert report['entries'][0]['status']=='improved'
    log=(Path(report['report_dir'])/'before.log').read_text()
    assert '/source/src/child_package.py' in log and str(site/'child_package.py') not in log


def test_snapshot_environment_does_not_mutate_shared_values_or_accept_ambient_pythonpath(tmp_path):
    from reprolab.runtime import snapshot_process_environment
    original={'PATH':'/original/bin','PYTHONPATH':'/unrelated','PROJECT_OPTION':'value'}
    result=snapshot_process_environment(original,tmp_path/'source','/chosen/env/bin/python')
    assert result['PYTHONPATH']==str(tmp_path/'source/src')+':'+str(tmp_path/'source')
    assert result['PATH']=='/chosen/env/bin:/original/bin'
    assert original['PYTHONPATH']=='/unrelated' and result['PROJECT_OPTION']=='value'
