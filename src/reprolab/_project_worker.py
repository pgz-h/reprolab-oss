"""Standalone multi-test pytest worker for trusted project regression sessions."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest
from _pytest.skipping import xfailed_key


class Recorder:
    def __init__(self, selected, collecting, journal=None):
        self.journal = journal
        self.sequence = 0
        self.selected = set(selected)
        self.collecting = collecting
        self.collected = []
        self.started = []
        self.events = {}
        self.errors = []
        self.guard = {'status': 'allowed', 'reason': ''}

    def emit(self, event, **payload):
        if self.journal is not None:
            self.sequence += 1
            self.journal.write(json.dumps({'sequence': self.sequence, 'event': event, **payload}, ensure_ascii=False) + '\n')
            self.journal.flush()

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
        self.emit('collected', count=len(self.collected))

    def check(self, session, item=None):
        actual = [entry.nodeid for entry in session.items]
        extra = set(actual) - self.selected
        if (len(actual) > 2000 or len(actual) != len(set(actual)) or extra
                or (item is not None and (item.nodeid not in self.selected or item.nodeid in self.started))):
            self.guard = {'status': 'rejected', 'reason': '项目钩子改变了明确选择的测试；执行已停止。',
                          'unexpected_tests': sorted(extra)[:30]}
            self.emit('selection_rejected', reason=self.guard['reason'])
            raise pytest.UsageError(self.guard['reason'])

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtestloop(self, session):
        if not self.collecting:
            self.check(session)
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.check(item.session, item)
        self.started.append(item.nodeid)
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

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        result = yield
        report = result.get_result()
        exception = None
        if call.excinfo is not None:
            exception = {'type': call.excinfo.typename[:200], 'message': str(call.excinfo.value)[:2000]}
        self.events.setdefault(item.nodeid, []).append({
            'phase': report.when, 'outcome': report.outcome,
            'xfail': bool(hasattr(report, 'wasxfail') or item.stash.get(xfailed_key, None)),
            'exception': exception, 'duration_seconds': round(report.duration, 6),
        })
        self.emit('phase_result', test_id=item.nodeid, phase=report.when, result=self.events[item.nodeid][-1])


def main():
    source, destination, options_file = (Path(value).resolve() for value in sys.argv[1:4])
    options = json.loads(options_file.read_text(encoding='utf-8'))
    selected = options.get('selected_tests', [])
    collecting = options.get('collect_only') is True
    if (not isinstance(selected, list) or any(not isinstance(item, str) or not item or item.startswith('-')
                                            or len(item) > 4000 for item in selected)
            or (not collecting and not 1 <= len(selected) <= 2000)):
        raise ValueError('无效的项目测试选择。')
    os.chdir(source)
    sys.path[:0] = [str(source / 'src'), str(source)]
    journal = Path(options['progress_path']).open('w', encoding='utf-8') if options.get('progress_path') else None
    recorder = Recorder(selected, collecting, journal)
    recorder.emit('session', schema_version=1, mode='collect' if collecting else 'run',
                  selection_sha256=hashlib.sha256(json.dumps(selected, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
                  python_version=sys.version.split()[0], pytest_version=pytest.__version__,
                  pytest_profile=options.get('pytest_profile'), runtime_environment=options.get('runtime_environment'))
    collection_path = options.get('collection_path', '')
    targets = selected
    if collecting:
        targets = [str(source / collection_path)] if collection_path else []
    args = [*targets, '--rootdir', str(source), '--confcutdir', str(source),
            '-o', 'addopts=', *options.get('pytest_args', []), '-q', '--tb=short', '--color=no', '-p', 'no:cacheprovider',
            '--continue-on-collection-errors']
    if collecting:
        args.append('--collect-only')
    code = int(pytest.main(args, plugins=[recorder]))
    result = {'schema_version': 1, 'kind': 'project_worker', 'mode': 'collect' if collecting else 'run',
              'exit_code': code, 'requested_tests': selected, 'collection_path': collection_path,
              'collected': len(recorder.collected), 'collected_tests': recorder.collected[:10000],
              'collection_truncated': len(recorder.collected) > 10000,
              'started_tests': recorder.started, 'events': recorder.events,
              'selection_guard': recorder.guard, 'collection_errors': recorder.errors,
              'python_version': sys.version.split()[0], 'pytest_version': pytest.__version__,
              'python_executable': sys.executable, 'pytest_profile': options.get('pytest_profile'),
              'runtime_environment': options.get('runtime_environment')}
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    recorder.emit('session_finish', exit_code=code)
    if journal is not None:
        journal.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
