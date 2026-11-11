import json
from pathlib import Path
import sys

from reprolab.execution_settings import inspect_execution_settings, describe_execution_settings
from reprolab.project_environment import inspect_project
from reprolab.runtime import BASE_ENVIRONMENT_KEYS, child_environment


def test_detects_addopts_without_loading_code_or_claiming_config_precedence(tmp_path, monkeypatch):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="demo"\ndependencies=[]\n[tool.pytest.ini_options]\naddopts=["-x", "-k", "smoke"]\n[tool.reprolab.pytest]\nplugins=["local_plugin"]\n')
    (tmp_path / 'pytest.ini').write_text('[pytest]\naddopts = -n auto\n')
    (tmp_path / 'local_plugin.py').write_text('raise AssertionError("must not import")')
    monkeypatch.setenv('DATABASE_URL', 'SECRET')
    settings = inspect_execution_settings(tmp_path)
    assert [row['file'] for row in settings['detected_addopts']] == ['pytest.ini', 'pyproject.toml']
    assert not settings['plugin_autoload'] and not settings['project_addopts_applied']
    assert settings['allowed_environment_names'] == list(BASE_ENVIRONMENT_KEYS)
    assert 'DATABASE_URL' not in child_environment()
    assert 'SECRET' not in json.dumps(settings) and 'local_plugin' not in sys.modules
    text = describe_execution_settings(settings)
    assert '-n auto' in text and '-x -k smoke' in text and '不会自动带入' in text
    assert inspect_project(tmp_path)['execution_settings'] == settings


def test_invalid_and_linked_configuration_is_reported_without_following_it(tmp_path):
    (tmp_path / 'pytest.toml').write_text('pytest = "wrong type"')
    outside = tmp_path / 'outside'; outside.write_text('[pytest]\naddopts = SECRET\n')
    (tmp_path / 'pytest.ini').symlink_to(outside)
    settings = inspect_execution_settings(tmp_path)
    assert len(settings['issues']) == 2 and not settings['detected_addopts']
    assert 'SECRET' not in json.dumps(settings)


def test_empty_configuration_explains_default_runtime_rules(tmp_path):
    settings = inspect_execution_settings(tmp_path)
    assert not settings['issues'] and settings['pytest_profile']['plugins'] == []
    text = describe_execution_settings(settings)
    assert '没有检测到' in text and '同一份所选测试' in text
