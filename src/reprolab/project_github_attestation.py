"""Check an offline GitHub Actions attestation for the exact transferred ZIP."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .project_history import _checked_report
from .replay_verification import open_replay_evidence

_PREDICATE = 'https://slsa.dev/provenance/v1'


def _file(location, limit):
    path = Path(location).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > limit * 1024 * 1024:
        raise ValueError('需要已保存的普通文件，大小超过限制或材料缺失。')
    return path


def verify_github_attestation(archive, bundle, trusted_root, *, repository, workflow, source_digest, control):
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', workflow)
            or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', source_digest)):
        raise ValueError('请从可信策略提供精确仓库、签署工作流路径和完整来源 Git SHA。')
    paths = [_file(archive, 250), _file(bundle, 16), _file(trusted_root, 8)]
    if paths[0].suffix.lower() != '.zip':
        raise ValueError('请选择需要核对来源的原始证据 ZIP。')
    gh = shutil.which('gh')
    if not gh:
        raise ValueError('需要 GitHub CLI 的 attestation verify。')
    with tempfile.TemporaryDirectory(prefix='reprolab-github-attest-') as temporary:
        root = Path(temporary)
        evidence, proof, trusted = [root / name for name in ('evidence.zip', 'bundle.jsonl', 'trusted-root.jsonl')]
        for source, destination in zip(paths, (evidence, proof, trusted)):
            control.check()
            shutil.copyfile(source, destination)
        with evidence.open('rb') as stream:
            fingerprint = hashlib.file_digest(stream, 'sha256').hexdigest()
        try:
            checked = subprocess.run(
                [gh, 'attestation', 'verify', str(evidence), '--bundle', str(proof),
                 '--custom-trusted-root', str(trusted), '--repo', repository, '--signer-workflow', workflow,
                 '--source-digest', source_digest, '--deny-self-hosted-runners',
                 '--predicate-type', _PREDICATE, '--format', 'json'],
                env={**os.environ, 'GH_PROMPT_DISABLED': '1'}, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('GitHub 签署证明核对超时。') from exc
        if checked.returncode:
            raise ValueError('GitHub CLI 未确认 ZIP 与可信工作流证明匹配。')
        try:
            results = json.loads(checked.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('GitHub CLI 未提供可核对的签名结果。') from exc
        if not isinstance(results, list) or not any(
                isinstance(item, dict)
                and isinstance(item.get('verificationResult'), dict)
                and isinstance((statement := item['verificationResult'].get('statement')), dict)
                and statement.get('predicateType') == _PREDICATE
                and any(isinstance(subject, dict) and (subject.get('digest') or {}).get('sha256') == fingerprint
                        for subject in statement.get('subject', ()))
                for item in results):
            raise ValueError('核对结果没有覆盖该 ZIP 指纹。')
        with open_replay_evidence(evidence, control=control) as source:
            report = _checked_report(source, control=control)
    return {'kind': 'project_github_attestation_check', 'schema_version': 1, 'status': 'verified',
            'artifact_sha256': fingerprint, 'report_sha256': report['_report_sha256'],
            'repository': repository, 'signer_workflow': workflow, 'source_digest': source_digest,
            'report_verdict': report['status']}
