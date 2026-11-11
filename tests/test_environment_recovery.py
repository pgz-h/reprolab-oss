"""Existing environments remain usable without hiding unknown declarations."""
import json
import sys

import pytest

from reprolab.project_environment import inspect_project, plan_environment
from test_workflow import cli


def test_unparsed_declarations_require_review_but_do_not_require_a_new_environment(tmp_path):
    (tmp_path / 'requirements.txt').write_text('-e .\n--index-url https://user:SECRET@private.invalid/simple\n')
    result = inspect_project(tmp_path)
    assert result['status'] == 'review'
    assert result['reuse']['status'] == 'review'
    assert result['reuse']['reasons'] == result['issues']
    assert 'SECRET' not in json.dumps(result)
    with pytest.raises(ValueError, match='人工'):
        plan_environment(tmp_path, tmp_path.parent / 'new-env', sys.executable)
    command = cli('environment', 'inspect', tmp_path, '--python', sys.executable)
    assert command.returncode == 2
    assert json.loads(command.stdout)['reuse'] == result['reuse']


@pytest.mark.parametrize('configuration', [
    '[project]\ndependencies=["missing-reprolab-recovery-dependency"]\ndynamic=["requires-python"]\n',
    '[project]\nrequires-python="<3.11"\ndynamic=["dependencies"]\n',
    '[tool.reprolab.pytest]\nimport_mode="invalid"\n',
])
def test_known_invalid_conditions_block_reuse_even_with_reviewable_declarations(tmp_path, configuration):
    (tmp_path / 'pyproject.toml').write_text(configuration)
    result = inspect_project(tmp_path)
    assert result['reuse']['status'] == 'blocked'
    assert result['reuse']['reasons']


def test_unavailable_interpreter_blocks_reuse(tmp_path):
    result = inspect_project(tmp_path, tmp_path / 'missing-python')
    assert result['reuse']['status'] == 'blocked'


def test_missing_pytest_cannot_be_accepted_as_an_unknown_declaration(tmp_path):
    from test_workflow import make_environment
    executable, _ = make_environment(tmp_path, False)
    (tmp_path / 'requirements.txt').write_text('-e .\n')
    result = inspect_project(tmp_path, executable)
    assert result['status'] == 'review'
    assert result['reuse']['status'] == 'blocked'
    assert next(row for row in result['dependencies'] if row['name'] == 'pytest')['status'] == 'missing'
