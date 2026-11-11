"""Explain recorded execution evidence without changing the acceptance verdict.

Only structured pytest exceptions and phase outcomes support specific diagnoses.
Logs are deliberately not searched: printed text is not proof of an exception.
"""


def _diagnosis(code, summary, detail, *next_steps):
    return {"code": code, "summary": summary, "detail": detail, "next_steps": list(next_steps)}


def _exception_detail(exception):
    if not isinstance(exception, dict):
        return "未记录结构化异常。"
    return f"{str(exception.get('type', '未知异常'))[:200]}: {str(exception.get('message', ''))[:6000]}"


def _matches(exception, expected):
    return (isinstance(exception, dict) and isinstance(expected, dict)
            and isinstance(expected.get("message_contains"), str)
            and bool(expected["message_contains"])
            and exception.get("type") == expected.get("type")
            and expected["message_contains"] in str(exception.get("message", "")))


def _missing_module(exception):
    name = exception.get("name")
    detail = _exception_detail(exception)
    if name:
        detail = f"无法导入模块 {str(name)[:500]}。" + detail
    return _diagnosis("module_not_found", "无法导入模块或项目依赖。", detail,
                      "检查所选 Python 环境、项目源码与导入路径，并核对项目的依赖说明。",
                      "模块名不一定等于发行包名；确认所需项目依赖后再由你决定如何准备环境。")


def diagnose_run(run, *, label, expected=None):
    """Return an explanation of one saved run; never evaluate or mutate it."""
    run = run if isinstance(run, dict) else {}
    if run.get("timed_out"):
        return _diagnosis("timeout", "测试执行超时，证据不足。", "运行记录表明子进程已因超时而终止。",
                          "查看测试是否等待外部资源或陷入循环；确认合理耗时后调整时限并重跑。")

    if run.get("worker_record_complete") is False:
        return _diagnosis("missing_worker_record", "测试进程没有返回完整记录，无法判断。",
                          "缺少可用的工作进程记录；不能推断测试是否已收集或执行。",
                          "查看日志、进程退出方式和所选环境，重新运行以获取完整证据。")

    modern = isinstance(run.get("phase_events"), list)
    recorded_events = run.get("phase_events", []) if modern else []
    events = [event for event in recorded_events if isinstance(event, dict)]
    exceptions = run.get("diagnostic_exceptions", [])
    exceptions = [exc for exc in exceptions if isinstance(exc, dict)] if isinstance(exceptions, list) else []
    collection_errors = run.get("collection_errors")
    missing = next((exc for exc in exceptions if exc.get("type") == "ModuleNotFoundError"), None)

    if collection_errors or any(exc.get("phase") == "collection" for exc in exceptions):
        if missing and missing.get("phase") == "collection":
            return _missing_module(missing)
        exception = next((exc for exc in exceptions if exc.get("phase") == "collection"), None)
        return _diagnosis("collection_error", "测试收集失败，尚不能验收。",
                          _exception_detail(exception) if exception else "pytest 记录了测试收集错误；未记录可细分的异常。",
                          "查看收集错误，核对测试文件语法、导入和项目配置，再重跑。")
    guard = run.get("selection_guard")
    if isinstance(guard, dict) and guard.get("status") == "rejected":
        if guard.get("collected") == 1:
            return _diagnosis("selection_changed", "即将执行的测试与选中项不一致，已停止。",
                              guard.get("reason", "选择记录发生变化。"), "检查项目 pytest 钩子，确保只执行选中的一个测试。")
        empty = guard.get("collected") == 0
        return _diagnosis("no_tests" if empty else "multiple_tests",
                          "没有选中测试；已在执行前停止。" if empty else "选中多个测试；已在执行前停止。",
                          guard.get("reason", "未开始 fixture 或测试主体。"),
                          "把案例目标缩小到恰好一个测试；参数化测试需选择一个具体实例。")
    if run.get("exit_code") == 4:
        return _diagnosis("invalid_target", "测试目标或 pytest 参数不可用。", "pytest 返回使用错误（退出码 4）。",
                          "核对测试文件与目标标识，并查看日志中的 pytest 使用错误。")
    if run.get("exit_code") not in (None, 0, 1, 5):
        return _diagnosis("worker_error", "测试进程异常退出，证据不足。",
                          f"进程退出码：{run.get('exit_code')}。未据此推断项目故障。",
                          "查看执行日志和解释器环境，修正进程错误后重跑。")
    if run.get("collected") == 0 and modern:
        return _diagnosis("no_tests", "没有收集到测试。", "记录的测试数量为 0；每个案例需要恰好一个测试。",
                          "核对测试文件、函数命名和所选目标，确保只选中一个测试。")
    if isinstance(run.get("collected"), int) and run["collected"] > 1:
        return _diagnosis("multiple_tests", "收集到多个测试，不能进行单案例验收。",
                          f"实际收集 {run['collected']} 个测试；要求恰好一个。",
                          "把案例目标缩小到一个测试；参数化测试也需只选中一个实例。")
    if run.get("has_xfail") or any(event.get("xfail") for event in events):
        return _diagnosis("xfail", "测试标记为预期失败，不能用于验收。", "存在 xfail 标记或 xfail 阶段记录。",
                          "审阅测试意图，移除用于屏蔽故障的 xfail 标记后重跑。")
    if any(event.get("outcome") == "skipped" for event in events):
        return _diagnosis("skipped", "测试被跳过，没有完成验收。", "pytest 阶段记录包含 skipped。",
                          "核对跳过条件和所需资源，确保测试主体实际执行后重跑。")
    for phase, summary in (("setup", "测试准备或 fixture 阶段失败。"), ("teardown", "测试清理或 fixture 退出阶段失败。")):
        failed = next((event for event in events if event.get("phase") == phase and event.get("outcome") != "passed"), None)
        if failed:
            exception = next((exc for exc in exceptions if exc.get("phase") == phase), failed.get("exception"))
            detail = _exception_detail(exception)
            if isinstance(exception, dict) and exception.get("type") == "ModuleNotFoundError":
                detail += " 无法导入模块或项目依赖；此错误发生在测试主体之外。"
            return _diagnosis(phase + "_error", summary, detail,
                              "查看该阶段的异常，检查 fixture、资源准备与清理逻辑，再重跑。")
    if modern and (len(recorded_events) != 3 or len(events) != 3 or [event.get("phase") for event in events] != ["setup", "call", "teardown"]):
        return _diagnosis("incomplete_events", "测试阶段记录不完整。", "需要依次记录 setup、call、teardown 三个阶段。",
                          "查看执行日志，确认测试进程正常结束并重新运行以取得完整证据。")

    if modern and (run.get("collected") != 1
                   or (run.get("kind") == "failed" and (events[1].get("outcome") != "failed" or run.get("exit_code") != 1))
                   or (run.get("kind") == "passed" and (events[1].get("outcome") != "passed" or run.get("exit_code") != 0))):
        return _diagnosis("inconsistent_evidence", "运行状态与记录的测试证据不一致。",
                          "测试数量、主体结果或退出码与原有运行状态不相符；不能进一步确认故障。",
                          "查看原报告与阶段记录，重新运行以获取一致的执行证据。")
    failure = run.get("failure")
    failure_detail = _exception_detail(failure) + (" 旧报告未记录完整阶段诊断证据。" if not modern else "")
    if run.get("kind") == "failed":
        if label == "before" and _matches(failure, expected):
            return _diagnosis("expected_failure_reproduced", "已复现声明的故障。", failure_detail,
                              "比较修复后同一测试的结果。")
        if missing:
            return _missing_module(missing)
        if label == "before" and expected:
            return _diagnosis("expected_failure_mismatch", "实际故障与预先声明不一致。",
                              failure_detail + "；声明：" + _exception_detail({"type": expected.get("type"), "message": expected.get("message_contains")}),
                              "核对声明的异常类型和消息，以及测试是否覆盖目标问题；审阅后再运行。")
        return _diagnosis("test_failed", "测试主体仍然失败。", failure_detail,
                          "查看实际异常和测试断言，继续检查修复是否覆盖该场景。")
    if run.get("kind") == "passed":
        if label == "before":
            return _diagnosis("not_reproduced", "旧代码测试也通过，未复现声明的故障。",
                              "记录的测试主体结果为通过。" + (" 旧报告未记录完整阶段诊断证据。" if not modern else ""),
                              "检查旧版本、测试输入和断言能否触发声明的问题。")
        return _diagnosis("passed", "测试通过。", "记录的测试主体结果为通过。" + (" 旧报告未记录完整阶段诊断证据。" if not modern else ""),
                          "结合旧代码故障与测试标识，查看本案例的验收结论。")
    if not modern:
        return _diagnosis("legacy_inconclusive", "证据不足，无法细分执行问题。",
                          "旧报告未记录结构化阶段诊断证据；仅沿用原有运行状态。",
                          "查看原报告和日志；在当前版本重跑可获得更完整的诊断。")
    return _diagnosis("inconclusive", "执行状态无法用于验收。", "完整阶段记录未产生可用的通过或故障结果。",
                      "查看原有运行说明与执行日志，核对测试状态后重跑。")


def diagnose_result(result):
    """Explain an existing result, preserving the authoritative stored status."""
    result = result if isinstance(result, dict) else {}
    expected = result.get("expected_failure")
    before_run = result.get("before") or {}
    after_run = result.get("after") or {}
    before = diagnose_run(before_run, label="before", expected=expected)
    after = diagnose_run(after_run, label="after", expected=expected)
    status = result.get("status", "inconclusive")
    summaries = {"verified": "旧代码已复现声明的故障，修复后同一测试通过。",
                 "still_failing": "旧代码已复现声明的故障，修复后测试仍失败。",
                 "not_reproduced": "旧代码测试通过，本次未复现声明的故障。",
                 "inconclusive": "存在测试或执行问题，证据不足，无法验收。",
                 "cancelled": "本次执行已取消，没有新的验收结论。"}
    summaries.update(regression_passed="这项回归检查在两版均通过。",
                     regression_detected="这项检查旧版通过，新版失败，需要处理新增回归。",
                     baseline_failure="这项检查两版均失败，请查看旧有失败及异常变化。",
                     improved="这项检查从旧版失败变为新版通过。")
    if result.get("check_mode") == "regression":
        if before_run.get("kind") == "passed":
            before.update(code="baseline_passed", summary="旧版基线通过。", next_steps=["结合新版结果检查是否引入回归。"])
        elif before_run.get("kind") == "failed":
            before.update(code="baseline_failed", summary="旧版基线已失败。", next_steps=["比较两版异常并查看是否改善。"])
    code = status
    summary = summaries.get(status, "原报告未提供可识别的验收结论。")
    before_id, after_id = before_run.get("test_id"), after_run.get("test_id")
    if before_id and after_id and before_id != after_id:
        code, summary = "test_id_mismatch", "两次运行的测试标识不一致，不能比较。"
        steps = ["核对两次运行使用的测试目标，确保使用同一测试后重新验收。"]
    else:
        if before_id and before_id == after_id and after["code"] == "passed":
            after["summary"] = "同一测试通过。"
        steps = list(dict.fromkeys(before["next_steps"] + after["next_steps"]))
    if status == "verified":
        steps.append("本结论只覆盖该测试场景；可继续检查其他相关回归场景。")
    return {"code": code, "summary": summary, "next_steps": steps, "before": before, "after": after}
