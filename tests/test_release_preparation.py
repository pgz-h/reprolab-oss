"""Release notes, validation attribution and artifacts must describe this candidate."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'packaging/prepare_release.py'


def release(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    (source / 'packaging').mkdir(parents=True)
    (source / 'src/reprolab').mkdir(parents=True)
    (source / 'pyproject.toml').write_text('[project]\nname="reprolab-local"\nversion="0.19.0a8"\n')
    (source / 'src/reprolab/__init__.py').write_text('__version__ = "0.19.0a8"\n')
    (source / 'src/reprolab/_project_worker.py').write_text('CURRENT = True\n')
    (source / 'CHANGELOG.md').write_text('# Changelog\n\n## 未发布 — 0.19.0a8\n\n- Current candidate feature.\n\n## 未发布 — 0.19.0a7\n\n- Old feature.\n')
    output = tmp_path / 'release'
    output.mkdir()
    spec = importlib.util.spec_from_file_location('release_script', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '__file__', str(source / 'packaging/prepare_release.py'))
    monkeypatch.setattr(sys, 'argv', ['prepare_release.py', '--output-dir', str(output)])
    artifacts(source, output)
    return module, source, output


def artifacts(source, output, *, stale_wheel=False, stale_sdist=False):
    with zipfile.ZipFile(output / 'reprolab_local-0.19.0a8-py3-none-any.whl', 'w') as wheel:
        wheel.writestr('reprolab_local-0.19.0a8.dist-info/METADATA', 'Metadata-Version: 2.4\nName: reprolab-local\nVersion: 0.19.0a8\n')
        for path in (source / 'src').rglob('*.py'):
            data = b'STALE = True\n' if stale_wheel and path.name == '_project_worker.py' else path.read_bytes()
            wheel.writestr(path.relative_to(source / 'src').as_posix(), data)
    with tarfile.open(output / 'reprolab_local-0.19.0a8.tar.gz', 'w:gz') as archive:
        for path in source.rglob('*'):
            if path.is_file():
                data = b'STALE = True\n' if stale_sdist and path.name == '_project_worker.py' else path.read_bytes()
                member = tarfile.TarInfo('reprolab_local-0.19.0a8/' + path.relative_to(source).as_posix())
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))


def test_notes_use_current_changelog_and_do_not_invent_test_history(tmp_path, monkeypatch):
    module, source, output = release(tmp_path, monkeypatch)
    module.main()
    notes = (output / 'release-notes.txt').read_text()
    assert 'Current candidate feature.' in notes and 'Old feature.' not in notes
    assert '按用户要求未运行' not in notes and '本候选增加当前工作区刷新' not in notes
    assert json.loads((output / 'release-manifest.json').read_text())['runtime_validation'] == 'not_supplied'


@pytest.mark.parametrize('kind', ['wheel', 'sdist'])
def test_stale_python_artifact_is_rejected_before_publishing(tmp_path, monkeypatch, kind):
    module, source, output = release(tmp_path, monkeypatch)
    artifacts(source, output, stale_wheel=kind == 'wheel', stale_sdist=kind == 'sdist')
    with pytest.raises(ValueError, match='源码|source'):
        module.main()
    assert not (output / 'SOURCE_REVIEW').exists()
    assert not (output / 'release-manifest.json').exists()


def summary(module, source, output, *, wrong_version=False, wrong_source=False):
    records = [{'path': p.relative_to(source).as_posix(), 'sha256': module.digest(p),
                'mode': p.stat().st_mode & 0o777} for p in module.source_files(source)]
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    value = {'schema_version': 1, 'version': '0.19.0a7' if wrong_version else '0.19.0a8',
             'source_tree_sha256': '0' * 64 if wrong_source else fingerprint,
             'checks': [{'name': 'local regression', 'result': 'passed', 'details': '3 checks passed'}],
             'limitations': ['No remote CI run was verified.']}
    path = output / 'validation-summary.json'
    path.write_text(json.dumps(value))
    return value


def test_validation_summary_is_bound_and_reported_as_supplied_evidence(tmp_path, monkeypatch):
    module, source, output = release(tmp_path, monkeypatch)
    value = summary(module, source, output)
    module.main()
    manifest = json.loads((output / 'release-manifest.json').read_text())
    assert manifest['runtime_validation'] == 'supplied'
    assert manifest['validation_summary_sha256'] == module.digest(output / 'validation-summary.json')
    notes = (output / 'release-notes.txt').read_text()
    assert value['checks'][0]['details'] in notes and value['limitations'][0] in notes


@pytest.mark.parametrize('change', ['version', 'source'])
def test_validation_from_another_candidate_is_rejected(tmp_path, monkeypatch, change):
    module, source, output = release(tmp_path, monkeypatch)
    summary(module, source, output, wrong_version=change == 'version', wrong_source=change == 'source')
    with pytest.raises(ValueError, match='验证|validation'):
        module.main()
    assert not (output / 'SOURCE_REVIEW').exists()


def test_missing_current_changes_never_reuses_an_old_version(tmp_path, monkeypatch):
    module, source, output = release(tmp_path, monkeypatch)
    (source / 'CHANGELOG.md').write_text('# Changelog\n\n## 未发布 — 0.19.0a7\nOld changes\n')
    with pytest.raises(ValueError, match='CHANGELOG'):
        module.main()
    assert not (output / 'SOURCE_REVIEW').exists()


def test_failed_validation_is_preserved_in_notes_without_becoming_passed(tmp_path, monkeypatch):
    module, source, output = release(tmp_path, monkeypatch)
    value = summary(module, source, output)
    value['checks'][0].update(result='failed', details='A specific check failed; candidate remains incomplete.')
    (output / 'validation-summary.json').write_text(json.dumps(value))
    module.main()
    notes = (output / 'release-notes.txt').read_text()
    assert '[failed]' in notes and value['checks'][0]['details'] in notes
    manifest = json.loads((output / 'release-manifest.json').read_text())
    assert manifest['runtime_validation'] == 'supplied'


def test_invalid_validation_records_stop_release_preparation(tmp_path, monkeypatch):
    module, source, output = release(tmp_path, monkeypatch)
    value = summary(module, source, output)
    value['checks'] = [{'name': 'unclear', 'result': 'all_good', 'details': 'unknown vocabulary'}]
    (output / 'validation-summary.json').write_text(json.dumps(value))
    with pytest.raises(ValueError, match='验证'):
        module.main()
    assert not (output / 'release-manifest.json').exists()


def test_source_distribution_cannot_keep_stale_public_documentation(tmp_path, monkeypatch):
    module, source, output = release(tmp_path, monkeypatch)
    (source / 'CHANGELOG.md').write_text((source / 'CHANGELOG.md').read_text().replace('Current candidate feature.', 'Updated candidate feature.'))
    with pytest.raises(ValueError, match='源码'):
        module.main()
    assert not (output / 'SOURCE_REVIEW').exists()


def test_optional_disk_image_is_in_manifest_and_checksums(tmp_path, monkeypatch):
    module, source, output = release(tmp_path, monkeypatch)
    image = output / 'ReproLab-0.19.0a8-macOS-arm64.dmg'
    image.write_bytes(b'synthetic-disk-image')
    module.main()
    manifest = json.loads((output / 'release-manifest.json').read_text())
    assert any(item['file'] == image.name and item['sha256'] == module.digest(image) for item in manifest['assets'])
    assert module.digest(image) + '  ' + image.name in (output / 'SHA256SUMS.txt').read_text()
