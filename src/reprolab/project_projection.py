"""Export a safe known-field projection and independently check it against a report."""
import json
from pathlib import Path

from .persistence import atomic_write
from .project_history import _checked_report
from .project_projection_format import inspect_projection, project_report, read_projection
from .replay_verification import open_replay_evidence


def _from_source(location, control):
    with open_replay_evidence(location, control=control) as folder:
        report = _checked_report(folder, control=control)
        value = project_report(report, report['_report_sha256'])
        inspect_projection(value)
        return value, Path(report['report_dir'])


def export_projection(location, output, *, control):
    record, source = _from_source(location, control)
    destination = Path(output).expanduser().absolute()
    if destination.resolve().is_relative_to(source):
        raise ValueError('请将脱敏投影保存到原报告之外。')
    control.commit()
    atomic_write(destination, json.dumps(record, ensure_ascii=True, indent=2), replace=False)
    return {'status': 'exported', 'output': str(destination), 'source_report_sha256': record['source_report_sha256'],
            'replayable': False}


def check_projection(location, *, source=None, control):
    value = read_projection(location)
    result = inspect_projection(value)
    if source is not None:
        original, _ = _from_source(source, control)
        if original != value:
            raise ValueError('脱敏投影与经校验的原报告不一致。')
        result['source_verified'] = True
    return result
