"""Bind a producer or reviewer claim to a checked report with a detached signature."""
import base64
import binascii
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .acceptance_plan import AcceptancePlan, _unique_fields, read_acceptance_plan
from .persistence import atomic_write
from .project_history import _checked_report
from .replay_verification import open_replay_evidence


_KIND = 'project_report_attestation'
_FIELDS = {'kind', 'schema_version', 'actor', 'role', 'decision', 'workflow', 'reason',
           'recorded_at', 'report_sha256', 'plan_sha256'}
_NOTICE = ('签名证明记录与所提供的已信任公钥匹配。签署者身份和权限由接收方的密钥来源确认，'
           '签名决定与实际测试判定分别保存。')


def _openssl():
    executable = shutil.which('openssl')
    if not executable:
        raise ValueError('签名及核对需要本机 OpenSSL。')
    return executable


def _key(path):
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError('请选择已有的 PEM 密钥文件。')
    return path.resolve()


def _text(value, *, required=False):
    return isinstance(value, str) and len(value) <= 500 and all(char.isprintable() for char in value) and (
        bool(value.strip()) or not required)


def _encode(record):
    if (not isinstance(record, dict) or record.keys() != _FIELDS or record['kind'] != _KIND
            or type(record['schema_version']) is not int or record['schema_version'] != 1
            or record['role'] not in ('producer', 'reviewer')
            or record['decision'] not in ('recorded', 'approved', 'rejected')
            or not _text(record['actor'], required=True)
            or not _text(record['workflow']) or not _text(record['reason'], required=record['role'] == 'reviewer')
            or not isinstance(record['recorded_at'], str) or len(record['recorded_at']) > 64
            or (record['role'] == 'producer') != (record['decision'] == 'recorded')
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
                   for value in (record['report_sha256'], *((record['plan_sha256'],)
                                  if record['plan_sha256'] is not None else ())))):
        raise ValueError('签名记录结构无效。')
    instant = datetime.fromisoformat(record['recorded_at'])
    if instant.utcoffset() is None:
        raise ValueError('签名记录时间须包含 UTC 偏移。')
    return json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def _plan_digest(location):
    if location is None:
        return None
    plan = location if isinstance(location, AcceptancePlan) else read_acceptance_plan(location)
    return plan.fingerprint()


def attest_report(location, output, *, key, actor, role, decision, workflow, reason, plan, control):
    output = Path(output).expanduser().absolute()
    with open_replay_evidence(location, control=control) as source:
        report = _checked_report(source, control=control)
        if output.resolve().is_relative_to(Path(report['report_dir'])):
            raise ValueError('请将签名记录保存到报告目录之外。')
        record = {'kind': _KIND, 'schema_version': 1, 'actor': actor, 'role': role,
                  'decision': decision, 'workflow': workflow, 'reason': reason,
                  'recorded_at': datetime.now(timezone.utc).isoformat(),
                  'report_sha256': report['_report_sha256'], 'plan_sha256': _plan_digest(plan)}
        signed = subprocess.run([_openssl(), 'dgst', '-sha256', '-sign', str(_key(key)), '-passin', 'pass:'],
                                input=_encode(record), capture_output=True, timeout=10)
        if signed.returncode or not signed.stdout or len(signed.stdout) > 16 * 1024:
            raise ValueError('OpenSSL 无法使用所选密钥生成非交互签名。')
    envelope = {'record': record, 'signature': base64.b64encode(signed.stdout).decode('ascii')}
    control.commit()
    atomic_write(output, json.dumps(envelope, ensure_ascii=False, indent=2), replace=False)
    return {'status': 'signed', 'output': str(output), 'record': record, 'notice': _NOTICE}


def check_report_attestation(location, document, *, key, actor, role, workflow, plan, control):
    path = Path(document).expanduser()
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise ValueError('请选择不超过 128 KiB 的普通签名记录。')
    envelope = json.loads(path.read_bytes(), object_pairs_hook=_unique_fields)
    if not isinstance(envelope, dict) or envelope.keys() != {'record', 'signature'}:
        raise ValueError('签名记录格式无效。')
    record = envelope['record']
    canonical = _encode(record)
    try:
        signature = base64.b64decode(envelope['signature'], validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError('签名编码无效。') from exc
    if not signature or len(signature) > 16 * 1024:
        raise ValueError('签名大小无效。')
    with open_replay_evidence(location, control=control) as source:
        report = _checked_report(source, control=control)
    with tempfile.TemporaryDirectory(prefix='reprolab-attest-') as directory:
        detached = Path(directory) / 'signature'
        detached.write_bytes(signature)
        verified = subprocess.run([_openssl(), 'dgst', '-sha256', '-verify', str(_key(key)),
                                   '-signature', str(detached)], input=canonical, capture_output=True, timeout=10)
    checks = {'signature': verified.returncode == 0, 'report': record['report_sha256'] == report['_report_sha256'],
              'actor': record['actor'] == actor, 'role': record['role'] == role,
              'workflow': record['workflow'] == workflow if workflow is not None else None,
              'plan': record['plan_sha256'] == _plan_digest(plan) if plan else None}
    status = 'verified' if False not in checks.values() else 'different'
    return {'status': status, 'checks': checks, 'record': record, 'report_verdict': report['status'],
            'notice': _NOTICE}, 0 if status == 'verified' else 2


def required_review_approval(report, plan, *, document, key, actor, control):
    try:
        proof, code = check_report_attestation(report['report_dir'], document, key=key, actor=actor,
                                               role='reviewer', workflow=None, plan=plan, control=control)
    except (OSError, ValueError, TypeError, KeyError):
        return {'status': 'blocked', 'reason': 'approval_unavailable'}
    record = proof['record']
    if code or record['report_sha256'] != report['_report_sha256']:
        return {'status': 'blocked', 'reason': 'approval_unverified', 'checks': proof['checks']}
    decision = record['decision']
    status = 'passed' if decision == 'approved' else 'failed'
    return {'status': status, 'reason': 'approved' if status == 'passed' else 'reviewer_rejected',
            'actor': actor, 'recorded_at': record['recorded_at'], 'report_sha256': record['report_sha256'],
            'plan_sha256': record['plan_sha256']}
