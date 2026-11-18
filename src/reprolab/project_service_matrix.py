"""Run a pytest candidate against combinations of frozen, local Python services."""
from contextlib import ExitStack
import itertools
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

from .git_source import export_revision, resolve_commit
from .persistence import save_json
from .process_tree import spawn, terminate_tree
from .project_collection import normalize_collection_path
from .project_history import _checked_report
from .project_review_summary import _review_summary
from .project_selections import validate_selection
from .project_sessions import prepare_project, run_project
from .runner import source_fingerprint, source_mode_fingerprint
from .runtime import child_environment, choose_runtime

_KIND = 'reprolab_service_matrix'
_RESULT = 'service-matrix.json'


def _recipe(location):
    path = Path(location).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('请选择 2 MiB 以内的服务组合方案。')
    value = json.loads(path.read_bytes())
    if (not isinstance(value, dict) or value.keys() != {'schema_version', 'consumer', 'services'}
            or value['schema_version'] != 1 or not isinstance(value['consumer'], dict)
            or value['consumer'].keys() != {'repository', 'before', 'after', 'tests'}
            or not isinstance(value['services'], list) or not 1 <= len(value['services']) <= 8):
        raise ValueError('服务组合方案格式无效。')
    consumer = value['consumer']
    for key in ('repository', 'before', 'after'):
        if not isinstance(consumer[key], str) or not consumer[key]:
            raise ValueError('消费者仓库、基线或候选不能为空。')
    validate_selection(consumer['tests'])
    aliases, variables = set(), set()
    combinations = 1
    for service in value['services']:
        if (not isinstance(service, dict) or service.keys() != {'name', 'repository', 'refs', 'entry', 'port_env'}
                or any(not isinstance(service[key], str) or not service[key] for key in ('name', 'repository', 'entry', 'port_env'))
                or not service['port_env'].isascii() or not service['port_env'].isidentifier()
                or service['port_env'].startswith('REPROLAB_') or service['name'] in aliases
                or service['port_env'] in variables or not isinstance(service['refs'], list)
                or not 1 <= len(service['refs']) <= 20
                or any(not isinstance(ref, str) or not ref for ref in service['refs'])):
            raise ValueError('每个服务须提供唯一名称和端口变量、仓库、Git 版本与入口。')
        entry = normalize_collection_path(service['entry'])
        if not entry.endswith('.py'):
            raise ValueError('当前服务入口须是仓库内的 Python 文件。')
        aliases.add(service['name'])
        variables.add(service['port_env'])
        combinations *= len(service['refs'])
    if combinations > 20:
        raise ValueError('服务版本组合超过 20，请分批运行。')
    return path.parent, value


def _fresh_ports(count):
    with ExitStack() as stack:
        listeners = [stack.enter_context(socket.socket()) for _ in range(count)]
        for listener in listeners:
            listener.bind(('127.0.0.1', 0))
        return [listener.getsockname()[1] for listener in listeners]


class _LocalServices:
    def __init__(self, plan, stage, ports, executable, output, control):
        self.plan, self.stage, self.ports = plan, stage, ports
        self.executable, self.output, self.control = executable, output, control
        self.active = None

    def stop(self):
        if self.active is not None:
            self.active.close()
            self.active = None

    def start(self, label):
        self.stop()
        stack = self.active = ExitStack()
        for number, (service, port) in enumerate(zip(self.plan, self.ports), 1):
            target = stack.enter_context(tempfile.TemporaryDirectory(prefix='reprolab-local-service-'))
            work = Path(target) / 'source'
            shutil.copytree(self.stage / service['snapshot'], work)
            marker = Path(target) / 'ready'
            output = stack.enter_context((self.output / f'{label}-{number}.log').open('wb'))
            env = {**child_environment(), 'REPROLAB_SERVICE_PORT': str(port), 'REPROLAB_READY_FILE': str(marker)}
            launcher = 'import os,runpy,sys; sys.path.insert(0,os.getcwd()); runpy.run_path(sys.argv[1],run_name="__main__")'
            process = spawn([self.executable, '-I', '-c', launcher, str(work / service['entry'])], cwd=work, env=env,
                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
            stack.callback(terminate_tree, process)
            deadline = time.monotonic() + 10
            while True:
                self.control.check()
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError(f'{service["name"]} 未在 10 秒内启动，详见保存的服务日志。')
                if marker.is_file():
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2):
                            break
                    except OSError:
                        pass
                time.sleep(.05)


def run_service_matrix(location, output, *, confirmed, python_executable, timeout, control):
    root, plan = _recipe(location)
    if not confirmed:
        return {'status': 'ready', 'consumer': plan['consumer'], 'services': plan['services']}, 0
    output = Path(output).expanduser().absolute()
    consumer = plan['consumer']
    repositories = [(root / item['repository']).resolve() for item in (consumer, *plan['services'])]
    if output.exists() or output.is_symlink() or any(output.resolve().is_relative_to(path) for path in repositories):
        raise ValueError('请选择不在任何被测仓库内的新输出目录。')
    runtime = choose_runtime(python_executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.service-matrix-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'matrix'
        (stage / 'services').mkdir(parents=True)
        versions = []
        for count, (service, repo) in enumerate(zip(plan['services'], repositories[1:]), 1):
            pool = []
            seen = set()
            for ref in service['refs']:
                commit = resolve_commit(repo, ref)
                if commit in seen:
                    raise ValueError('同一服务的方案中出现重复提交。')
                seen.add(commit)
                relative = f'services/{count:02d}-{len(pool) + 1:02d}'
                snapshot = stage / relative
                export_revision(repo, commit, snapshot)
                entry = normalize_collection_path(service['entry'])
                if not (snapshot / entry).is_file():
                    raise ValueError('指定服务入口在固定提交中缺失。')
                pool.append({'name': service['name'], 'commit': commit, 'entry': entry, 'snapshot': relative,
                             'port_env': service['port_env'], 'sha256': source_fingerprint(snapshot)[1],
                             'mode_sha256': source_mode_fingerprint(snapshot)})
            versions.append(pool)
        state = prepare_project(repositories[0], consumer['before'], consumer['after'], stage / 'sessions',
                                python_executable=runtime['executable'])
        cells = []
        for number, selected in enumerate(itertools.product(*versions), 1):
            control.check()
            folder = stage / 'cells' / f'{number:02d}'
            folder.mkdir(parents=True)
            ports = _fresh_ports(len(selected))
            services = _LocalServices(selected, stage, ports, runtime['executable'], folder, control)
            def progress(phase, details):
                if phase in ('project_before', 'project_after'):
                    services.start(phase)
                control.progress(phase, details)

            child = control.child(progress=progress)
            child.runtime_values.update({item['port_env']: str(port) for item, port in zip(selected, ports)})
            case = {'cell': number, 'services': [item['snapshot'] for item in selected]}
            try:
                services.start('collection')
                report = run_project(state['directory'], folder / 'reports', selected_ids=consumer['tests'],
                                     python_executable=runtime['executable'], timeout=timeout, confirmed=True,
                                     control=child)
                checked = _checked_report(report['report_dir'], control=control)
                case.update(report=Path(checked['report_dir']).relative_to(stage).as_posix(),
                            report_sha256=checked['_report_sha256'],
                            gate=_review_summary(checked, False, 'strict', None)['gate'])
            except (OSError, ValueError) as exc:
                case['issue'] = str(exc)
            finally:
                services.stop()
            cells.append(case)
        record = {'kind': _KIND, 'schema_version': 1, 'consumer_source_sha256': state['source_sha256'],
                  'tests': consumer['tests'], 'versions': versions, 'cells': cells}
        save_json(stage / _RESULT, record)
        control.commit()
        if output.exists() or output.is_symlink():
            raise ValueError('服务组合目标已存在。')
        stage.rename(output)
    return {**record, 'directory': str(output)}, _exit_code(cells)


def _exit_code(cells):
    states = [(case.get('gate') or {}).get('status') for case in cells]
    return 1 if 'failed' in states else 2 if any(state != 'passed' for state in states) else 0


def _safe(root, name):
    path = root / normalize_collection_path(name)
    if path == root or not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError('服务矩阵材料路径无效。')
    return path


def check_service_matrix(directory, *, control):
    root = Path(directory).expanduser().resolve()
    manifest = root / _RESULT
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('请选择完整的服务矩阵。')
    record = json.loads(manifest.read_bytes())
    if record.get('kind') != _KIND or record.get('schema_version') != 1:
        raise ValueError('无法识别该服务矩阵。')
    for service in itertools.chain.from_iterable(record['versions']):
        control.check()
        snapshot = _safe(root, service['snapshot'])
        if source_fingerprint(snapshot)[1] != service['sha256'] or source_mode_fingerprint(snapshot) != service['mode_sha256']:
            raise ValueError('服务固定源码或权限指纹不一致。')
    matrix = [[service['snapshot'] for service in selected] for selected in itertools.islice(
        itertools.product(*record['versions']), 21)]
    if len(matrix) > 20 or matrix != [cell['services'] for cell in record['cells']]:
        raise ValueError('服务版本与矩阵格不一致。')
    for case in record['cells']:
        if 'report' not in case:
            if not case.get('issue'):
                raise ValueError('服务矩阵格缺少报告。')
            continue
        report = _checked_report(_safe(root, case['report']), control=control)
        if (report['_report_sha256'] != case['report_sha256']
                or report['source_sha256'] != record['consumer_source_sha256']
                or report['selected_tests'] != record['tests']
                or _review_summary(report, False, 'strict', None)['gate'] != case['gate']):
            raise ValueError('其消费者报告与矩阵格不一致。')
    return {**record, 'integrity': 'consistent', 'exit_code': _exit_code(record['cells'])}
