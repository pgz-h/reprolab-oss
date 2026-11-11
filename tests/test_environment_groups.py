from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from reprolab.dependency_groups import resolve_dependency_groups
from reprolab import project_environment as env
from reprolab import plugin_inventory as plugins
from reprolab.pytest_profile import render_pytest_profile


def project(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('''[project]
name = "environment-demo"
version = "0.1"
dependencies = []
[project.optional-dependencies]
fast = ["packaging>=23"]
[dependency-groups]
Test_Group = ["pytest>=8,<10", {include-group = "helpers"}]
helpers = ["packaging>=23"]
future = [{unsupported-format = true}]
''')
    return tmp_path


def test_groups_are_normalized_included_and_selected_without_evaluating_unselected_groups():
    data = {'dependency-groups': {'Test_Group': ['a', {'include-group': 'helper'}],
                                 'helper': ['b', 'a'], 'future': [{'new-format': True}]},
            'project': {'optional-dependencies': {'speed': ['c>=1']}}}
    result = resolve_dependency_groups(data, ['group:test-group', 'extra:speed'])
    assert result['requirements'] == ['a', 'b', 'a', 'c>=1'] and not result['issues']
    assert result['selected'] == ['group:test-group', 'extra:speed']
    assert resolve_dependency_groups(data)['requirements'] == []


@pytest.mark.parametrize('groups', [
    {'test': [{'include-group': 'missing'}]},
    {'test': [{'include-group': 'helper'}], 'helper': [{'include-group': 'test'}]},
    {'test': [{'future-format': True}]},
])
def test_unsupported_selected_groups_report_review_instead_of_partial_installation(groups):
    result = resolve_dependency_groups({'dependency-groups': groups}, ['group:test'])
    assert result['issues'] and not result['requirements']


def test_plan_selection_and_manifest_are_rechecked_before_creating_environment(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    project(root)
    plan = env.plan_environment(root, tmp_path / 'new-env', sys.executable,
                                selected_groups=['group:test_group', 'extra:fast'])
    assert plan['selected_groups'] == ['group:test-group', 'extra:fast']
    assert 'packaging>=23' in plan['requirements']
    changed = deepcopy(plan)
    changed['selected_groups'] = []
    with pytest.raises(ValueError, match='重新预览'):
        env.prepare_environment(changed)
    assert not (tmp_path / 'new-env').exists()


def test_prepare_rechecks_the_selected_groups_after_installation(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    project(root)
    plan = env.plan_environment(root, tmp_path / 'new-env', sys.executable, selected_groups=['extra:fast'])
    calls = []
    def run(command, *, cwd, control, phase):
        if phase == 'installing':
            assert 'packaging>=23' in (cwd / 'reprolab-requirements.txt').read_text()
    def inspect(root, executable=None, *, selected_groups=None):
        calls.append(selected_groups)
        return {'status': 'ready', 'manifest_fingerprint': plan['manifest_fingerprint']}
    monkeypatch.setattr(env, '_run_step', run)
    monkeypatch.setattr(env, 'inspect_project', inspect)
    result = env.prepare_environment(plan)
    assert result['status'] == 'ready' and calls == [['extra:fast']]


def test_plugin_catalog_reads_real_entry_point_metadata_without_importing_plugin(tmp_path, monkeypatch):
    info = tmp_path / 'reprolab_catalog_fixture-1.2.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: reprolab-catalog-fixture\nVersion: 1.2\n')
    (info / 'entry_points.txt').write_text('[pytest11]\nfixture_plugin = reprolab_catalog_fixture\n')
    (tmp_path / 'reprolab_catalog_fixture.py').write_text('raise AssertionError("inspection must not import this plugin")\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    result = plugins.inspect_pytest_plugins(tmp_path, sys.executable, ['fixture_plugin', 'unknown_plugin'])
    match = next(row for row in result['available'] if row['name'] == 'fixture_plugin')
    assert match['distribution'] == 'reprolab-catalog-fixture' and match['version'] == '1.2'
    assert result['declared'][0]['status'] == 'installed'
    assert result['declared'][1]['status'] == 'unknown'
    assert 'reprolab_catalog_fixture' not in sys.modules
    text = render_pytest_profile(['fixture_plugin'], 'importlib')
    assert 'plugins = ["fixture_plugin"]' in text and 'import_mode = "importlib"' in text


def test_unavailable_plugin_metadata_keeps_declared_local_module_visible(tmp_path, monkeypatch):
    (tmp_path / 'local_plugin.py').write_text('raise AssertionError("do not import")')
    def unavailable(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(plugins, '_entry_points', unavailable)
    result = plugins.inspect_pytest_plugins(tmp_path, sys.executable, ['local_plugin', 'other'])
    assert result['status'] == 'unavailable'
    assert [row['status'] for row in result['declared']] == ['local', 'unknown']


def test_cli_inspection_and_preparation_preview_share_group_selection(tmp_path, capsys):
    from reprolab.cli import main
    project(tmp_path)
    assert main(['environment', 'inspect', str(tmp_path), '--group', 'test-group', '--extra', 'fast']) == 0
    inspection = json.loads(capsys.readouterr().out)
    assert inspection['selected_groups'] == ['group:test-group', 'extra:fast']
    assert 'packaging' in {row['name'] for row in inspection['dependencies']}
    assert not inspection['issues']


def test_project_preferences_survive_restart_including_existing_report_locations(tmp_path):
    from reprolab.desktop_store import DesktopStore
    store = DesktopStore(tmp_path)
    values = {'project_environment_groups': {'/example/project': ['group:test', 'extra:dev']},
              'project_report_views': {'project': {'run_id': 'chosen', 'report_sha256': 'a' * 64}},
              'project_report_locations': {'a' * 64: '/example/moved-report'}}
    store.settings.update(values)
    store.save()
    restored = DesktopStore(tmp_path)
    assert {key: restored.settings[key] for key in values} == values


def test_malformed_project_preferences_are_rejected_without_overwriting_settings(tmp_path):
    from reprolab.desktop_store import DesktopStore
    path = tmp_path / 'settings.json'
    raw = json.dumps({'project_environment_groups': {'/example/project': 'not-a-list'}})
    path.write_text(raw)
    with pytest.raises(ValueError, match='设置'):
        DesktopStore(tmp_path)
    assert path.read_text() == raw
