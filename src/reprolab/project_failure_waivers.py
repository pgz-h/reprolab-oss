"""Check dated, explicitly scoped pytest failure exemptions against saved observations."""
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from .project_selections import validate_selection


def waiver_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('日期须为 YYYY-MM-DD。')
    return date.fromisoformat(value)


def _failure_identity(outcome):
    parts = []
    for event in outcome.get('phases', ()):
        if event.get('outcome') != 'failed':
            continue
        exception = event.get('exception')
        if (not isinstance(exception, dict) or not isinstance(exception.get('type'), str)
                or not isinstance(exception.get('message'), str)):
            return None
        parts.append([event['phase'], exception['type'], exception['message']])
    if not parts:
        return None
    canonical = json.dumps(parts, ensure_ascii=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


def recorded_failure_fingerprint(outcome):
    if 'phases' in outcome:
        return _failure_identity(outcome)
    error = outcome.get('exception')
    if not isinstance(error, dict) or not all(isinstance(error.get(key), str) for key in ('type', 'message')):
        return None
    return hashlib.sha256(json.dumps([error['type'], error['message']], ensure_ascii=True,
                                     separators=(',', ':')).encode()).hexdigest()


def recorded_existing_failures(report):
    if report.get('runner', 'pytest') != 'pytest':
        return []
    return [{'test_id': entry['test_id'], **{side: _failure_identity(entry[side]) for side in ('before', 'after')}}
            for entry in report['entries'] if entry['status'] == 'baseline_failure']


def _read_waivers(location):
    path = Path(location).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('请选择不超过 2 MiB 的普通失败豁免 JSON。')
    data = json.loads(path.read_bytes())
    if (not isinstance(data, dict) or data.keys() != {'kind', 'schema_version', 'entries'}
            or data['kind'] != 'project_failure_waivers' or type(data['schema_version']) is not int
            or data['schema_version'] != 1 or not isinstance(data['entries'], list) or len(data['entries']) > 2000):
        raise ValueError('失败豁免文件格式无效。')
    entries = data['entries']
    if any(not isinstance(row, dict) or row.keys() != {'test_id', 'failure_sha256', 'expires_on'}
           or not isinstance(row['failure_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', row['failure_sha256'])
           for row in entries):
        raise ValueError('每项豁免须指定完整测试 ID、故障指纹与到期日。')
    identities = [row['test_id'] for row in entries]
    if validate_selection(identities, allow_empty=True) != identities:
        raise ValueError('豁免目标不可重复。')
    for row in entries:
        waiver_date(row['expires_on'])
    reference = hashlib.sha256(json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {row['test_id']: row for row in entries}, reference


def check_failure_waivers(report, location, as_of):
    if report.get('runner', 'pytest') != 'pytest':
        raise ValueError('具名失败豁免目前用于 pytest。')
    now = waiver_date(as_of)
    waivers, digest = _read_waivers(location)
    failures = recorded_existing_failures(report)
    results = []
    for failure in failures:
        identity = failure['test_id']
        approved = waivers.get(identity)
        reason = ('not_approved' if approved is None else 'expired' if now >= waiver_date(approved['expires_on'])
                  else 'fingerprint_changed' if any(failure[side] != approved['failure_sha256']
                                                    for side in ('before', 'after')) else 'matched')
        results.append({**failure, 'reason': reason, 'expires_on': approved['expires_on'] if approved else None})
    return {'status': 'passed' if all(row['reason'] == 'matched' for row in results) else 'blocked',
            'waivers_sha256': digest, 'as_of': as_of, 'entries': results,
            'unused_count': len(waivers.keys() - {row['test_id'] for row in failures})}
