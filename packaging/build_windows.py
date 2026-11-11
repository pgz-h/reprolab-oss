"""Build and validate an x64 Windows portable app on a Windows host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tomllib

from verify_desktop_payload import verify_desktop_payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64') or sys.maxsize <= 2**32:
        parser.error('Windows x64 Python on a Windows host is required; this is not a cross-compiler.')
    work, output = args.work_root.resolve(), args.output_dir.resolve()
    if work.is_relative_to(source) or output.is_relative_to(source):
        parser.error('Build and output directories must be outside the source tree.')
    if work.exists() or output.exists():
        parser.error('Choose new build and output directories; existing artifacts are not overwritten.')
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    version = tomllib.loads((source/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    env = dict(os.environ, PYTHONUTF8='1', PYTHONPYCACHEPREFIX=str(work/'pycache'))
    subprocess.run([sys.executable, '-X', 'utf8', '-m', 'PyInstaller', '--noconfirm',
        '--workpath', str(work/'pyinstaller'), '--distpath', str(work/'dist'),
        str(source/'packaging/desktop.spec')], cwd=source, env=env, check=True)
    app = work/'dist/ReproLab'
    executable = app/'ReproLab.exe'
    if not executable.is_file():
        raise RuntimeError('PyInstaller did not create the expected Windows executable.')
    verify_desktop_payload(executable)
    subprocess.run([sys.executable, '-X', 'utf8', str(source/'packaging/verify_windows.py'),
        '--exe', str(executable), '--output', str(work/'acceptance')], cwd=source, env=env, check=True)
    # Only publish the portable bundle after all native acceptance checks pass.
    name = f'ReproLab-{version}-Windows-x64'
    archive = Path(shutil.make_archive(str(output/name), 'zip', root_dir=app.parent, base_dir=app.name))
    shutil.copy2(work/'acceptance/windows-validation.json', output/'windows-validation.json')
    with archive.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    record = {'version': version, 'platform': platform.platform(), 'architecture': 'x64',
        'artifact': archive.name, 'bytes': archive.stat().st_size,
        'sha256': checksum,
        'signing': 'unsigned', 'native_acceptance': 'passed'}
    (output/'windows-build.json').write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (output/'SHA256SUMS-Windows.txt').write_text(record['sha256']+'  '+archive.name+'\n', encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
