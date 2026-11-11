import importlib.util
import json
from pathlib import Path

import pytest
from test_runner import make_case


def store_module():
    assert importlib.util.find_spec("reprolab.desktop_store"), "desktop persistence is missing"
    from reprolab import desktop_store
    return desktop_store


def test_defaults_use_standard_directories_even_with_a_development_workspace(tmp_path, monkeypatch):
    module = store_module()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local/share"))
    legacy = tmp_path / "开源项目研究/work/reprolab"
    (legacy / "cases").mkdir(parents=True)
    (legacy / "ai.local.json").write_text('{"api_key":"test-only"}')

    defaults = module.default_settings()
    work, output = module.application_directories()
    assert defaults["cases"] == str(work / "cases")
    assert defaults["drafts"] == str(work / "drafts")
    assert defaults["reports"] == str(output / "reports")
    assert defaults["imports"] == str(output / "imported")
    assert defaults["model_config"] == ""
    assert module.default_state_directory() == work / "desktop"


def test_macos_preserves_existing_legacy_desktop_settings(tmp_path, monkeypatch):
    module = store_module()
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    legacy = tmp_path / '开源项目研究/work/reprolab/desktop'
    legacy.mkdir(parents=True)
    settings = '{"reports":"existing-reports"}'
    (legacy / 'settings.json').write_text(settings)
    restored = module.DesktopStore()
    assert restored.directory == legacy
    assert restored.settings['reports'] == 'existing-reports'
    assert (legacy / 'settings.json').read_text() == settings

    standard = tmp_path / 'Library/Application Support/ReproLab/desktop'
    standard.mkdir(parents=True)
    (standard / 'settings.json').write_text('{"reports":"new-reports"}')
    assert module.DesktopStore().settings['reports'] == 'new-reports'


def test_standard_saved_settings_still_restore_explicit_paths(tmp_path, monkeypatch):
    module = store_module()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local/share"))
    state = module.DesktopStore()
    state.settings["cases"] = str(tmp_path / "my-cases")
    state.settings["model_config"] = str(tmp_path / "my-model.json")
    state.save()

    restored = module.DesktopStore()
    assert restored.settings["cases"] == str(tmp_path / "my-cases")
    assert restored.settings["model_config"] == str(tmp_path / "my-model.json")


def test_settings_keep_venv_path_and_persist_directories(tmp_path):
    module = store_module()
    state = module.DesktopStore(tmp_path / "state")
    target = tmp_path / "real"; target.write_text("")
    link = tmp_path / "venv/bin/python"; link.parent.mkdir(parents=True); link.symlink_to(target)
    state.settings.update(python=str(link), cases=str(tmp_path / "案例"))
    state.save()
    restored = module.DesktopStore(tmp_path / "state")
    assert restored.settings["python"] == str(link)
    assert restored.settings["cases"] == str(tmp_path / "案例")


def test_interrupted_task_is_not_reloaded_as_success(tmp_path):
    module = store_module()
    state = module.DesktopStore(tmp_path / "state")
    state.record("case", "running")
    restarted = module.DesktopStore(tmp_path / "state")
    assert restarted.activity["case"]["status"] == "interrupted"


def test_case_source_or_test_changes_make_result_historical(tmp_path):
    module = store_module()
    case = make_case(tmp_path)
    state = module.DesktopStore(tmp_path / "state")
    stamp = module.input_stamp(case)
    state.record(str(case), "verified", stamp=stamp)
    assert state.case_status(case) == "verified"
    (tmp_path / "regression.py").write_text("def test_changed(): pass\n")
    assert state.case_status(case) == "stale"


def test_source_change_also_marks_stale(tmp_path):
    module = store_module()
    case = make_case(tmp_path)
    state = module.DesktopStore(tmp_path / "state")
    state.record(str(case), "verified", stamp=module.input_stamp(case))
    (tmp_path / "after/sample.py").write_text("def compute(): return 0\n")
    assert state.case_status(case) == "stale"


def test_broken_settings_do_not_get_silently_overwritten(tmp_path):
    module = store_module()
    tmp_path.joinpath("settings.json").write_text("{broken")
    with pytest.raises(ValueError, match="设置"):
        module.DesktopStore(tmp_path)
    assert tmp_path.joinpath("settings.json").read_text() == "{broken"


def test_ai_preview_never_contains_key_and_rejects_changed_context(tmp_path):
    module = store_module()
    folder = tmp_path / "draft"; folder.mkdir()
    (folder / "prompt.txt").write_text("test-only public teaching context")
    config = tmp_path / "ai.json"
    config.write_text(json.dumps({"base_url": "https://example.org/v1", "model": "fixture", "api_key": "SECRET-TEST-KEY"}))
    preview = module.generation_preview(folder, config)
    assert "SECRET-TEST-KEY" not in repr(preview)
    assert preview["endpoint"] == "https://example.org/v1/chat/completions"
    (folder / "prompt.txt").write_text("changed context")
    with pytest.raises(ValueError, match="改变"):
        module.check_preview(folder, config, preview)


@pytest.mark.parametrize("entries", [None, [None], [{"status": []}]])
def test_corrupt_batch_entries_do_not_break_other_catalog_records(tmp_path, entries):
    module = store_module()
    store = module.DesktopStore(tmp_path / "state")
    reports = tmp_path / "reports"
    good = reports / "good"; good.mkdir(parents=True)
    (good / "report.json").write_text(json.dumps({"title": "正常报告", "status": "verified", "generated_at": "2026-10-30"}))
    bad = reports / "bad"; bad.mkdir()
    (bad / "summary.json").write_text(json.dumps({"kind": "batch", "total": 1, "entries": entries}))
    store.settings["reports"] = str(reports)
    catalog = store.catalog()
    assert any(entry["title"] == "正常报告" for entry in catalog["reports"])


@pytest.mark.parametrize('options', [
    {'python': [], 'timeout': 120}, {'python': '/python', 'timeout': float('nan')},
    {'python': '/python', 'timeout': 0}, {'python': '/python', 'timeout': 4000},
    {'python': '/python'},
])
def test_invalid_project_options_are_reported_without_overwriting_settings(tmp_path, options):
    from reprolab.desktop_store import DesktopStore
    import json
    path = tmp_path / 'settings.json'
    raw = json.dumps({'project_defaults': {'/project': options}})
    path.write_text(raw)
    with pytest.raises(ValueError, match='设置无法读取'):
        DesktopStore(tmp_path)
    assert path.read_text() == raw


def test_incomplete_repository_input_does_not_break_preference_lookup(tmp_path):
    from reprolab.desktop_store import DesktopStore
    store = DesktopStore(tmp_path)
    assert store.project_defaults('/tmp/invalid\0path') is None
    assert store.project_defaults('') is None
