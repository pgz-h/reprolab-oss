"""Save and verify the files returned by a replay recipient."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .project_history import report_reference
from .project_sessions import MAX_RECORD_BYTES
from .replay_verification import checked_replay_pair, compare_replay_reports, verify_replay


EVIDENCE_FILE = 'replay-evidence.zip'
CHECK_FILE = 'replay-check.json'
RECEIPT_FILE = 'replay-receipt.md'
MAX_MARKDOWN_BYTES = 64 * 1024


def replay_receipt_destination(destination, source, output_root=None):
    """Keep the receipt separate from saved evidence and any pending replay output."""
    target = Path(destination).expanduser()
    if target.exists() or target.is_symlink():
        raise ValueError('请选择一个不存在的重放回执目录。')
    target = target.resolve()
    folder = Path(source).resolve()
    if folder.is_file():
        folder = folder.parent
    if target.is_relative_to(folder):
        raise ValueError('重放回执须保存在来源报告目录之外。')
    if output_root is not None and Path(output_root).expanduser().resolve().is_relative_to(target):
        raise ValueError('重放报告的输出根目录须位于回执目录之外。')
    for parent in target.parents:
        if parent.exists():
            if not parent.is_dir():
                raise ValueError('重放回执的父路径须为目录。')
            break
    return target


def save_replay_receipt(replay, target, *, control):
    """Publish the checked replay archive and its two summaries together."""
    from .persistence import atomic_write, save_json
    from .project_archive import pack_project_report
    from .project_review_summary import render_replay_markdown

    verification = replay['replay_verification']
    reference = report_reference({**replay, '_report_sha256': verification['replay']['report_sha256']})
    if target.is_relative_to(Path(reference['report_dir']).resolve()):
        raise ValueError('重放回执须保存在重放报告目录之外。')
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent, prefix='.reprolab-receipt-') as temporary:
        stage = Path(temporary) / 'receipt'
        stage.mkdir(mode=0o700)
        pack_project_report(reference['report_dir'], stage / EVIDENCE_FILE,
                            expected_report=reference, control=control.child())
        save_json(stage / CHECK_FILE, verification)
        atomic_write(stage / RECEIPT_FILE, render_replay_markdown(verification))
        if target.exists() or target.is_symlink():
            raise ValueError('重放回执目录已被其他操作创建，未覆盖。')
        control.commit()
        stage.rename(target)


def export_replay_receipt(source, replay, destination, *, control):
    """Make a receipt from saved evidence without re-executing either run."""
    with checked_replay_pair(source, replay, control=control) as (original, rerun):
        verification = compare_replay_reports(original, rerun)
        result = {'kind': 'project_replay_receipt_export', 'schema_version': 1,
                  'status': 'not_saved', 'replay_verification': verification}
        if verification['status'] in ('same_run', 'inputs_changed'):
            return result
        target = replay_receipt_destination(destination, original['report_dir'])
        save_replay_receipt({**rerun, 'replay_verification': verification}, target, control=control)
        return {**result, 'status': 'saved', 'receipt_dir': str(target)}


def _receipt_file(directory, name):
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'缺少普通回执文件：{name}')
    return path


def _receipt_text(directory, name, limit):
    with _receipt_file(directory, name).open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'回执文件超过大小限制：{name}')
    return content.decode('utf-8')


def _unique_json_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('核对 JSON 含重复字段。')
    return value


def _check_summaries(directory, verification):
    from .project_review_summary import render_replay_markdown

    issues = []
    for name in (CHECK_FILE, RECEIPT_FILE):
        try:
            if name == CHECK_FILE:
                content = _receipt_text(directory, name, MAX_RECORD_BYTES)
                recorded = json.loads(content, object_pairs_hook=_unique_json_object)
                # Canonical JSON distinguishes true, 1 and 1.0 while ignoring formatting.
                matches = (json.dumps(recorded, sort_keys=True, allow_nan=False)
                           == json.dumps(verification, sort_keys=True, allow_nan=False))
            else:
                content = _receipt_text(directory, name, MAX_MARKDOWN_BYTES)
                matches = content.replace('\r\n', '\n') == render_replay_markdown(verification)
        except (OSError, ValueError, RecursionError) as exc:
            issues.append({'part': name, 'reason': 'invalid', 'message': str(exc)})
        else:
            if not matches:
                issues.append({'part': name, 'reason': 'different',
                               'message': '与根据两份证据重新计算的结果不同。'})
    return issues


def check_replay_receipt(source, receipt, *, control=None):
    """Check the returned archive and both summaries without executing project code."""
    directory = Path(receipt).expanduser()
    if not directory.is_dir():
        raise ValueError('请选择包含重放回执文件的目录。')
    verification = None
    try:
        archive = _receipt_file(directory, EVIDENCE_FILE)
        verification = verify_replay(source, archive, control=control)
    except (OSError, ValueError) as exc:
        issues = [{'part': 'evidence', 'reason': 'invalid', 'message': str(exc)}]
    else:
        issues = _check_summaries(directory, verification)
    return {'kind': 'project_replay_receipt_check', 'schema_version': 1,
            'status': 'inconsistent' if issues else 'consistent',
            'replay_verification': verification, 'issues': issues}
