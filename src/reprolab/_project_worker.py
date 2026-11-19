"""Standalone multi-test pytest worker for trusted project regression sessions."""
import ast
import hashlib
from inspect import signature
import json
import locale
import os
from pathlib import Path
from runpy import run_path
import shlex
import sys
import time
from types import ModuleType

import pytest
from _pytest.config.findpaths import determine_setup
from _pytest.skipping import xfailed_key


if __package__:
    from ._assertion_sites import assertion_site_digest, assertions_by_scope
    from ._fixture_bindings import collect_fixture_bindings
    from ._input_samples import InputCapture, sample_digest
else:
    # The external interpreter runs this script in isolated mode without ReproLab installed.
    _samples = run_path(str(Path(__file__).with_name('_input_samples.py')))
    InputCapture, sample_digest = _samples['InputCapture'], _samples['sample_digest']
    _sites = run_path(str(Path(__file__).with_name('_assertion_sites.py')))
    assertion_site_digest, assertions_by_scope = _sites['assertion_site_digest'], _sites['assertions_by_scope']
    collect_fixture_bindings = run_path(str(Path(__file__).with_name('_fixture_bindings.py')))['collect_fixture_bindings']


def warning_policy(source, targets):
    """Use pytest's config resolution and take only its warning-filter options."""
    options = {'inifile': None, 'args': targets, 'rootdir_cmd_arg': str(source), 'invocation_dir': source}
    # pytest 9.2 adds explicit INI overrides to setup and wraps the loaded values.
    if 'override_ini' in signature(determine_setup).parameters:
        options['override_ini'] = None
    _, path, config, *_ = determine_setup(**options)
    rule = {'config_path': None, 'warning_filters': 0, 'other_addopts_ignored': False}
    if path is None or not path.is_relative_to(source):
        return rule, []
    rule['config_path'] = path.relative_to(source).as_posix()
    configured = config.get('addopts', '')
    configured = getattr(configured, 'value', configured)
    tokens = iter(shlex.split(configured) if isinstance(configured, str) else configured)
    filters = []
    for token in tokens:
        if token == '--':
            rule['other_addopts_ignored'] = True
            break
        if token in ('-W', '--pythonwarnings'):
            value = next(tokens, None)
            if value is None or value.startswith('-'):
                raise pytest.UsageError(f'{rule["config_path"]}: addopts 中的 {token} 缺少警告筛选值。')
        elif token.startswith('--pythonwarnings='):
            value = token.split('=', 1)[1]
        elif token.startswith('-W'):
            value = token[2:].removeprefix('=')
        else:
            rule['other_addopts_ignored'] = True
            continue
        filters.append('--pythonwarnings=' + value)
    rule['warning_filters'] = len(filters)
    return rule, filters


class Recorder:
    def __init__(self, selected, collecting, journal=None, *, source=None, watched_modules=(), fixed_order=None,
                 input_values=None, record_fixture_bindings=False):
        self.journal = journal
        self.sequence = 0
        self.selected = set(selected)
        self.collecting = collecting
        self.collected = []
        self.record_fixture_bindings = record_fixture_bindings
        self.fixture_bindings = None
        self.started = []
        self.started_ids = set()
        self.parameter_digests = {}
        self.fixed_order = fixed_order
        self.source = source
        self.watched_modules = set(watched_modules)
        self.module_origins = {}
        self._origin_cache = {}
        self._assertions = {}
        self.events = {}
        self.errors = []
        self.guard = {'status': 'allowed', 'reason': ''}
        self.input_capture = InputCapture(input_values or {})

    @pytest.fixture
    def reprolab_input(self, request):
        def read(name, factory):
            return self.input_capture.read(request.node.nodeid, name, factory)
        return read

    def emit(self, event, **payload):
        if self.journal is not None:
            self.sequence += 1
            self.journal.write(json.dumps({'sequence': self.sequence, 'event': event, **payload}, ensure_ascii=False) + '\n')
            self.journal.flush()

    def _record_imports(self):
        if self.collecting or not self.watched_modules:
            return
        for name, module in sys.modules.copy().items():
            if name not in self.watched_modules or not isinstance(module, ModuleType):
                continue
            values = vars(module)
            origin = values.get('__file__') or getattr(values.get('__spec__'), 'origin', None)
            if not isinstance(origin, str):
                origin = ''
            if origin not in self._origin_cache:
                self._origin_cache[origin] = self._classify_import(origin)
            self.module_origins.setdefault(name, set()).add(self._origin_cache[origin])

    def _classify_import(self, origin):
        if origin in ('built-in', 'frozen'):
            return origin
        if not origin or not Path(origin).is_absolute():
            return 'unknown'
        try:
            path = Path(origin).resolve()
        except (OSError, RuntimeError, ValueError):
            return 'unknown'
        if path.is_relative_to(self.source):
            return 'snapshot:' + path.relative_to(self.source).as_posix()
        if any(part in ('site-packages', 'dist-packages') for part in path.parts):
            return 'installed-package'
        return 'external'

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session):
        self._record_imports()
        self.collected = [item.nodeid for item in session.items]
        self.emit('collected', count=len(self.collected))
        if self.collecting and self.record_fixture_bindings:
            self.fixture_bindings = collect_fixture_bindings(session.items, self.source)

    def _check_inventory(self, session):
        actual = [entry.nodeid for entry in session.items]
        extra = set(actual) - self.selected
        if len(actual) > 2000 or len(actual) != len(set(actual)) or extra:
            self._reject_selection(extra)

    def _reject_selection(self, extra=()):
        self.guard = {'status': 'rejected', 'reason': '项目钩子改变了明确选择的测试；执行已停止。',
                      'unexpected_tests': sorted(extra)[:30]}
        self.emit('selection_rejected', reason=self.guard['reason'])
        raise pytest.UsageError(self.guard['reason'])

    def _record_parameters(self, item):
        parameters = getattr(getattr(item, 'callspec', None), 'params', {})
        if not parameters:
            return
        if len(parameters) > 64:
            self.parameter_digests[item.nodeid] = {'too_many': None}
            return
        digests = {}
        for name, value in parameters.items():
            try:
                digests[name] = sample_digest(value)
            except (ValueError, TypeError):
                digests[name] = None
        self.parameter_digests[item.nodeid] = digests

    def _reject_order(self):
        self.guard = {'status': 'rejected', 'reason': '未能按对照顺序运行全部所选测试。'}
        self.emit('selection_rejected', reason=self.guard['reason'])
        raise pytest.UsageError(self.guard['reason'])

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtestloop(self, session):
        if not self.collecting:
            self._check_inventory(session)
            if self.fixed_order is not None:
                if len(session.items) != len(self.fixed_order):
                    self._reject_order()
                index = {name: position for position, name in enumerate(self.fixed_order)}
                session.items.sort(key=lambda item: index[item.nodeid])
        result = yield
        if not self.collecting:
            self._check_inventory(session)
        return result

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if item.nodeid not in self.selected or item.nodeid in self.started_ids:
            self._reject_selection({item.nodeid} - self.selected)
        if self.fixed_order is not None and item.nodeid != self.fixed_order[len(self.started)]:
            self._reject_order()
        self.started.append(item.nodeid)
        self.started_ids.add(item.nodeid)
        self._record_parameters(item)
        self.emit('test_start', test_id=item.nodeid)
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.emit('phase_start', test_id=item.nodeid, phase='setup')
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.emit('phase_start', test_id=item.nodeid, phase='call')
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        self.emit('phase_start', test_id=item.nodeid, phase='teardown')
        return (yield)

    def pytest_collectreport(self, report):
        if report.failed and len(self.errors) < 100:
            self.errors.append(str(report.longrepr)[-6000:])
            self.emit('collection_error', message=self.errors[-1])

    def _assertion_site(self, traceback):
        if traceback is None:
            return None
        # The innermost frame is where the error surfaced, including assertions in project helpers.
        while traceback.tb_next is not None:
            traceback = traceback.tb_next
        code = traceback.tb_frame.f_code
        path = Path(code.co_filename)
        try:
            if not path.is_absolute() or self.source is None:
                return None
            path = path.resolve()
            relative = path.relative_to(self.source).as_posix()
            if path not in self._assertions:
                tree = ast.parse(path.read_bytes())
                self._assertions[path] = assertions_by_scope(tree)
            line = traceback.tb_lineno
            scopes = self._assertions[path]
            matches = [(scope, node) for scope, nodes in scopes.items()
                       for node in nodes if node.lineno <= line <= node.end_lineno]
            if len(matches) > 1:
                return {'assertion_site_sha256': None, 'assertion_location_ambiguous': True}
            if not matches:
                return None
            scope, assertion = matches[0]
            predicate = ast.dump(assertion.test)
            if any(key != scope and key[1] == scope[1] for key in scopes):
                return {'assertion_site_sha256': None, 'assertion_location_ambiguous': True}
            peers = sorted((node for node in scopes[scope] if ast.dump(node.test) == predicate),
                           key=lambda node: (node.lineno, node.col_offset))
            structure = hashlib.sha256(ast.dump(scope[0]).encode()).hexdigest() if len(peers) > 1 else None
        except (OSError, ValueError, RuntimeError, SyntaxError):
            return None
        return {
            'assertion_site_sha256': assertion_site_digest(relative, code.co_qualname, predicate),
            'assertion_occurrence': {'index': peers.index(assertion) + 1, 'count': len(peers), 'scope_sha256': structure},
        }

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        result = yield
        report = result.get_result()
        self._record_imports()
        exception = None
        if call.excinfo is not None:
            value = call.excinfo.value
            exception = {'type': call.excinfo.typename[:200], 'message': str(value)[:2000]}
            if isinstance(value, OSError) and type(value.errno) is int:
                exception['errno'] = value.errno
        observation = {
            'phase': report.when, 'outcome': report.outcome,
            'xfail': bool(hasattr(report, 'wasxfail') or item.stash.get(xfailed_key, None)),
            'exception': exception, 'duration_seconds': round(report.duration, 6),
        }
        if (report.when == 'call' and report.failed and not observation['xfail']
                and exception and exception['type'] == 'AssertionError'):
            site = self._assertion_site(value.__traceback__) if type(value) is AssertionError else None
            observation.update(site or {'assertion_site_sha256': None})
        self.events.setdefault(item.nodeid, []).append(observation)
        self.emit('phase_result', test_id=item.nodeid, phase=report.when, result=observation)


def main():
    source, destination, options_file = (Path(value).resolve() for value in sys.argv[1:4])
    options = json.loads(options_file.read_text(encoding='utf-8'))
    selected = options.get('selected_tests', [])
    collecting = options.get('collect_only') is True
    if (not isinstance(selected, list) or any(not isinstance(item, str) or not item or item.startswith('-')
                                            or len(item) > 4000 for item in selected)
            or (not collecting and not 1 <= len(selected) <= 2000)):
        raise ValueError('无效的项目测试选择。')
    if clock := options.get('clock_control'):
        if __package__:
            from ._clock_control import freeze_worker_clock
        else:
            freeze_worker_clock = run_path(str(Path(__file__).with_name('_clock_control.py')))['freeze_worker_clock']
        freeze_worker_clock(clock)
    os.chdir(source)
    sys.path[:0] = [str(source / 'src'), str(source)]
    collection_path = options.get('collection_path', '')
    targets = selected
    if collecting:
        targets = [str(source / collection_path)] if collection_path else []
    fixture_hashes = {}
    for name in options.get('fixture_paths', ()):
        with (source / name).open('rb') as stream:
            fixture_hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    rule, warning_args = warning_policy(source, targets)
    fixed_order = options.get('fixed_order')
    if fixed_order is not None:
        rule['order_sha256'] = hashlib.sha256(
            json.dumps(fixed_order, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    journal = Path(options['progress_path']).open('w', encoding='utf-8') if options.get('progress_path') else None
    recorder = Recorder(selected, collecting, journal, source=source,
                        watched_modules=options.get('watched_modules', ()), fixed_order=fixed_order,
                        input_values=options.get('input_values'), record_fixture_bindings=options.get('record_fixture_bindings') is True)
    recorder.emit('session', schema_version=1, mode='collect' if collecting else 'run',
                  selection_sha256=hashlib.sha256(json.dumps(selected, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
                  python_version=sys.version.split()[0], pytest_version=pytest.__version__,
                  pytest_profile=options.get('pytest_profile'), runtime_environment=options.get('runtime_environment'),
                  pytest_rules=rule)
    args = [*targets, '--rootdir', str(source), '--confcutdir', str(source),
            '-o', 'addopts=', *options.get('pytest_args', []), *warning_args, '-q', '--tb=short', '--color=no', '-p', 'no:cacheprovider',
            '--continue-on-collection-errors']
    if collecting:
        args.append('--collect-only')
    # Record the initial context before trusted pytest setup can change it.
    context = {
        'locale': locale.setlocale(locale.LC_CTYPE),
        'utc_offset_seconds': getattr(time.localtime(), 'tm_gmtoff', -time.timezone),
        'time_and_locale_environment_sha256': hashlib.sha256(json.dumps(
            {name: os.environ.get(name) for name in ('LANG', 'LC_ALL', 'LC_TIME', 'LC_NUMERIC', 'TZ')},
            sort_keys=True).encode()).hexdigest(),
    }
    code = int(pytest.main(args, plugins=[recorder]))
    result = {'schema_version': 1, 'kind': 'project_worker', 'mode': 'collect' if collecting else 'run',
              'exit_code': code, 'requested_tests': selected, 'collection_path': collection_path,
              'collected': len(recorder.collected), 'collected_tests': recorder.collected[:10000],
              'collection_truncated': len(recorder.collected) > 10000,
              'started_tests': recorder.started, 'events': recorder.events,
              'pytest_rules': rule, 'module_origins': {name: sorted(origins) for name, origins in recorder.module_origins.items()},
              'selection_guard': recorder.guard, 'collection_errors': recorder.errors,
              'python_version': sys.version.split()[0], 'pytest_version': pytest.__version__,
              'python_executable': sys.executable, 'pytest_profile': options.get('pytest_profile'),
              'runtime_environment': options.get('runtime_environment')}
    if not collecting:
        result['runtime_context'] = context
    if recorder.fixture_bindings is not None:
        result['fixture_bindings'] = recorder.fixture_bindings
    if sys.platform == 'darwin' or sys.platform.startswith('linux'):
        import resource

        metrics = resource.getrusage(resource.RUSAGE_SELF)
        result['worker_resources'] = {
            'user_cpu_seconds': round(metrics.ru_utime, 6),
            'system_cpu_seconds': round(metrics.ru_stime, 6),
            'peak_rss_bytes': metrics.ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'scope': 'worker_process_only',
        }
    if clock is not None:
        result['clock_control'] = clock
    capture = recorder.input_capture
    if capture.values or capture.errors:
        result['input_samples'] = {'values': capture.values, 'errors': capture.errors}
    if recorder.parameter_digests:
        result['parameter_digests'] = recorder.parameter_digests
    if fixture_hashes:
        result['fixture_sha256'] = fixture_hashes
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    recorder.emit('session_finish', exit_code=code)
    if journal is not None:
        journal.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
