"""Review revisions are independent runnable inputs, never edits of old evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from reprolab import repair_suite
from test_cli import cli
from test_git_source import prepare_repo, git
from test_repair_suite import _plan_response


@pytest.fixture
def planned_suite(tmp_path):
    repo, before, after, _ = prepare_repo(tmp_path)
    (repo / 'tests').mkdir()
    (repo / 'tests/test_project.py').write_text(
        'import pytest\nfrom sample import compute\n'
        '@pytest.mark.parametrize("want", [42, 100])\n'
        'def test_compute(want): assert compute() == want\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'project test')
    state = repair_suite.prepare_suite(repo, before, 'HEAD', '修复 compute', tmp_path / 'suites')
    response = tmp_path / 'response.json'
    plan = _plan_response()
    plan['checks'] = plan['checks'][:1]
    plan['checks'][0]['expected_failure']['message_contains'] = 'incorrect AI assumption'
    response.write_text(json.dumps(plan))
    return repair_suite.generate_suite(state['directory'], response_file=response)


def review_api():
    from reprolab import suite_review
    return suite_review


def tree_bytes(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file()}


def test_corrected_plan_runs_independently_and_keeps_original_evidence(planned_suite, tmp_path):
    api = review_api()
    original = Path(planned_suite['directory'])
    saved = tree_bytes(original)
    plan = api.editable_plan(original)
    assert set(plan) == {'title', 'overview', 'checks'}
    assert 'case' not in plan['checks'][0]
    plan['title'] = '人工确认故障条件'
    plan['checks'][0]['expected_failure']['message_contains'] = 'reported bug'
    plan['checks'][0]['test_code'] = 'from sample import compute\ndef test_fix(): assert compute() == 42, "correct output"\n'
    revision = api.revise_suite(original, plan, tmp_path / 'reviewed')
    folder = Path(revision['directory'])
    assert folder != original and tree_bytes(original) == saved
    assert revision['revision']['parent_suite_id'] == planned_suite['suite_id']
    assert revision['revision']['parent_plan_sha256'] == planned_suite['plan_sha256']
    assert revision['source_sha256'] == planned_suite['source_sha256']
    assert revision['source_mode_sha256'] == planned_suite['source_mode_sha256']
    assert revision['plan']['origin']['provider'] == 'human-review'
    assert not (tmp_path / 'reports').exists()
    moved = tmp_path / 'moved-review'
    folder.rename(moved)
    original.rename(tmp_path / 'original-away')
    result = repair_suite.run_suite(moved, tmp_path / 'reports', confirmed=True)
    assert result['status'] == 'verified'
    report = (Path(result['summary_dir']) / result['entries'][0]['report']).parent
    from reprolab.evidence import check_report
    from reprolab.recipes import run_saved_case
    assert check_report(report)['status'] == 'consistent'
    assert '人工审阅的检查来源' in (report / 'report.html').read_text()
    assert planned_suite['suite_id'] in (report / 'report.html').read_text()
    assert run_saved_case(report / 'evidence/case.json', tmp_path / 'replay')['status'] == 'verified'


def test_review_can_choose_an_exact_existing_parameter_instance(planned_suite, tmp_path):
    api = review_api()
    plan = api.editable_plan(planned_suite['directory'])
    check = plan['checks'][0]
    check.update(test_code=None, test_target='tests/test_project.py::test_compute[42]',
                 expected_failure={'type': 'ValueError', 'message_contains': 'reported bug'})
    revision = api.revise_suite(planned_suite['directory'], plan, tmp_path / 'reviewed')
    result = repair_suite.run_suite(revision['directory'], tmp_path / 'reports', confirmed=True)
    assert result['status'] == 'verified'


@pytest.mark.parametrize('invalid', ['no_repair', 'duplicate', 'skip', 'missing_test', 'empty_failure', 'too_many', 'two_sources'])
def test_invalid_revisions_never_create_an_output_or_change_the_original(planned_suite, tmp_path, invalid):
    api = review_api()
    original = Path(planned_suite['directory'])
    saved = tree_bytes(original)
    plan = api.editable_plan(original)
    check = plan['checks'][0]
    if invalid == 'no_repair':
        check.update(role='regression', expected_failure=None)
    elif invalid == 'duplicate':
        plan['checks'].append(deepcopy(check))
    elif invalid == 'skip':
        check['test_code'] = 'import pytest\nfrom sample import compute\ndef test_skip():\n    compute()\n    pytest.skip("not checked")\n'
    elif invalid == 'missing_test':
        check.update(test_code=None, test_target='tests/missing.py::test_one')
    elif invalid == 'empty_failure':
        check['expected_failure']['message_contains'] = ''
    elif invalid == 'two_sources':
        check.update(test_code='', test_target='tests/test_project.py::test_compute[42]')
    else:
        plan['checks'] = [{**deepcopy(check), 'id': f'check-{i}'} for i in range(21)]
    with pytest.raises(ValueError):
        api.revise_suite(original, plan, tmp_path / 'reviewed')
    assert tree_bytes(original) == saved
    assert not (tmp_path / 'reviewed').exists() or not list((tmp_path / 'reviewed').iterdir())


def test_unchanged_or_nested_revision_is_rejected(planned_suite, tmp_path):
    api = review_api()
    original = Path(planned_suite['directory'])
    plan = api.editable_plan(original)
    with pytest.raises(ValueError, match='变化|修改'):
        api.revise_suite(original, plan)
    plan['title'] = 'changed'
    with pytest.raises(ValueError, match='之外|内部'):
        api.revise_suite(original, plan, original / 'reviewed')
    assert not (original / 'reviewed').exists()


def test_failed_revision_publication_leaves_original_and_output_clean(planned_suite, tmp_path, monkeypatch):
    api = review_api()
    original = Path(planned_suite['directory'])
    saved = tree_bytes(original)
    plan = api.editable_plan(original)
    plan['title'] = 'changed'
    save = repair_suite.save_json

    def failed(path, data):
        if Path(path).name == 'plan.json':
            raise OSError('teaching disk failure')
        return save(path, data)

    monkeypatch.setattr(repair_suite, 'save_json', failed)
    with pytest.raises(OSError, match='disk failure'):
        api.revise_suite(original, plan, tmp_path / 'reviewed')
    assert tree_bytes(original) == saved
    assert not list((tmp_path / 'reviewed').iterdir())


@pytest.mark.parametrize('change_mode', [False, True])
def test_revision_copy_rejects_mid_copy_source_changes(planned_suite, tmp_path, monkeypatch, change_mode):
    api = review_api()
    original = Path(planned_suite['directory'])
    plan = api.editable_plan(original)
    plan['title'] = 'reviewed'
    copy = api.snapshot

    def changed(source, destination, entries):
        result = copy(source, destination, entries)
        if source.name == 'after':
            path = source / 'sample.py'
            if change_mode:
                path.chmod(path.stat().st_mode ^ 0o100)
            else:
                path.write_text('def compute(): return 100\n')
        return result

    monkeypatch.setattr(api, 'snapshot', changed)
    with pytest.raises(ValueError, match='变化'):
        api.revise_suite(original, plan, tmp_path / 'reviewed')
    assert not list((tmp_path / 'reviewed').iterdir())


def test_cli_exports_and_revises_then_runs_the_reviewed_plan(planned_suite, tmp_path):
    editable = tmp_path / 'review-plan.json'
    exported = cli('suite', 'export-plan', planned_suite['directory'], '--output', editable)
    assert exported.returncode == 0, exported.stderr
    plan = json.loads(editable.read_text())
    plan['checks'][0]['expected_failure']['message_contains'] = 'reported bug'
    editable.write_text(json.dumps(plan))
    revised = cli('suite', 'revise', planned_suite['directory'], '--plan', editable, '--output', tmp_path / 'reviewed')
    assert revised.returncode == 0, revised.stderr
    folder = json.loads(revised.stdout)['directory']
    result = cli('suite', 'run', folder, '--output', tmp_path / 'reports')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'verified'
    old = editable.read_bytes()
    assert cli('suite', 'export-plan', folder, '--output', editable).returncode == 2
    assert editable.read_bytes() == old
