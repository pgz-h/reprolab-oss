"""Observed outcomes and matching comparison conditions are separate contracts."""
from copy import deepcopy
import pytest
from reprolab import project_history as history
from test_project_recheck import entry, saved_report, compare, DISPLAY


def pair():
    before = saved_report('first', [entry(DISPLAY, 'regression_detected', new='failed')])
    before['reprolab_version'] = '0.19.0a2'
    before['runtime'] = {'executable': '/trusted/python', 'python_version': '3.12', 'pytest_version': '8.4'}
    before['environment'] = {'status': 'recorded', 'platform': 'Darwin', 'machine': 'arm64', 'distributions': {'pytest': '8.4'}}
    for side in ('before', 'after'):
        before[side] = {'python_version': '3.12', 'pytest_version': '8.4'}
    before['persistence'] = {'sha256': {
        'evidence/project/inputs/after/tests/helpers.py': 'a' * 64,
        'evidence/project/inputs/after/conftest.py': 'b' * 64,
        'evidence/project/inputs/after/pyproject.toml': 'c' * 64,
        'evidence/project/inputs/after/cart.py': 'd' * 64}}
    after = deepcopy(before)
    after['run_id'] = 'second'
    after['entries'] = [entry(DISPLAY, 'regression_passed')]
    return before, after


@pytest.mark.parametrize('condition', ['baseline', 'environment', 'timeout', 'selection', 'helper', 'fixture', 'config'])
def test_condition_changes_preserve_observation_but_require_review(monkeypatch, condition):
    before, after = pair()
    if condition == 'baseline':
        after['before_commit'] = '1' * 40
    elif condition == 'environment':
        after['environment']['distributions']['extra'] = '1'
    elif condition == 'timeout':
        after['timeout'] = 360
    elif condition == 'selection':
        before['selected_tests'].append('tests/test_cart.py::test_extra')
    else:
        name = {'helper': 'tests/helpers.py', 'fixture': 'conftest.py', 'config': 'pyproject.toml'}[condition]
        after['persistence']['sha256']['evidence/project/inputs/after/' + name] = 'e' * 64
    result = compare(monkeypatch, before, after)
    row = result['entries'][0]
    assert result['same_check_resolved'] == 0
    assert row['change'] == 'conditions_changed'
    assert row['state_change'] == 'became_passed'
    assert row['after_execution'] == 'passed'
    assert False in row['conditions'].values()


def test_missing_environment_is_unknown_and_business_source_changes_are_allowed(monkeypatch):
    before, after = pair()
    after['persistence']['sha256']['evidence/project/inputs/after/cart.py'] = '0' * 64
    result = compare(monkeypatch, before, after)
    assert result['same_check_resolved'] == 1
    after.pop('environment')
    result = compare(monkeypatch, before, after)
    assert result['same_check_resolved'] == 0
    assert result['entries'][0]['change'] == 'conditions_unknown'


def test_order_changes_and_removed_helper_are_visible(monkeypatch):
    before, after = pair()
    before['selected_tests'].append('tests/test_cart.py::test_extra')
    after['selected_tests'] = list(reversed(before['selected_tests']))
    del after['persistence']['sha256']['evidence/project/inputs/after/tests/helpers.py']
    result = compare(monkeypatch, before, after)
    row = result['entries'][0]
    assert row['conditions']['selection'] is False
    assert row['changed_support_files'] == ['after/tests/helpers.py']
