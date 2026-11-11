"""Prepare a reviewable public source snapshot and release manifests; no execution QA."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import tarfile
from email.parser import BytesParser
import tomllib
import zipfile


ROOT_FILES = ('README.md', 'README.en.md', 'LICENSE', 'CHANGELOG.md', 'INSTALL.md',
              'CONTRIBUTING.md', 'SECURITY.md', 'pyproject.toml', 'MANIFEST.in',
              '.gitignore', '.gitattributes')
FOLDERS = ('src', 'tests', 'docs', 'packaging', '.github')
EXCLUDED = {'.git', '__pycache__', '.pytest_cache', '.venv', 'node_modules', 'build', 'dist', 'outputs', '.DS_Store'}
TOKEN = re.compile(rb'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{25,}|gh[pousr]_[A-Za-z0-9]{25,}|AKIA[0-9A-Z]{16})\b')
PRIVATE_KEY = re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_files(source):
    candidates = [source / name for name in ROOT_FILES if (source / name).exists()]
    for name in FOLDERS:
        folder = source / name
        if folder.is_symlink():
            raise ValueError('公开源码目录不能是符号链接：' + name)
        if folder.is_dir():
            candidates.extend(folder.rglob('*'))
    result = []
    for path in candidates:
        relative = path.relative_to(source)
        if (any(part in EXCLUDED or part.endswith('.egg-info') or part.startswith('.env') for part in relative.parts)
                or relative.parts[:2] == ('docs', 'superpowers') or path.suffix == '.pyc'):
            continue
        if path.is_symlink():
            raise ValueError('公开源码中存在符号链接，请先审阅：' + relative.as_posix())
        if path.is_file():
            result.append(path)
    return sorted(set(result))



def current_changes(source, version):
    text = (source / 'CHANGELOG.md').read_text(encoding='utf-8')
    sections = re.split(r'^## +', text, flags=re.MULTILINE)
    for section in sections[1:]:
        title, _, body = section.partition('\n')
        if re.search(r'(?<![\w.])' + re.escape(version) + r'(?![\w.])', title):
            return body.strip()
    raise ValueError('CHANGELOG.md 缺少当前候选版本的更新记录。')


def verify_python_artifacts(source, wheel_path, sdist_path, files, version):
    """Compare build payloads, rather than trusting filenames with the right version."""
    package = {p.relative_to(source / 'src').as_posix(): p.read_bytes()
               for p in files if p.is_relative_to(source / 'src/reprolab')}
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        metadata = [name for name in names if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or len(names) != len(set(names)):
            raise ValueError('wheel 元数据或源码成员不唯一。')
        info = BytesParser().parsebytes(wheel.read(metadata[0]))
        if info['Name'] != 'reprolab-local' or info['Version'] != version:
            raise ValueError('wheel 元数据与当前源码版本不一致。')
        packaged = {name: wheel.read(name) for name in names if name.startswith('reprolab/') and not name.endswith('/')}
        if packaged != package:
            raise ValueError('wheel 内容与当前源码不一致，请重新构建同一候选。')
    with tarfile.open(sdist_path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        roots = {name.split('/')[0] for name in names}
        if len(roots) != 1 or len(names) != len(set(names)):
            raise ValueError('Python 源码包结构不唯一。')
        root = roots.pop()
        required = {p.relative_to(source).as_posix(): p for p in files
                    if p.relative_to(source).as_posix() not in ('.gitignore', '.gitattributes')}
        actual = {m.name[len(root) + 1:]: m for m in members if m.name.startswith(root + '/src/') and not m.isdir()}
        if set(actual) != {name for name in required if name.startswith('src/')}:
            # setuptools metadata is generated separately, outside the source package.
            actual = {name: m for name, m in actual.items() if not any(part.endswith('.egg-info') for part in Path(name).parts)}
            if set(actual) != {name for name in required if name.startswith('src/')}:
                raise ValueError('Python 源码包的模块清单与当前源码不一致。')
        by_name = {m.name: m for m in members}
        for name, path in required.items():
            member = by_name.get(root + '/' + name)
            if member is None or not member.isfile() or archive.extractfile(member).read() != path.read_bytes():
                raise ValueError('Python 源码包内容与当前源码不一致：' + name)


def read_validation(path, version, tree_sha256):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('验证摘要需要是不超过 1 MiB 的普通 JSON 文件。')
    value = json.loads(path.read_bytes())
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('version') != version or value.get('source_tree_sha256') != tree_sha256
            or not isinstance(value.get('checks'), list) or not value['checks']
            or not isinstance(value.get('limitations'), list)
            or any(not isinstance(item, str) for item in value['limitations'])):
        raise ValueError('验证摘要格式、版本或源码指纹与当前候选不一致。')
    for check in value['checks']:
        if (not isinstance(check, dict) or check.get('result') not in ('passed', 'failed', 'skipped', 'not_run')
                or any(not isinstance(check.get(key), str) or not check[key].strip() for key in ('name', 'details'))):
            raise ValueError('验证摘要需要明确的检查名称、结果和范围说明。')
    return value


def main():
    parser = argparse.ArgumentParser(description='准备公开源码审阅副本、源码 ZIP 和发行清单；不执行应用或测试。')
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    output = args.output_dir.expanduser().resolve()
    if output.is_relative_to(source):
        parser.error('发行目录须位于源码仓库之外。')
    project = tomllib.loads((source / 'pyproject.toml').read_text())['project']
    version = project['version']
    tree = ast.parse((source / 'src/reprolab/__init__.py').read_text())
    code_version = next((ast.literal_eval(node.value) for node in tree.body
                         if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)), None)
    if code_version != version:
        parser.error('源码版本与软件包版本不一致。')
    output.mkdir(parents=True, exist_ok=True)
    source_zip = f'ReproLab-{version}-source.zip'
    publish_names = ('SOURCE_REVIEW', source_zip, 'source-manifest.json', 'release-manifest.json',
                     'release-notes.txt', 'SHA256SUMS.txt')
    if any((output / name).exists() for name in publish_names):
        parser.error('本目录已有发行准备材料，请使用新的候选目录，不覆盖旧材料。')
    required = [output / f'reprolab_local-{version}-py3-none-any.whl', output / f'reprolab_local-{version}.tar.gz']
    if any(not path.is_file() for path in required):
        parser.error('请先在发行目录构建同版本 wheel 和源码包。')
    files = source_files(source)
    with tempfile.TemporaryDirectory(prefix='.release-prepare-', dir=output) as temporary:
        stage = Path(temporary)
        review = stage / 'SOURCE_REVIEW'
        review.mkdir()
        records = []
        for path in files:
            relative = path.relative_to(source)
            data = path.read_bytes()
            if TOKEN.search(data) or PRIVATE_KEY.search(data):
                raise ValueError('公开源码有凭据格式候选，请先审阅文件：' + relative.as_posix())
            if str(Path.home()).encode('utf-8') in data:
                raise ValueError('公开源码仍有本机用户绝对路径，请先整理：' + relative.as_posix())
            target = review / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            mode = stat.S_IMODE(path.stat().st_mode) & 0o777
            target.chmod(mode)
            records.append({'path': relative.as_posix(), 'sha256': digest(target), 'mode': mode})
        changes = current_changes(review, version)
        verify_python_artifacts(review, *required, source_files(review), version)
        source_manifest = {'schema_version': 1, 'version': version, 'origin': 'working_tree_snapshot',
                           'files': records, 'contains_git_history': False,
                           'scan_scope': 'Basic known credential/private-key patterns and local-user paths; not a complete security audit.'}
        source_manifest['tree_sha256'] = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        validation = output / 'validation-summary.json'
        validation_record = read_validation(validation, version, source_manifest['tree_sha256'])
        (stage / 'source-manifest.json').write_text(json.dumps(source_manifest, ensure_ascii=False, indent=2) + '\n')
        with zipfile.ZipFile(stage / source_zip, 'x', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(review.rglob('*')):
                if path.is_file():
                    archive.write(path, 'ReproLab-' + version + '/' + path.relative_to(review).as_posix())
        assets = [*required, stage / source_zip]
        app_zip = output / f'ReproLab-{version}-macOS-arm64.zip'
        if app_zip.is_file():
            assets.insert(0, app_zip)
        windows_zip = output / f'ReproLab-{version}-Windows-x64.zip'
        if windows_zip.is_file():
            assets.insert(0, windows_zip)
        disk_image = output / f'ReproLab-{version}-macOS-arm64.dmg'
        if disk_image.is_file():
            assets.insert(0, disk_image)
        if validation_record is not None:
            assets.append(validation)
        validation_lines = ['验证状态：未附本候选验证摘要；本脚本不运行测试，也不推断此前是否执行过验证。']
        if validation_record is not None:
            validation_lines = ['验证状态：已附与当前版本和源码指纹匹配的验证摘要；以下为提供者记录，本脚本未重新执行。',
                                *(item['name'] + ' [' + item['result'] + ']：' + item['details'] for item in validation_record['checks']),
                                '验证边界：', *validation_record['limitations']]
        notes = '\n'.join([
            f'ReproLab {version} — 开源 Alpha 候选', '',
            '面向 Python/pytest 的修复验收与项目回归对比工具。',
            '', '本候选更新：', changes, '', *validation_lines, '',
            '桌面范围：macOS arm64；ad-hoc 签名，尚未进行 Apple Developer ID 公证。',
            '执行边界：只用于可信代码，子进程和临时副本不是安全沙箱；结论限于所选测试。',
            'AI 是可选功能，需要配置服务；生成质量和真实服务兼容性仍需按账号和项目核对。', '',
            '附件：', *('  ' + path.name for path in assets),
            '  source-manifest.json / release-manifest.json / SHA256SUMS.txt', '',
            '发布时请标记为 Pre-release。源码审阅副本不含开发历史，原工作区与历史保留。',
        ]) + '\n'
        (stage / 'release-notes.txt').write_text(notes)
        manifest = {'schema_version': 1, 'version': version, 'release_type': 'alpha_candidate',
                    'runtime_validation': 'supplied' if validation_record is not None else 'not_supplied',
                    'validation_summary_sha256': digest(validation) if validation_record is not None else None,
                    'python_artifacts_match_source': True,
                    'desktop_artifact_source_check': 'not_performed_by_this_script',
                    'github_publication': 'not_performed', 'source_tree_sha256': source_manifest['tree_sha256'],
                    'assets': [{'file': path.name, 'bytes': path.stat().st_size, 'sha256': digest(path)} for path in assets]}
        (stage / 'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        checksum_paths = [*assets, stage / 'source-manifest.json', stage / 'release-manifest.json', stage / 'release-notes.txt']
        (stage / 'SHA256SUMS.txt').write_text('\n'.join(digest(path) + '  ' + path.name for path in checksum_paths) + '\n')
        for name in publish_names:
            (stage / name).rename(output / name)
    print(json.dumps({'version': version, 'source_files': len(files), 'prepared': list(publish_names)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
