"""Standalone, standard-library reader for the public replay classification format.

This file can be copied and run without installing ReproLab:
python -I project_projection_format.py outcome-projection.json
"""
from collections import Counter
from pathlib import Path
import hashlib
import json
import re
import sys
import zipfile

_KIND = 'reprolab_outcome_projection'
_SIDES = ('before', 'after')
_STATES = {'passed', 'failed', 'skipped', 'inconclusive', 'not_run'}
_CLASSIFICATIONS = {'regression_passed', 'improved', 'regression_detected', 'baseline_failure', 'inconclusive'}


def _classify(before, after):
    return {('passed', 'passed'): 'regression_passed', ('passed', 'failed'): 'regression_detected',
            ('failed', 'failed'): 'baseline_failure', ('failed', 'passed'): 'improved'}.get((before, after), 'inconclusive')


def project_report(report, source_sha256):
    rows = []
    for index, entry in enumerate(report['entries'], 1):
        rows.append({'id': f't{index:04d}', **{side: entry[side]['status'] for side in _SIDES},
                     'classification': entry['status']})
    return {'kind': _KIND, 'schema_version': 1, 'source_report_sha256': source_sha256,
            'runner': report.get('runner', 'pytest'), 'original_verdict': report['status'],
            'replayable': False, 'rows': rows}


def inspect_projection(record):
    fields = {'kind', 'schema_version', 'source_report_sha256', 'runner', 'original_verdict', 'replayable', 'rows'}
    if (not isinstance(record, dict) or record.keys() != fields or record.get('kind') != _KIND
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or not isinstance(record['source_report_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', record['source_report_sha256'])
            or record['runner'] not in ('pytest', 'go', 'vitest')
            or record['original_verdict'] not in ('regression_passed', 'regression_detected', 'attention', 'cancelled')
            or record['replayable'] is not False or not isinstance(record['rows'], list) or len(record['rows']) > 2000):
        raise ValueError('Invalid outcome projection.')
    lines, counts = [], Counter()
    for index, row in enumerate(record['rows'], 1):
        if (not isinstance(row, dict) or row.keys() != {'id', *_SIDES, 'classification'}
                or row['id'] != f't{index:04d}' or not all(row[side] in _STATES for side in _SIDES)
                or row['classification'] not in _CLASSIFICATIONS):
            raise ValueError('Invalid projected outcome.')
        status = _classify(row['before'], row['after']) if record['runner'] == 'pytest' else row['classification']
        if status != row['classification']:
            raise ValueError('Saved pytest classification does not match the projected outcomes.')
        counts[status] += 1
        lines.append(f'{row["id"]} · {row["before"]} → {row["after"]} · {status}')
    return {'status': 'consistent', 'replayable': False, 'source_verified': False,
            'source_report_sha256': record['source_report_sha256'], 'runner': record['runner'],
            'counts': dict(counts), 'transcript': lines,
            'notice': 'Standalone check covers only the projected outcome data. No tests, original scope gate or source are verified.'}


def read_projection(location):
    file = Path(location).expanduser()
    if not file.is_file() or file.is_symlink() or file.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Provide an outcome projection of at most 4 MiB.')
    return json.loads(file.read_bytes())


def read_archive(location):
    """Check primary archive file digests. Does not extract or execute the archive."""
    with zipfile.ZipFile(location) as archive:
        members = archive.infolist()
        if len(members) > 20000 or sum(item.file_size for item in members) > 250 * 1024 * 1024:
            raise ValueError('Archive exceeds the standalone reader limits.')
        roots = [item.filename for item in members if item.filename.count('/') == 1 and item.filename.endswith('/report.json')]
        names = {item.filename for item in members}
        if len(roots) != 1 or len(names) != len(members):
            raise ValueError('Archive must contain one primary report and no duplicate paths.')
        raw = archive.read(roots[0])
        report = json.loads(raw)
        if report.get('kind') != 'project_regression' or report.get('schema_version') != 1:
            raise ValueError('Unsupported project report.')
        hashes = report.get('persistence', {}).get('sha256')
        if not isinstance(hashes, dict) or not hashes:
            raise ValueError('Missing primary evidence digests.')
        prefix = roots[0].removesuffix('report.json')
        for name, digest in hashes.items():
            if ('\\' in name or ':' in name or any(piece in ('', '.', '..') for piece in name.split('/'))
                    or prefix + name not in names):
                raise ValueError('Invalid evidence member.')
            with archive.open(prefix + name) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError('Primary evidence file digest mismatch.')
        result = inspect_projection(project_report(report, hashlib.sha256(raw).hexdigest()))
        return {**result, 'primary_manifest_verified': True,
                'notice': 'Checked primary archive file digests and outcome format. Associated histories and replay policies require ReproLab.'}


def main():
    if len(sys.argv) != 2:
        print('Usage: python -I project_projection_format.py PROJECTION_OR_ZIP', file=sys.stderr)
        return 2
    location = Path(sys.argv[1])
    try:
        result = read_archive(location) if location.suffix.lower() == '.zip' else inspect_projection(read_projection(location))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
