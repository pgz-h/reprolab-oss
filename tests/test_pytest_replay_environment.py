import json
from pathlib import Path

from reprolab.project_cli import main
from reprolab.project_sessions import prepare_project, run_project
from reprolab.workspace import WORKTREE
from test_pytest_profile import make_project
from test_workflow import make_environment


def test_imported_report_checks_receiver_python_before_loading_plugins(tmp_path, capsys):
    repo = make_project(tmp_path / 'project')
    (repo / 'pyproject.toml').write_text(
        '[project]\nname = "replay-fixture"\nversion = "0.0.0"\ndependencies = []\n'
        '[tool.reprolab.pytest]\nplugins = ["reprolab_receiver_plugin"]\n')
    plugin_marker = tmp_path / 'plugin-loaded'
    plugin = ("from pathlib import Path\nimport pytest\n"
              f"Path({str(plugin_marker)!r}).touch()\n"
              "@pytest.fixture\ndef expected():\n    return 42\n")

    def install_plugin(purelib):
        (purelib / 'reprolab_receiver_plugin.py').write_text(plugin)
        dist = purelib / 'reprolab_receiver_plugin-1.0.dist-info'
        dist.mkdir()
        (dist / 'METADATA').write_text('Metadata-Version: 2.1\nName: reprolab-receiver-plugin\nVersion: 1.0\n')
        (dist / 'entry_points.txt').write_text('[pytest11]\nreceiver = reprolab_receiver_plugin\n')

    original, original_site = make_environment(tmp_path / 'sender', True)
    receiver, receiver_site = make_environment(tmp_path / 'receiver', True)
    install_plugin(original_site)
    state = prepare_project(repo, 'HEAD', WORKTREE, tmp_path / 'sessions', python_executable=original)
    report = run_project(state['directory'], tmp_path / 'reports', confirmed=True)
    assert report['counts'] == {'improved': 1}
    assert plugin_marker.is_file()
    plugin_marker.unlink()

    bundle = tmp_path / 'evidence.zip'
    imported = tmp_path / 'imported'

    def cli(*args, code=0):
        assert main(list(map(str, args))) == code
        stdout, stderr = capsys.readouterr()
        return json.loads(stdout or stderr)

    cli('pack', report['report_dir'], '--output', bundle)
    cli('unpack', bundle, imported)
    source_bytes = (imported / 'report.json').read_bytes()
    archive_bytes = bundle.read_bytes()
    repo.rename(tmp_path / 'moved-project')
    output = tmp_path / 'replays'
    missing = tmp_path / 'missing-python'
    options = ('replay', bundle, '--output', output)
    preview = cli(*options, '--python', missing)
    assert preview['status'] == 'ready' and 'environment_check' not in preview
    assert preview['source_archive'] == str(bundle) and 'report_dir' not in preview
    directory_preview = cli('replay', imported, '--python', missing, '--output', output)
    assert directory_preview['report_dir'] == str(imported) and 'source_archive' not in directory_preview
    damaged = tmp_path / 'damaged.zip'
    damaged.write_bytes(archive_bytes[:32])
    rejected = cli('replay', damaged, '--python', receiver, '--check-environment', code=2)
    assert rejected['status'] == 'error' and 'ZIP' in rejected['error']['message']
    blocked = cli(*options, '--python', missing, '--check-environment', code=2)
    assert blocked['status'] == 'blocked'
    assert '不存在或不可执行' in blocked['environment_check']['issues'][0]

    review = cli(*options, '--python', receiver, '--check-environment')
    assert review['status'] == 'review'
    check = review['environment_check']
    assert review['source_archive'] == str(bundle) and 'report_dir' not in review
    assert check['plugins'][0]['status'] == 'unknown'
    assert check['plugins'][0]['name'] == 'reprolab_receiver_plugin'
    assert {'name': 'reprolab-receiver-plugin', 'version': '1.0'} in check['comparison']['removed']
    assert not plugin_marker.exists() and not output.exists()

    failure = cli(*options, '--python', receiver, '--confirm-run', code=2)
    message = failure['error']['message']
    assert '插件 reprolab_receiver_plugin' in message
    assert Path(message.split('收集日志：', 1)[1]).is_file()

    install_plugin(receiver_site)
    ready = cli(*options, '--python', receiver, '--check-environment')
    assert ready['status'] == 'ready'
    assert ready['environment_check']['plugins'][0]['status'] == 'installed'
    assert ready['environment_check']['comparison']['path_changed']
    assert not plugin_marker.exists()
    replayed = cli(*options, '--python', receiver, '--confirm-run')
    assert replayed['counts'] == report['counts'] and replayed['source_sha256'] == report['source_sha256']
    assert replayed['runtime']['executable'] == str(receiver)
    assert plugin_marker.exists()
    assert (imported / 'report.json').read_bytes() == source_bytes
    assert bundle.read_bytes() == archive_bytes
