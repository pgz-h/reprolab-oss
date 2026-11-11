"""Publish a result only after its evidence and readable report are saved."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .platform_support import private_permissions


def atomic_write(path, content):
    """Write/fsync a sibling temporary file; replace the destination in one step."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            private_permissions(stream.fileno())
            stream.write(content if isinstance(content, bytes) else content.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


class RunJournal:
    """A small interruption record, not an acceptance verdict."""
    def __init__(self):
        self.directory = None
        self.phase = 'preparing'
        self.completed = {}

    def record(self, phase=None, *, status='running', error=None):
        if phase:
            self.phase = phase
        if self.directory:
            save_json(self.directory / 'operation.json', {
                'status': status, 'phase': self.phase, 'completed': self.completed,
                'error': error, 'notice': '过程记录不代表验收通过；最终结果以完整的 report.json 为准。',
            })


def publish_report(result):
    from .report import write_report
    from .runner import source_fingerprint, source_mode_fingerprint
    root = Path(result['report_dir'])
    expected = {'evidence/case.json': result['preparation']['evidence_case_sha256']}
    for label in ('before', 'after'):
        expected[label + '.events.json'] = result[label]['events_sha256']
        expected[label + '.log'] = result[label]['log_sha256']

    def check_evidence():
        for label in ('before', 'after'):
            if (source_fingerprint(root / 'evidence' / label)[1] != result['source_sha256'][label]
                    or source_mode_fingerprint(root / 'evidence' / label) != result['source_mode_sha256'][label]):
                raise ValueError(f'{label} 保存的源码证据发生变化，不能发布验收结果。')
        if hashlib.sha256((root / 'evidence/regression.py').read_bytes()).hexdigest() != result['test_sha256']:
            raise ValueError('保存的测试证据发生变化，不能发布验收结果。')
        for name, digest in expected.items():
            with (root / name).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError(f'{name} 保存的证据与执行时记录不一致，不能发布验收结果。')

    check_evidence()
    # HTML is prepared first. report.json is the commit marker that catalogs use.
    # Failure leaves raw evidence and a journal, never a partially written verdict.
    try:
        write_report(result)
        names = ('report.html', 'before.log', 'after.log', 'before.events.json',
                 'after.events.json', 'evidence/case.json', 'evidence/regression.py')
        digests = {}
        for name in names:
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'缺少普通证据文件：{name}')
            digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        check_evidence()
        result['persistence'] = {'status': 'complete', 'sha256': digests,
                                 'notice': '保存内容的一致性校验，不是作者身份认证或依赖锁。'}
        save_json(root / 'report.json', result)
    except Exception:
        # This run owns its newly created directory. Do not leave a success HTML
        # beside a missing final JSON after a recoverable save failure.
        if not (root / 'report.json').exists():
            (root / 'report.html').unlink(missing_ok=True)
        raise
