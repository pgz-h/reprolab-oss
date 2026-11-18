"""Shrink explicitly captured JSON values while preserving a recorded baseline failure."""
import json
from pathlib import Path
from uuid import uuid4

from .persistence import save_json
from .project_failure_waivers import _failure_identity
from .project_history import _checked_report
from .project_input_control import CONTROL_FILE, control_metadata, select_failure_inputs
from .project_sessions import _copy_session, _read, project_replay_settings, read_project, run_project
from .replay_verification import open_replay_evidence
from .runner import RunCancelled


def _smaller(value):
    if value is True:
        yield False
    elif type(value) in (int, float) and value:
        yield type(value)(0)
        half = value // 2 if type(value) is int else value / 2
        if half and abs(half) < abs(value):
            yield half
    elif isinstance(value, str) and value:
        yield ''
        if len(value) > 1:
            middle = len(value) // 2
            yield value[:middle]
            yield value[middle:]
    elif isinstance(value, list) and value:
        yield []
        if len(value) > 1:
            middle = len(value) // 2
            yield value[:middle]
            yield value[middle:]
        for index, old in enumerate(value):
            if len(value) > 1:
                yield value[:index] + value[index + 1:]
            for new in _smaller(old):
                yield [*value[:index], new, *value[index + 1:]]
    elif isinstance(value, dict) and value:
        yield {}
        for key, old in value.items():
            if len(value) > 1:
                yield {name: item for name, item in value.items() if name != key}
            for new in _smaller(old):
                yield {**value, key: new}


def _variants(values):
    for key, value in values.items():
        for smaller in _smaller(value):
            yield {**values, key: smaller}


def reduce_input(location, output, *, test_id, budget, python_executable, timeout, confirmed, control):
    if not 2 <= budget <= 100:
        raise ValueError('样本缩减探针预算须为 2–100。')
    output = Path(output).expanduser().resolve()
    with open_replay_evidence(location, control=control) as source:
        original = _checked_report(source, control=control)
        record = select_failure_inputs(original, [test_id])
        failure = _failure_identity(original['before']['tests'][test_id])
        if not failure:
            raise ValueError('基线缺少可核对的失败指纹。')
        result = {'kind': 'reprolab_input_reduction', 'schema_version': 1, 'status': 'ready',
                  'source_report_sha256': original['_report_sha256'],
                  'source_sha256': original['source_sha256'], 'test_id': test_id,
                  'failure_sha256': failure, 'probes': [], 'budget': budget,
                  'source_value': record['values'][test_id], 'value': None,
                  'notice': '仅采纳保持原基线失败指纹的较小 JSON 变体。局部缩减不会证明全局最小。'}
        if not confirmed:
            return result, 0
        if output.exists() or output.is_symlink() or any(output.is_relative_to(Path(value)) for value in
                                                        (original['report_dir'], original['repository'])):
            raise ValueError('请选择报告及仓库之外的新样本缩减目录。')
        output.mkdir(parents=True)
        index = output / 'input-reduction.json'
        save_json(index, result)
        session = output / ('session-' + uuid4().hex)
        session.mkdir()
        state = read_project(Path(original['report_dir']) / 'evidence/project')
        _copy_session(state, session)
        settings = project_replay_settings(original, python_executable=python_executable, timeout=timeout)

        def probe(values):
            control.check()
            sampled = {**record, 'values': {test_id: values}}
            replay = run_project(session, output / 'reports', selected_ids=[test_id], confirmed=True,
                                 python_executable=settings['runtime']['executable'], timeout=settings['timeout'],
                                 input_control=sampled, control=control.child(progress=control.progress))
            observed = _checked_report(replay['report_dir'])
            status = _observed_failure(observed, test_id, failure)
            result['probes'].append({'report': Path(replay['report_dir']).relative_to(output).as_posix(),
                                     'report_sha256': observed['_report_sha256'], 'status': status,
                                     'value': values})
            save_json(index, result)
            return status

        try:
            current = result['source_value']
            if probe(current) != 'matched':
                result['status'] = 'incomplete'
            else:
                result['value'] = current
                tried = {json.dumps(current, sort_keys=True)}
                result['status'] = 'unchanged'
                while True:
                    changed = False
                    for candidate in _variants(current):
                        key = json.dumps(candidate, sort_keys=True)
                        if key in tried:
                            continue
                        if len(result['probes']) >= budget:
                            result['status'] = 'incomplete'
                            break
                        tried.add(key)
                        observed = probe(candidate)
                        if observed == 'matched':
                            current = result['value'] = candidate
                            changed = True
                            result['status'] = 'reduced'
                            break
                        if observed == 'incomplete':
                            result['status'] = 'incomplete'
                            break
                    if not changed:
                        break
        except (RunCancelled, KeyboardInterrupt):
            result['status'] = 'cancelled'
        save_json(index, result)
        return {**result, 'summary_file': str(index)}, 0 if result['status'] in ('reduced', 'unchanged') else 2


def _observed_failure(report, test_id, fingerprint):
    observed = report['before']['tests'].get(test_id, {})
    if report['before']['status'] != 'completed' or report['status'] == 'cancelled':
        return 'incomplete'
    if observed.get('status') == 'failed':
        return 'matched' if _failure_identity(observed) == fingerprint else 'different_failure'
    return 'passed' if observed.get('status') == 'passed' else 'incomplete'


def check_input_reduction(directory, *, source=None, control):
    root = Path(directory).expanduser().resolve()
    summary = root / 'input-reduction.json'
    if summary.is_symlink() or not summary.is_file() or summary.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('请选择完整的样本缩减目录。')
    result = json.loads(summary.read_bytes())
    if result.get('kind') != 'reprolab_input_reduction' or result.get('schema_version') != 1:
        raise ValueError('无法识别该样本缩减记录。')
    if source is not None:
        with open_replay_evidence(source, control=control) as folder:
            original = _checked_report(folder, control=control)
            sample = select_failure_inputs(original, [result['test_id']])
            if (sample['source_report_sha256'] != result['source_report_sha256']
                    or sample['values'][result['test_id']] != result['source_value']
                    or _failure_identity(original['before']['tests'][result['test_id']]) != result['failure_sha256']):
                raise ValueError('样本缩减记录与来源失败不一致。')
    accepted = None
    for number, trial in enumerate(result['probes']):
        if (number == 0 and trial['value'] != result['source_value']
                or number > 0 and (accepted is None or not any(candidate == trial['value'] for candidate in _variants(accepted)))):
            raise ValueError('试探不是上一个保留样本的缩减变体。')
        control.check()
        path = (root / trial['report']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('样本试探报告路径越界。')
        report = _checked_report(path, control=control)
        test_id = result['test_id']
        record = {'source_report_sha256': result['source_report_sha256'], 'values': {test_id: trial['value']}}
        if (report['_report_sha256'] != trial['report_sha256'] or report['source_sha256'] != result['source_sha256']
                or report['selected_tests'] != [test_id]
                or report['input_control'] != control_metadata(record, [test_id])
                or _read(path / CONTROL_FILE) != record
                or _observed_failure(report, test_id, result['failure_sha256']) != trial['status']):
            raise ValueError('保存的缩减试探与固定报告不一致。')
        if trial['status'] == 'matched':
            accepted = trial['value']
    if accepted != result['value']:
        raise ValueError('保存的缩减样本与通过核对的试探不一致。')
    return {**result, 'integrity': 'consistent', 'source_verified': source is not None}
