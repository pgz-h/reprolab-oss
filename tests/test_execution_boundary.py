"""Actual subprocess checks: rejected selections must not start fixtures or bodies."""
import json
from pathlib import Path

import pytest

from reprolab.runner import run_case
from test_runner import make_case


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("dynamic", [False, True])
def test_multiple_tests_never_start_fixture_or_body(tmp_path, project, dynamic):
    marker = tmp_path / "executed"
    test = ("import pytest\nfrom pathlib import Path\n"
            f"MARKER = Path({str(marker)!r})\n"
            "@pytest.fixture(autouse=True)\ndef side_effect():\n    MARKER.write_text('fixture')\n")
    if dynamic:
        test += "@pytest.mark.parametrize('value', [1, 2])\ndef test_one(value):\n    MARKER.write_text('body')\n"
    else:
        test += "def test_one():\n    MARKER.write_text('body')\ndef test_two(): pass\n"
    case = make_case(tmp_path, test=test)
    if project:
        data = json.loads(case.read_text())
        data["test_target"] = "tests/test_regression.py"
        case.write_text(json.dumps(data))
    result = run_case(case, tmp_path / "reports")
    assert not marker.exists(), "Rejected selection executed a fixture or test body"
    assert result["status"] == "inconclusive"
    for label in ("before", "after"):
        run = result[label]
        assert run["collected"] == 2
        assert run["worker_record_complete"] is True
        assert run["phase_events"] == []
        assert run["selection_guard"]["status"] == "rejected"
        assert run["diagnosis"]["code"] == "multiple_tests"
        raw = json.loads((Path(result["report_dir"]) / run["events"]).read_text())
        assert raw["events"] == []
        assert len(raw["selection_guard"]["test_ids"]) == 2


def test_empty_collection_records_explicit_rejection(tmp_path):
    result = run_case(make_case(tmp_path, test="# no tests\n"), tmp_path / "reports")
    assert result["status"] == "inconclusive"
    assert result["before"]["selection_guard"]["status"] == "rejected"
    assert result["before"]["diagnosis"]["code"] == "no_tests"
    assert result["before"]["phase_events"] == []


def test_one_selected_parameter_runs_normally(tmp_path):
    case = make_case(tmp_path, test="import pytest\nfrom sample import compute\n@pytest.mark.parametrize('value', [42, 43])\ndef test_one(value):\n    assert compute() == value\n")
    data = json.loads(case.read_text())
    data["test_target"] = "tests/test_regression.py::test_one[42]"
    case.write_text(json.dumps(data))
    result = run_case(case, tmp_path / "reports")
    assert result["status"] == "verified"
    assert result["after"]["selection_guard"]["status"] == "allowed"
    assert len(result["after"]["phase_events"]) == 3


def test_project_hook_cannot_expand_selection_before_execution(tmp_path):
    marker = tmp_path / 'fixture-started'
    case = make_case(tmp_path, test=f'from pathlib import Path\ndef test_one(): Path({str(marker)!r}).touch()\n')
    data = json.loads(case.read_text())
    data['test_target'] = 'tests/test_one.py::test_one'
    case.write_text(json.dumps(data))
    for label in ('before', 'after'):
        (tmp_path / label / 'conftest.py').write_text(
            'def pytest_runtestloop(session):\n    session.items.extend(session.items[:])\n')
    result = run_case(case, tmp_path / 'reports')
    assert not marker.exists(), 'runtestloop changed selection and still started the body'
    assert result['status'] == 'inconclusive'
    assert result['before']['selection_guard']['collected'] == 2


def test_project_collection_hook_can_narrow_to_one_test(tmp_path):
    case = make_case(tmp_path, test='from sample import compute\ndef test_one(): assert compute() == 42\ndef test_other(): assert False\n')
    data = json.loads(case.read_text())
    data['test_target'] = 'tests/test_one.py'
    case.write_text(json.dumps(data))
    for label in ('before', 'after'):
        (tmp_path / label / 'conftest.py').write_text('def pytest_collection_modifyitems(items):\n    items[:] = items[:1]\n')
    assert run_case(case, tmp_path / 'reports')['status'] == 'verified'


@pytest.mark.parametrize('damage', ['different_id', 'missing_id', 'invalid_exception', 'guard_id', 'guard_count'])
def test_invalid_worker_phase_records_are_inconclusive(damage):
    from reprolab.runner import _classify
    events = [{'phase': phase, 'outcome': 'passed', 'test_id': 'regression.py::test_one', 'exception': None}
              for phase in ('setup', 'call', 'teardown')]
    raw = {'collected': 1, 'exit_code': 0, 'events': events, 'collection_errors': [],
           'selection_guard': {'status': 'allowed', 'collected': 1, 'test_ids': ['regression.py::test_one']}}
    code = 0
    if damage == 'different_id':
        events[0]['test_id'] = 'regression.py::other'
    elif damage == 'missing_id':
        del events[1]['test_id']
    elif damage == 'invalid_exception':
        events[1].update(outcome='failed', exception={'type': 42})
        raw['exit_code'] = code = 1
    elif damage == 'guard_id':
        raw['selection_guard']['test_ids'] = ['regression.py::other']
    else:
        raw['selection_guard']['collected'] = 2
    assert _classify(raw, code)[0] == 'inconclusive'
