"""Execute a shared regression test in a trusted source copy."""
import json
import os
from pathlib import Path
import sys

import pytest
from _pytest.skipping import xfailed_key


class Recorder:
    def __init__(self, project_mode=False, collect_only=False):
        self.events = []
        self.collection_errors = []
        self.diagnostic_exceptions = []
        self.collected = 0
        self.has_xfail = False
        self.project_mode = project_mode
        self.selection_guard = None
        self.collect_only = collect_only
        self.session = None

    def pytest_collection_finish(self, session):
        self.session = session
        self.collected = len(session.items)

    def _test_id(self, item):
        return item.nodeid if self.project_mode else "regression.py::" + item.nodeid.split("::", 1)[-1]

    def _check_selection(self, session, item=None):
        self.collected = len(session.items)
        self.selection_guard = {
            "status": "allowed" if self.collected == 1 else "rejected",
            "collected": self.collected,
            "test_ids": [self._test_id(selected) for selected in session.items[:100]],
            "test_ids_truncated": self.collected > 100,
            "reason": "恰好选中一个测试。" if self.collected == 1 else
                      f"实际选中 {self.collected} 个测试；要求恰好一个。未开始 fixture 或测试主体。",
        }
        if self.collected == 1 and item is not None and item is not session.items[0]:
            self.selection_guard.update(status="rejected", reason="即将执行的测试与选中项不一致；已停止执行。")
        if self.selection_guard["status"] == "rejected":
            raise pytest.UsageError(self.selection_guard["reason"])

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtestloop(self, session):
        if not self.collect_only:
            self._check_selection(session)
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        # Project runtestloop hooks may change the selection after collection.
        self._check_selection(item.session, item)
        return (yield)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(str(report.longrepr)[-6000:])

    def pytest_exception_interact(self, node, call, report):
        # Pytest supplies the actual exception for collection and runtime errors.
        # This supplemental evidence never participates in the pass/fail decision.
        if call.excinfo is None or len(self.diagnostic_exceptions) >= 30:
            return
        value = call.excinfo.value
        # pytest wraps import/syntax failures in CollectError with an explicit
        # cause. Unwrap only that known framework wrapper, not arbitrary chains.
        if isinstance(value, pytest.Collector.CollectError) and value.__cause__ is not None:
            value = value.__cause__
        phase = getattr(report, "when", "collection")
        evidence = {"phase": "collection" if phase == "collect" else phase,
                    "type": type(value).__name__[:200], "message": str(value)[:6000]}
        if isinstance(value, ModuleNotFoundError) and value.name:
            evidence["name"] = str(value.name)[:500]
        self.diagnostic_exceptions.append(evidence)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        exception = None
        if call.excinfo is not None:
            exception = {"type": call.excinfo.typename, "message": str(call.excinfo.value)}
        # Reuse pytest's evaluated decision; do not re-evaluate marker expressions.
        effective_xfail = bool(item.stash.get(xfailed_key, None)) or hasattr(report, "wasxfail")
        self.has_xfail = self.has_xfail or effective_xfail
        test_id = self._test_id(item)
        self.events.append({
            "test_id": test_id, "phase": report.when, "outcome": report.outcome,
            "xfail": effective_xfail, "exception": exception,
            "observations": [{"name": str(name)[:200], "value": str(value)[:4000]}
                             for name, value in list(report.user_properties)[:30]],
        })


def main():
    source, test, result = (Path(p).resolve() for p in sys.argv[1:4])
    options = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
    target = options.get("test_target")
    collect_only = options.get("collect_only") is True
    os.chdir(source)
    # Import pytest before project files can shadow it.
    sys.path[:0] = [str(source / "src"), str(source)]
    recorder = Recorder(project_mode=bool(target) or collect_only, collect_only=collect_only)
    common = ["-q", "--tb=short", "--color=no", "-p", "no:cacheprovider"]
    if target or collect_only:
        args = [*([target] if target else []), "--rootdir", str(source), "--confcutdir", str(source),
                "-o", "addopts=", *options.get("pytest_args", []), *common]
    else:
        config = test.parent / "pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
        args = [str(test), "--import-mode=importlib", "--confcutdir", str(test.parent),
                "-c", str(config), *common]
    if collect_only:
        args.append("--collect-only")
    code = pytest.main(args, plugins=[recorder])
    items = recorder.session.items if recorder.session is not None else []
    result.write_text(json.dumps({
        "exit_code": int(code), "collected": len(items) if collect_only else recorder.collected,
        "collection_only": collect_only,
        "collected_tests": [item.nodeid for item in items[:5000]] if collect_only else [],
        "collection_truncated": collect_only and len(items) > 5000,
        "selection_guard": recorder.selection_guard, "pytest_profile": options.get("pytest_profile"),
        "runtime_environment": options.get("runtime_environment"),
        "has_xfail": recorder.has_xfail, "events": recorder.events,
        "collection_errors": recorder.collection_errors,
        "diagnostic_exceptions": recorder.diagnostic_exceptions,
        "pytest_version": pytest.__version__,
        "python_version": sys.version.split()[0], "python_executable": sys.executable,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
