"""Reduce a failing pytest sequence without claiming a global minimum."""
import json
from pathlib import Path
from uuid import uuid4

from .persistence import atomic_write, save_json
from .project_failure_waivers import _failure_identity
from .project_history import _checked_report
from .project_order import order_control_completed
from .replay_verification import open_replay_evidence
from .runner import RunCancelled


def minimize_order(location, output, *, test_id, confirmed, python_executable, timeout, budget, control):
    from .project_sessions import _copy_session, project_replay_settings, read_project, run_project

    if not 2 <= budget <= 100:
        raise ValueError('最小化尝试上限须为 2–100。')
    output = Path(output).expanduser().resolve()
    with open_replay_evidence(location, control=control) as source:
        original = _checked_report(source, control=control)
        if (original.get('runner', 'pytest') != 'pytest' or original['after']['status'] != 'completed'
                or any(original.get(key) for key in ('fixture_control', 'version_order', 'input_control', 'test_source_control'))):
            raise ValueError('需要候选已完成的原生 pytest 报告。')
        outcome = original['after']['tests'].get(test_id, {})
        observed_inputs = ('input_digests', 'parameter_digests')
        signature = _failure_identity(outcome) if outcome.get('status') == 'failed' else None
        order = original['after'].get('execution_order', [])
        if not signature or test_id not in order:
            raise ValueError('请选择已记录可核对失败和启动位置的候选测试。')
        start = order[:order.index(test_id)]
        if not start:
            raise ValueError('原测试前没有可缩减的执行项。')
        if any(output.is_relative_to(Path(path).resolve()) for path in (original['report_dir'], original['repository'])):
            raise ValueError('对照输出须在来源报告与被测仓库之外。')
        settings = project_replay_settings(original, python_executable=python_executable, timeout=timeout)
        result = {'kind': 'project_order_reduction', 'schema_version': 1, 'status': 'ready', 'test_id': test_id,
                  'source_report_sha256': original['_report_sha256'], 'failure_sha256': signature,
                  'original_predecessors': start, 'predecessors': None, 'probes': [], 'budget': budget,
                  'notice': '只核对能否逐个删除前置项，记录允许继续复核；外部状态或非确定性可能影响每次执行。'}
        if not confirmed:
            return result, 0

        output.mkdir(parents=True, exist_ok=True)
        summary_file = output / 'order-reduction.json'
        atomic_write(summary_file, json.dumps(result, ensure_ascii=False, indent=2), replace=False)
        state = read_project(Path(original['report_dir']) / 'evidence/project')
        clone = output / ('frozen-session-' + uuid4().hex)
        clone.mkdir()
        _copy_session(state, clone)

        def probe(predecessors):
            control.check()
            selected = [*predecessors, test_id]
            order_control = {'mode': 'requested', 'source_report_sha256': original['_report_sha256'], 'test_ids': selected}
            run = run_project(clone, output / 'reports', selected_ids=selected, confirmed=True,
                              python_executable=settings['runtime']['executable'], timeout=settings['timeout'],
                              order_control=order_control, control=control.child(progress=control.progress))
            checked = _checked_report(run['report_dir'])
            observed = checked['after']['tests'].get(test_id, {})
            comparison = 'incomplete'
            if order_control_completed(checked) and all(
                    key not in outcome or outcome[key] == observed.get(key) for key in observed_inputs):
                if observed['status'] == 'failed' and _failure_identity(observed) == signature:
                    comparison = 'matched'
                elif observed['status'] == 'passed':
                    comparison = 'passed'
            result['probes'].append({'predecessors': predecessors, 'status': comparison,
                                     'report_dir': checked['report_dir'], 'report_sha256': checked['_report_sha256']})
            save_json(summary_file, result)
            return comparison

        try:
            if probe(start) == 'matched':
                current = start
                result['predecessors'] = current
                # Repeat after every removal because an earlier unsuccessful deletion may become possible.
                index = 0
                while index < len(current) and len(result['probes']) < budget:
                    trial = current[:index] + current[index + 1:]
                    observed = probe(trial)
                    if observed == 'incomplete':
                        break
                    if observed == 'matched':
                        current = trial
                        result['predecessors'] = current
                        index = 0
                    else:
                        index += 1
                result['status'] = 'one_minimal' if index == len(current) else 'incomplete'
            else:
                result['status'] = 'incomplete'
        except (RunCancelled, KeyboardInterrupt):
            result['status'] = 'cancelled'
        save_json(summary_file, result)
        return {**result, 'summary_file': str(summary_file)}, 0 if result['status'] == 'one_minimal' else 2
