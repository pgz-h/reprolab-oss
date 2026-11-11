"""Offline HTML for project regression reports, using only supplied report data.

This renderer has no GUI dependency and never reads source files or logs. The
engine owns report.json and evidence publication; this module writes report.html.
"""

import base64
from collections import Counter
import hashlib
from html import escape
import json
import math
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .project_diagnostics import group_project_failures
from .project_progress import OBSERVATION_LABELS


_ENTRY_LABELS = {
    "regression_passed": "两版通过",
    "regression_detected": "新增失败",
    "baseline_failure": "原有失败",
    "improved": "已改善",
    "inconclusive": "受阻 / 未定",
}
_OUTCOME_LABELS = {"passed": "通过", "failed": "失败", "skipped": "跳过",
                   "inconclusive": "受阻", "not_run": "未执行"}
_GLOBAL = {
    "regression_passed": ("所选项目回归通过", "good", "本次所选测试均为两版通过或改善，且运行记录完整。"),
    "regression_detected": ("发现新增失败", "bad", "至少一个所选测试在旧版通过、在新版失败；请先查看对应异常。"),
    "attention": ("需要进一步核对", "warn", "存在原有失败、受阻或不完整记录，尚不能判定所选回归通过。"),
    "cancelled": ("本次运行已取消", "warn", "已完成的结果保留在下方；未执行或不完整项目不会计为通过。"),
}
_CHANGE_LABELS = {"added": "新增", "removed": "删除", "modified": "修改", "renamed": "重命名"}


def _e(value):
    return escape(str(value), quote=True)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, (list, tuple)) else []


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _value(value):
    return "未记录" if value is None or value == "" else str(value)


def _seconds(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{value:.3f} 秒"
    return "未记录"


def _facts(pairs):
    return '<dl class="facts">' + "".join(
        f"<dt>{_e(label)}</dt><dd>{_e(_value(value))}</dd>" for label, value in pairs
    ) + "</dl>"


def _relative_href(value):
    """Accept only report-relative paths, never URL schemes or traversal."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None
    normalized = path.as_posix() + ("/" if value.endswith("/") else "")
    return quote(normalized, safe="/")


def _link(path, label):
    href = _relative_href(path)
    return f'<a href="{_e(href)}">{_e(label)}</a>' if href else f"<span>{_e(label)}</span>"


def _exception(exception):
    if not exception:
        return ""
    if not isinstance(exception, dict):
        return f'<pre class="exception">{_e(exception)}</pre>'
    kind = exception.get("type") or "异常"
    message = exception.get("message", "")
    return f'<div class="exception"><strong>{_e(kind)}</strong><pre>{_e(message)}</pre></div>'


def _outcome(value):
    outcome = _mapping(value)
    status = outcome.get("status", "not_run")
    style = status if status in _OUTCOME_LABELS else "inconclusive"
    label = _OUTCOME_LABELS.get(status, "受阻 / 未知状态")
    phases = _items(outcome.get("phases"))
    content = _exception(outcome.get("exception"))
    if 'observed_status' in outcome:
        observed = OBSERVATION_LABELS.get(outcome['observed_status'], '阶段未完整结束')
        content += '<p class="muted">中断前观察：' + _e(observed) + '；整版执行未完整结束，不计为正式通过。</p>'
    if outcome.get("reason"):
        content += f'<p class="muted">{_e(outcome["reason"])}</p>'
    if phases:
        phase_html = []
        for raw in phases:
            phase = _mapping(raw)
            name = phase.get("phase", phase.get("when", "阶段"))
            phase_status = phase.get("outcome", phase.get("status", "未记录"))
            extra = " · xfail 标记" if phase.get("xfail") or phase.get("wasxfail") else ""
            phase_html.append(
                f'<li><div class="phase-head"><code>{_e(name)}</code>'
                f'<span>{_e(_OUTCOME_LABELS.get(phase_status, phase_status))}{extra}</span>'
                f'<span>{_e(_seconds(phase.get("duration_seconds", phase.get("duration"))))}</span></div>'
                f'{_exception(phase.get("exception"))}</li>'
            )
        content += '<ol class="phases">' + "".join(phase_html) + "</ol>"
    elif outcome:
        content += '<p class="muted">未记录可展开的测试阶段。</p>'
    else:
        content += '<p class="muted">本版本没有该测试的执行记录。</p>'
    content += f'<details class="raw"><summary>原始测试记录</summary><pre>{_e(_json(outcome))}</pre></details>'
    return (f'<span class="outcome {style}">{_e(label)}</span>'
            f'<span class="duration">{_e(_seconds(outcome.get("duration_seconds")))}</span>'
            f'<details class="outcome-details"><summary>阶段与异常</summary>{content}</details>')


def _entries(report):
    entries = []
    seen = set()
    for raw in _items(report.get("entries")):
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        test_id = str(entry.get("test_id", "未记录测试 ID"))
        entry["test_id"] = test_id
        entries.append(entry)
        seen.add(test_id)
    # A cancelled report may contain only completed entries. Show every selected
    # target explicitly without inventing execution or a passing classification.
    for selected in _items(report.get("selected_tests")):
        test_id = str(selected)
        if test_id not in seen:
            entries.append({"test_id": test_id, "status": "inconclusive",
                            "before": _mapping(_mapping(report.get("before")).get("tests")).get(test_id, {}),
                            "after": _mapping(_mapping(report.get("after")).get("tests")).get(test_id, {}),
                            "missing_entry": True})
            seen.add(test_id)
    return entries


def _case_comparison(entry):
    cases = entry.get('case_entries') or []
    if not cases:
        return ''
    rows = []
    for case in cases:
        sides = []
        for side in ('before', 'after'):
            observed = case[side]
            label = _OUTCOME_LABELS.get(observed['status'], observed['status'])
            if observed.get('retry_passed'):
                label = '重试后通过'
            if observed.get('expected_failure'):
                label += '（预期失败）'
            sides.append(label)
        rows.append(f'<li><code>{_e(case["test_id"])}</code> · {_e(_ENTRY_LABELS[case["status"]])}'
                    f' · {_e(" → ".join(sides))}</li>')
    counts = Counter(case['status'] for case in cases)
    summary = ' · '.join(f'{_ENTRY_LABELS[key]} {count}' for key, count in counts.items() if key != 'regression_passed')
    title = f'查看 {len(rows)} 个用例' + (' · ' + summary if summary else '')
    return f'<details><summary>{_e(title)}</summary><ul>{"".join(rows)}</ul></details>'


def _entry_statuses(entry):
    status = entry.get('status', 'inconclusive')
    if status not in _ENTRY_LABELS:
        status = 'inconclusive'
    return {status, *(case['status'] for case in entry.get('case_entries') or ())}


def _matrix(entries, memberships=None, *, target_label="测试 node ID"):
    rows = []
    for entry in entries:
        status = entry.get("status", "inconclusive")
        status = status if status in _ENTRY_LABELS else "inconclusive"
        statuses = _entry_statuses(entry)
        search = " ".join((entry["test_id"], *(_ENTRY_LABELS.get(key, key) for key in sorted(statuses)),
                           _json(entry.get("before")), _json(entry.get("after"))))
        missing = '<span class="missing">逐项结论未记录，暂列受阻</span>' if entry.get("missing_entry") else ""
        rows.append(
            f'<tr class="test-row" data-status="{status}" data-statuses="{_e(" ".join(sorted(statuses)))}" '
            f'data-groups="{_e(" ".join((memberships or {}).get(entry["test_id"], [])))}" data-search="{_e(search)}">'
            f'<th scope="row"><code class="node-id">{_e(entry["test_id"])}</code>{missing}</th>'
            f'<td><span class="classification {status}">{_e(_ENTRY_LABELS[status])}</span>'
            + (f'<p>{_e(entry["reason"])}</p>' if entry.get("reason") else '') + _case_comparison(entry) + '</td>'
            f'<td>{_outcome(entry.get("before"))}</td><td>{_outcome(entry.get("after"))}</td></tr>'
        )
    empty = '<p class="empty">没有可展示的所选测试记录。</p>' if not entries else ""
    return (empty + '<div class="table-scroll"><table class="matrix"><caption class="sr-only">'
            f'所选范围的前后版本执行结果</caption><thead><tr><th scope="col">{_e(target_label)}</th>'
            '<th scope="col">回归判断</th><th scope="col">BEFORE · 基线</th><th scope="col">AFTER · 对比版本</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
            '<p id="no-matches" class="empty" hidden>没有匹配的测试。请调整关键词或状态筛选。</p>')


def _diagnostic_overview(diagnostics):
    if not diagnostics['groups']:
        return ''
    cards = []
    for group in diagnostics['groups'][:12]:
        cards.append('<details><summary>' + _e(group['label']) + '</summary>'
            + '<p>' + _e(group['advice']) + '</p><p class="muted">' + _e(group['basis']) + '</p>'
            + ('<pre>' + _e(group['message']) + '</pre>' if group['message'] else '')
            + ('<p class="muted">整版问题尚未关联到某个已选测试，请查看下方版本日志。</p>' if not group['count'] else '')
            + '<button type="button" data-diagnostic="' + group['id'] + '">查看本组 ' + str(group['count']) + ' 项</button></details>')
    return ('<section class="card section"><p class="eyebrow">DIAGNOSTICS</p><h2>按失败现象处理</h2>'
            + f"<p>{diagnostics['attention_test_count']} 项测试需处理 · {diagnostics['group_count']} 组现象。</p>"
            + '<p class="muted">' + _e(diagnostics['notice']) + '</p>' + ''.join(cards)
            + ('<p class="muted">此处展示前 12 组，全部组可在下方现象筛选中查看。</p>' if diagnostics['group_count'] > 12 else '')
            + '</section>')


def _runtime_card(report, label, heading):
    run = _mapping(report.get(label))
    runtime = _mapping(report.get("runtime"))
    errors = _items(run.get("collection_errors"))
    record = run.get("worker_record_complete")
    complete = "完整" if record is True else "不完整" if record is False else "未记录"
    duration = run.get("duration_seconds")
    facts = _facts([
        ("执行状态", run.get("status", "未执行 / 无记录")),
        ("运行耗时", _seconds(duration)), ("退出码", run.get("exit_code")),
        ("超时", "是" if run.get("timed_out") else "否" if "timed_out" in run else "未记录"),
        ("worker 记录", complete), ("测试记录数", len(_mapping(run.get("tests")))),
        ("Python", run.get("python_version") or runtime.get("python_version")),
        ("pytest", run.get("pytest_version") or runtime.get("pytest_version")),
        ("Vitest", run.get("vitest_version")), ("Go", runtime.get("go_version")),
        ("最后执行阶段", run.get("phase")),
    ])
    if run.get('status') == 'dependencies_failed':
        errors = ['npm ci 未完成，请查看日志中的依赖、缓存或 registry 错误。', *errors]
    error_html = ('<div class="collection-errors"><h4>收集 / 执行错误</h4><pre>'
                  + _e(_json(errors)) + "</pre></div>") if errors else ""
    if run.get("reason"):
        error_html += f'<p class="muted">{_e(run["reason"])}</p>'
    progress = _mapping(run.get('progress'))
    progress_link = ''
    if progress:
        error_html += '<p>已保存逐项进度：' + _e(progress.get('completed', 0)) + '/' + _e(progress.get('total', 0)) + ' 项完成阶段记录。</p>'
        if progress.get('current_test'):
            error_html += '<p class="muted">最后记录：' + _e(progress['current_test']) + ' · ' + _e(progress.get('current_phase')) + '</p>'
        if progress.get('notice'):
            error_html += '<p class="muted">' + _e(progress['notice']) + '</p>'
        progress_link = _link(label + '.progress.jsonl', '逐项进度记录')
    log = str(run.get("log") or f"{label}.log")
    events = str(run.get("events") or f"{label}.events.json")
    return (f'<article class="card runtime-card"><p class="eyebrow">{label.upper()}</p><h3>{heading}</h3>'
            f'{facts}{error_html}<details><summary>查看日志摘要</summary><pre>{_e(run.get("log_excerpt") or "未记录日志摘要。")}</pre></details>'
            f'<div class="links">{_link(log, "原始日志")}{_link(events, "阶段事件 JSON")}{progress_link}</div></article>')


def _source(report):
    revision = _mapping(report.get("git_revision"))
    workspace = _mapping(report.get("workspace_origin"))
    before = report.get("before_commit") or revision.get("before_commit")
    after = report.get("after_commit") or revision.get("after_commit")
    after_ref = report.get("after_ref") or revision.get("after_ref")
    working_tree = bool(workspace) or after == "@working-tree" or after_ref == "@working-tree"
    mode = "冻结的当前工作区" if working_tree else "冻结的 Git 提交"
    facts = _facts([
        ("ReproLab 版本", report.get("reprolab_version") or "未记录"),
        ("本地仓库", report.get("repository") or revision.get("repository")),
        ("输入方式", mode), ("基线提交", before), ("对比提交 / 来源", after),
        ("基线引用", report.get("before_ref") or revision.get("before_ref")),
        ("对比引用", after_ref),
    ])
    note = ("对比版本使用准备会话时冻结的工作区内容；之后编辑原仓库不会更新本报告。"
            if working_tree else "对比使用保存的固定源码输入；原仓库之后的改动不会更新本报告。")
    hashes = []
    for key, title in (("source_sha256", "源码内容 SHA-256"),
                       ("source_mode_sha256", "普通文件权限 SHA-256")):
        value = _mapping(report.get(key))
        hashes.append(f'<h4>{title}</h4>' + _facts([
            ("BEFORE", value.get("before")), ("AFTER", value.get("after")),
        ]))
    for key in ("shared_tests", "shared_tests_sha256", "selected_tests_sha256", "selection_sha256"):
        if report.get(key):
            hashes.append(f'<h4>{_e(key)}</h4><pre>{_e(_json(report[key]))}</pre>')
    if workspace:
        hashes.append(f'<h4>工作区快照元数据</h4><pre>{_e(_json(workspace))}</pre>')
    origin = _mapping(report.get('refresh_origin'))
    refresh = ''
    if origin:
        refresh = ('<h3>刷新来源</h3>' + _facts([
            ('来源会话', origin.get('project_id')), ('来源会话目录', origin.get('directory')),
            ('沿用的基线提交', origin.get('before_commit')),
        ]) + '<p class="muted">本次另存当前工作区，沿用来源会话的固定基线。'
               '来源会话和旧报告保留；本报告的结果只覆盖本次实际选择。</p>')
    review_source = _mapping(report.get('review_source'))
    if review_source:
        refresh += '<h3>复查所绑定的来源报告</h3>' + _facts([
            ('来源会话 ID', review_source.get('project_id')), ('来源运行 ID', review_source.get('run_id')),
            ('来源报告目录', review_source.get('report_dir')), ('来源报告指纹', review_source.get('report_sha256')),
            ('带入范围', {'all': '来源报告全部选择', 'attention': '来源报告失败／受阻项',
                        'selected': '来源报告明确子集', 'workspace': '当前工作区的勾选／继承选择'}.get(review_source.get('selection_mode'), '范围方式未记录')),
        ]) + '<p class="muted">在桌面项目结果页使用“与来源报告比较”。报告移动后可重新定位，须通过身份与证据核对。</p>'
    return (f'<section class="card section"><div class="section-head"><div><p class="eyebrow">FROZEN INPUTS</p>'
            f'<h2>来源与固定输入</h2></div><span class="tag">{mode}</span></div>{facts}'
            f'<p class="muted">{note}</p>{refresh}<details><summary>内容、权限与所选测试指纹</summary>{"".join(hashes)}</details>'
            f'<div class="links">{_link("evidence/project/project.json", "项目会话记录")}'
            f'{_link("evidence/project/inputs/before/", "基线源码快照")}'
            f'{_link("evidence/project/inputs/after/", "对比源码快照")}</div></section>')


def _changes(report):
    changes = _mapping(report.get("changes"))
    previews = [item for item in _items(changes.get("files")) if isinstance(item, dict)]
    overview = [item for item in _items(report.get("changed_files")) if isinstance(item, dict)] or previews
    total = changes.get("total", len(overview))
    counts = Counter(item.get("kind", "modified") for item in overview)
    chips = "".join(f'<span class="tag">{_e(_CHANGE_LABELS.get(kind, kind))} {count}</span>'
                    for kind, count in counts.items())
    overview_html = []
    for item in overview:
        path = str(item.get("path", "未记录路径"))
        kind = item.get("kind", "modified")
        links = []
        if _relative_href(path):
            if kind != "added":
                links.append(_link("evidence/project/inputs/before/" + path, "before"))
            if kind != "removed":
                links.append(_link("evidence/project/inputs/after/" + path, "after"))
        overview_html.append(f'<li><code>{_e(path)}</code><span>{_e(_CHANGE_LABELS.get(kind, kind))}</span>'
                             f'<span class="file-links">{" ".join(links)}</span></li>')
    diffs = []
    for item in previews:
        lines = []
        for line in str(item.get("diff", "")).splitlines():
            style = ("diff-add" if line.startswith("+") and not line.startswith("+++") else
                     "diff-remove" if line.startswith("-") and not line.startswith("---") else "diff-line")
            lines.append(f'<span class="{style}">{_e(line) or " "}</span>')
        preview = '<pre class="diff">' + "".join(lines) + "</pre>" if lines else '<p class="muted">无文本差异预览；可打开源码快照核对。</p>'
        note = f'<p class="muted">{_e(item["note"])}</p>' if item.get("note") else ""
        diffs.append(f'<details class="diff-block"><summary><code>{_e(item.get("path", "未记录路径"))}</code>'
                     f'<span>{_e(_CHANGE_LABELS.get(item.get("kind"), item.get("kind", "修改")))}</span></summary>'
                     f'{preview}{note}</details>')
    truncated = ('<p class="muted">差异预览已截断；完整文件请查看源码快照。</p>'
                 if changes.get("truncated") else "")
    overview_note = f"总计 {_value(total)} 个变更文件，本报告记录 {len(overview)} 个文件的概览。"
    return ('<section class="card section"><p class="eyebrow">CHANGE REVIEW</p><h2>改动文件与差异</h2>'
            f'<p class="muted">{_e(overview_note)} 文本预览由固定源码生成，绿色表示新增，红色表示删除。</p>'
            f'<div class="tags">{chips}</div><details><summary>展开文件概览</summary>'
            f'<ul class="file-list">{"".join(overview_html)}</ul></details>{"".join(diffs)}{truncated}</section>')


def _environment(report):
    from .runtime_environment import describe_variables
    from .pytest_profile import describe_pytest_profile
    runtime = _mapping(report.get("runtime"))
    environment = _mapping(report.get("environment"))
    if report.get("runner") == "go":
        facts = _facts([
            ("Go", runtime.get("go_version")), ("包目录", report.get("go_package")),
            ("系统", environment.get("platform")), ("架构", environment.get("machine")),
            ("每版总超时", _seconds(report.get("timeout"))),
            ("依赖", "使用本机模块缓存"),
        ])
        return ('<section class="card section"><h2>Go 环境与执行范围</h2>'
                + facts + f'<p>{_e(report.get("notice"))}</p></section>')
    if report.get("runner") == "vitest":
        facts = _facts([
            ("Node.js", runtime.get("node_version")), ("npm", runtime.get("npm_version")),
            ("系统", environment.get("platform")), ("架构", environment.get("machine")),
            ("每版总超时", _seconds(report.get("timeout"))),
            ("依赖安装", "仅使用 npm 缓存" if report.get("offline") else "npm ci"),
        ])
        return ('<section class="card section"><h2>Vitest 环境与执行范围</h2>'
                + facts + f'<p>{_e(report.get("notice"))}</p></section>')
    facts = _facts([
        ("解释器", runtime.get("executable") or environment.get("executable")),
        ("Python", runtime.get("python_version") or environment.get("python_version")),
        ("pytest", runtime.get("pytest_version")), ("系统", environment.get("platform")),
        ("架构", environment.get("machine")), ("环境指纹", environment.get("fingerprint")),
        ("每版总超时", _seconds(report.get("timeout", report.get("timeout_seconds")))),
    ])
    record = environment or {"notice": "没有可用的环境版本清单。"}
    return ('<section class="card section"><p class="eyebrow">RUNTIME RECORD</p><h2>环境与执行范围</h2>'
            f'{facts}<p class="muted">两版分别运行一个 pytest 进程，使用对比版本的同一份目标测试文件；'
            '每版保留各自的项目配置与 conftest。超时覆盖每版全部所选测试。</p>'
            f'<p class="muted">{_e(describe_pytest_profile(report.get("pytest_profile")))}</p>'
            f'<p class="muted">{_e(describe_variables(report.get("runtime_environment")))}</p>'
            f'<details><summary>运行变量声明</summary><pre>{_e(_json(report.get("runtime_environment") or {}))}</pre></details>'
            f'<details><summary>展开环境版本清单</summary><pre>{_e(_json(record))}</pre></details>'
            '<p class="muted">环境版本清单用于核对，不是依赖锁，也不能证明环境中的所有文件相同。</p></section>')


_SCRIPT = """(() => {
  const search = document.getElementById('test-search');
  const filter = document.getElementById('status-filter');
  const groupFilter = document.getElementById('diagnostic-filter');
  const rows = Array.from(document.querySelectorAll('.test-row'));
  const count = document.getElementById('visible-count');
  const empty = document.getElementById('no-matches');
  function apply() {
    const words = search.value.trim().toLocaleLowerCase().split(/\\s+/).filter(Boolean);
    let visible = 0;
    for (const row of rows) {
      const matchesStatus = !filter.value || row.dataset.statuses.split(' ').includes(filter.value);
      const text = row.dataset.search.toLocaleLowerCase();
      const matchesGroup = !groupFilter.value || row.dataset.groups.split(' ').includes(groupFilter.value);
      row.hidden = !(matchesStatus && matchesGroup && words.every(word => text.includes(word)));
      if (!row.hidden) visible += 1;
    }
    count.textContent = `显示 ${visible} / ${rows.length} 项`;
    empty.hidden = visible !== 0 || rows.length === 0;
  }
  search.addEventListener('input', apply);
  filter.addEventListener('change', apply);
  groupFilter.addEventListener('change', () => { search.value = ''; filter.value = ''; apply(); });
  document.querySelectorAll('[data-diagnostic]').forEach(button => {
    button.addEventListener('click', () => {
      groupFilter.value = button.dataset.diagnostic;
      search.value = ''; filter.value = ''; apply();
      document.getElementById('tests').scrollIntoView();
    });
  });
  document.getElementById('clear-filters').addEventListener('click', () => {
    search.value = '';
    filter.value = '';
    groupFilter.value = '';
    apply();
    search.focus();
  });
  apply();
})();"""


_CSS = """
:root{color-scheme:light;--ink:#162a3e;--muted:#607287;--line:#dbe3ea;--paper:#fff;--bg:#f3f5f6;--good:#116b54;--bad:#a53839;--warn:#936217}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}a{color:#1b5b88;text-underline-offset:3px}a:hover{color:#103c60}button,input,select{font:inherit}button,select,summary{cursor:pointer}code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}code{font-size:.87em;overflow-wrap:anywhere}pre{margin:12px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.7;tab-size:4;max-height:380px;overflow:auto;background:#f2f5f8;border-radius:8px;padding:14px}p{margin:10px 0}h1,h2,h3,h4{line-height:1.35;margin:0 0 12px}h1{font-size:clamp(28px,3.5vw,42px);letter-spacing:-.035em;max-width:1000px;overflow-wrap:anywhere}h2{font-size:22px;letter-spacing:-.025em}h3{font-size:19px}h4{font-size:13px;margin-top:18px}header{padding:21px max(24px,calc((100vw - 1280px)/2));background:#112c42;color:#e9f3f7;display:flex;align-items:center;justify-content:space-between;gap:24px}.brand{font-weight:750;font-size:23px;letter-spacing:-.04em}.brand span{color:#83d5c0}.header-label{font:11px/1.5 ui-monospace,monospace;letter-spacing:.12em;color:#aec6d4}main{max-width:1328px;margin:auto;padding:38px 24px 30px}.eyebrow{font:11px/1.5 ui-monospace,monospace;font-weight:650;letter-spacing:.12em;color:var(--muted);margin:0 0 9px}.subtitle{color:var(--muted);margin:12px 0 24px}.hero{padding:24px 27px;border:1px solid var(--line);border-left:5px solid var(--warn);background:#fffaf0;border-radius:12px}.hero.good{border-left-color:var(--good);background:#edf8f3}.hero.bad{border-left-color:var(--bad);background:#fff1ef}.hero h2{font-size:26px;margin:0 0 8px}.hero p:last-child{margin-bottom:0}.scope{font-size:13px;color:#41566c}.notice{border-top:1px solid #d5dfdf;padding-top:12px;margin-top:15px;white-space:pre-wrap;overflow-wrap:anywhere}.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:18px 0 28px}.stat{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:16px 18px}.stat b{display:block;font-size:30px;line-height:1.2;font-weight:700;letter-spacing:-.04em;margin-bottom:6px}.stat span{font-size:12px;color:var(--muted)}.stat.regression_passed b,.stat.improved b{color:var(--good)}.stat.regression_detected b{color:var(--bad)}.stat.baseline_failure b,.stat.inconclusive b{color:var(--warn)}.card{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:25px}.section{margin-top:24px}.section-head{display:flex;justify-content:space-between;align-items:center;gap:20px}.muted{font-size:13px;color:var(--muted)}.tag{display:inline-block;background:#eef3f7;border:1px solid #e0e7ee;border-radius:5px;padding:3px 8px;font-size:11px;color:#455d73}.tags{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0}.runtime-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:24px}.facts{display:grid;grid-template-columns:116px minmax(0,1fr);gap:9px 14px;margin:18px 0;font-size:13px}.facts dt{color:var(--muted)}.facts dd{margin:0;overflow-wrap:anywhere}.runtime-card .facts{grid-template-columns:100px minmax(0,1fr)}details{border-top:1px solid var(--line);margin-top:15px;padding-top:12px}summary{font-size:13px;font-weight:600;overflow-wrap:anywhere}summary:hover{color:#1b5b88}details[open]>summary{margin-bottom:12px}.links{display:flex;flex-wrap:wrap;gap:9px;margin-top:20px}.links a{background:#eff4f8;border:1px solid #e0e8ef;border-radius:6px;padding:6px 10px;font-size:12px;text-decoration:none}.collection-errors{margin-top:15px;color:var(--bad)}.collection-errors pre{background:#fff2ef}.toolbar{display:flex;align-items:end;flex-wrap:wrap;gap:12px;padding:15px 0 20px}.toolbar label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}.toolbar .search{flex:1;min-width:240px}.toolbar input,.toolbar select{min-height:40px;border:1px solid #cbd7e1;border-radius:7px;padding:8px 11px;background:#fff;color:var(--ink);font-size:13px}.toolbar button{min-height:40px;padding:7px 13px;border:1px solid #cbd7e1;border-radius:7px;background:#f5f7fa;color:#38536c;font-size:12px}.visible-count{font-size:12px;color:var(--muted);padding-bottom:9px;white-space:nowrap}.table-scroll{overflow:auto;border:1px solid var(--line);border-radius:9px}.matrix{border-collapse:collapse;width:100%;table-layout:fixed;font-size:13px;min-width:780px}.matrix th,.matrix td{padding:16px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}.matrix thead th{font-size:11px;letter-spacing:.035em;background:#edf2f6;color:#53697e;padding:13px 16px}.matrix thead th:first-child{width:35%}.matrix thead th:nth-child(2){width:15%}.matrix thead th:nth-child(3),.matrix thead th:nth-child(4){width:25%}.matrix tbody th{font-weight:500}.matrix tbody tr:last-child>*{border-bottom:0}.matrix tbody tr:hover{background:#fbfcfd}.node-id{display:block;white-space:pre-wrap;font-size:12px;line-height:1.7}.classification,.outcome{display:inline-block;border-radius:5px;padding:3px 7px;font-size:11px;font-weight:650;background:#edf1f5;color:#596c80}.classification.regression_passed,.classification.improved,.outcome.passed{background:#e7f5ee;color:var(--good)}.classification.regression_detected,.outcome.failed{background:#fceae7;color:var(--bad)}.classification.baseline_failure,.classification.inconclusive,.outcome.skipped,.outcome.inconclusive{background:#fff2da;color:var(--warn)}.duration{display:block;font:11px/1.5 ui-monospace,monospace;color:var(--muted);margin:7px 0}.outcome-details{margin-top:10px;padding-top:8px}.outcome-details summary{font-size:11px;font-weight:500}.exception{margin-top:12px;font-size:12px;color:var(--bad);overflow-wrap:anywhere}.exception pre{background:#fff1ee;margin-top:6px;color:#6b3031;padding:10px;max-height:260px}.phases{list-style:none;padding:0;margin:14px 0}.phases li{padding:10px 0;border-top:1px solid var(--line)}.phase-head{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;color:var(--muted);font-size:10px}.phase-head code{color:var(--ink);font-size:11px}.raw summary{font-size:10px;color:var(--muted)}.missing{display:block;font-size:11px;color:var(--warn);margin-top:9px}.file-list{list-style:none;padding:0;margin:12px 0;max-height:350px;overflow:auto}.file-list li{display:grid;grid-template-columns:minmax(0,1fr) 60px 110px;align-items:baseline;gap:12px;padding:8px 0;border-bottom:1px solid #edf1f5;font-size:12px}.file-list li>span{color:var(--muted)}.file-links{text-align:right}.file-links a{font:11px/1.5 ui-monospace,monospace;margin-left:6px}.diff-block summary{display:flex;align-items:baseline;justify-content:space-between;gap:15px}.diff-block summary>span{font-size:11px;color:var(--muted)}.diff{background:#162b3f;color:#dce7ef;padding:14px 0;max-height:420px;white-space:pre;overflow:auto}.diff>span{display:block;padding:0 14px;min-height:1.7em}.diff-add{background:#1a453f;color:#bcf3d4}.diff-remove{background:#4a303b;color:#ffd0cf}.empty{text-align:center;padding:26px;color:var(--muted);font-size:13px}.scope-list{margin:14px 0 0;padding-left:20px;color:#4c6379;font-size:13px}.scope-list li{margin:7px 0}footer{display:flex;justify-content:space-between;gap:20px;border-top:1px solid var(--line);margin-top:30px;padding-top:20px;font-size:11px;color:var(--muted)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}[hidden]{display:none!important}input:focus-visible,select:focus-visible,button:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid #70afcc;outline-offset:3px}
@media(max-width:820px){main{padding:26px 16px}header{padding:18px 20px}.header-label{max-width:150px;text-align:right;font-size:9px}.stats{grid-template-columns:repeat(3,minmax(0,1fr))}.runtime-grid{grid-template-columns:1fr}.card{padding:20px}.facts{grid-template-columns:96px minmax(0,1fr)}.section-head{align-items:start}.hero{padding:22px}.hero h2{font-size:23px}}
@media(max-width:480px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat{padding:13px}.stat b{font-size:26px}.file-list li{grid-template-columns:minmax(0,1fr) 44px}.file-links{grid-column:1/-1;text-align:left}.toolbar .search{min-width:100%}footer{display:block}.facts{font-size:12px;gap:9px}.card{padding:18px}}
@media print{body{background:#fff;color:#111}header{background:#fff;color:#111;border-bottom:1px solid #bbb}.header-label{color:#555}main{max-width:none;padding:20px 0}.toolbar,.links{display:none}.card,.hero,.stat{border-color:#ccc;box-shadow:none}.runtime-grid{grid-template-columns:1fr 1fr}.table-scroll{overflow:visible}.matrix{min-width:0}.matrix th,.matrix td{padding:9px}.matrix{font-size:11px}.matrix .node-id{font-size:9px}.test-row[hidden]{display:table-row!important}pre{max-height:none}.hero,.runtime-card{break-inside:avoid}.stats{grid-template-columns:repeat(5,1fr)}}
"""


def write_project_report(report: dict) -> str:
    """Write only ``report_dir/report.html`` and return its absolute path."""
    if not isinstance(report, dict) or not report.get("report_dir"):
        raise ValueError("项目报告必须包含 report_dir。")
    entries = _entries(report)
    runner = report.get("runner")
    is_vitest = runner == "vitest"
    diagnostics = {"groups": []} if runner in ("vitest", "go") else group_project_failures(report)
    memberships = {}
    for group in diagnostics['groups']:
        for identity in group['test_ids']:
            memberships.setdefault(identity, []).append(group['id'])
    group_options = ''.join('<option value="' + group['id'] + '">' + _e(group['label'] + ' · ' + ' '.join(group['message'].split())[:100])
                            + '</option>' for group in diagnostics['groups'])
    counts = Counter(entry.get("status") if entry.get("status") in _ENTRY_LABELS else "inconclusive"
                     for entry in entries)
    status = report.get("status", "attention")
    heading, style, explanation = _GLOBAL.get(status, _GLOBAL["attention"])
    title = report.get("title") or "项目回归报告"
    selected = _items(report.get("selected_tests"))
    selected_count = len(selected) if "selected_tests" in report else len(entries)
    collection = _mapping(report.get("collection"))
    collected = collection.get("count")
    if collected is None and isinstance(collection.get("tests"), list):
        collected = len(collection["tests"])
    discovery = f"实际收集 {_value(collected)} 项" if collected is not None else "实际收集总数未记录"
    if collection.get("collection_path"):
        discovery += " · 发现范围 " + collection["collection_path"]
    if collection.get("truncated"):
        discovery += "（发现列表已截断）"
    case_summary = ''
    if is_vitest:
        scope = f'本次选择 <strong>{selected_count}</strong> 个测试文件。'
        if 'case_counts' in report:
            case_counts = _mapping(report['case_counts'])
            case_stats = ' · '.join(f'{label} {case_counts.get(key, 0)}' for key, label in _ENTRY_LABELS.items())
            retries = sum(any(case[side].get('retry_passed') for side in ('before', 'after'))
                          for entry in entries for case in entry.get('case_entries') or ())
            case_summary = (f'<p>匹配用例 · {_e(case_stats)}</p>'
                            + (f'<p>{retries} 个用例出现重试后通过，计入受阻 / 未定。</p>' if retries else ''))
            scope += ' 上方结论考虑已匹配的用例，下方卡片按文件计数。'
        else:
            scope += ' 结论以文件为单位。'
    elif runner == 'go':
        scope = f'本次选择 <strong>{selected_count}</strong> 个顶层 Go 测试 · 包目录 {_e(report["go_package"])}。'
    else:
        scope = f'本次选择 <strong>{selected_count}</strong> 个测试 · {_e(discovery)} · 表中展示 <strong>{len(entries)}</strong> 项。结论仅覆盖这些所选测试。'
    stats = "".join(f'<div class="stat {key}"><b>{counts[key]}</b><span>{label}</span></div>'
                    for key, label in _ENTRY_LABELS.items())
    filter_counts = Counter(status for entry in entries for status in _entry_statuses(entry))
    options = "".join(f'<option value="{key}">{label} · {filter_counts[key]}</option>'
                      for key, label in _ENTRY_LABELS.items())
    notice = f'<p class="notice">{_e(report["notice"])}</p>' if report.get("notice") else ""
    issue = (f'<details><summary>本次问题与检查说明</summary><pre>{_e(report["issue"])}</pre></details>'
             if report.get("issue") else "")
    collection_error = ""
    if collection.get("status") == "collection_failed" or collection.get("collection_errors"):
        collection_error = ('<details open class="collection-errors"><summary>会话收集问题</summary><pre>'
                            + _e(_json(collection.get("collection_errors") or collection.get("log_excerpt") or collection))
                            + "</pre></details>")
    selection_unit = {'vitest': '测试文件', 'go': 'Go 测试函数'}.get(runner, '测试 node ID')
    blocked_note = ('依赖准备失败、收集异常、跳过和执行不完整保留为受阻。' if runner in ('vitest', 'go')
                    else '跳过、xfail、fixture 问题、缺失记录和超时保留为受阻。')
    code_notice = {'vitest': 'Vitest 配置和测试会执行本机代码。',
                   'go': 'Go 测试和包初始化会执行本机代码。'}.get(runner, '测试和 conftest 可以运行任意本机代码。')
    acceptance_note = ('' if runner in ('vitest', 'go') else '<p class="scope">项目回归通过不等于目标问题已完整修复；'
                       '目标修复的预声明验收继续由单项验收与 AI 验收计划承担。</p>')
    script_hash = base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")
    csp = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-" + script_hash
           + "'; base-uri 'none'; form-action 'none'; connect-src 'none'")
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_e(csp)}"><title>ReproLab · {_e(title)}</title><style>{_CSS}</style></head>
<body><header><div class="brand">Repro<span>Lab</span></div><div class="header-label">PROJECT REGRESSION<br>LOCAL EVIDENCE / 项目回归</div></header>
<main><p class="eyebrow">ONE PROJECT · TWO SNAPSHOTS · SELECTED TESTS</p><h1>{_e(title)}</h1>
<p class="subtitle">项目 {_e(_value(report.get("project_id")))} · 运行 {_e(_value(report.get("run_id")))} · {_e(_value(report.get("created_at")))}</p>
<section class="hero {style}" aria-label="本次项目回归结论"><h2>{heading}</h2><p>{explanation}</p>
<p class="scope">{scope}</p>{acceptance_note}{notice}{issue}{collection_error}</section>
<div class="stats" aria-label="逐项结果数量">{stats}</div>
{case_summary}{_diagnostic_overview(diagnostics)}
<section class="card section" id="tests"><div class="section-head"><div><p class="eyebrow">TEST MATRIX</p><h2>逐项比较前后结果</h2></div><span class="tag">同一份目标测试</span></div>
<p class="muted">新增失败：旧版通过、新版失败。原有失败：两版均失败。改善：旧版失败、新版通过。{blocked_note}</p>
<div class="toolbar"><label class="search" for="test-search">搜索 node ID、异常或阶段记录<input id="test-search" type="search" placeholder="例如 test_export[utf8] 或 AssertionError" autocomplete="off"></label>
<label for="status-filter">结果筛选<select id="status-filter"><option value="">全部结果 · {len(entries)}</option>{options}</select></label><label for="diagnostic-filter">现象筛选<select id="diagnostic-filter"><option value="">全部现象</option>{group_options}</select></label><button type="button" id="clear-filters">清除筛选</button><span class="visible-count" id="visible-count" aria-live="polite">显示 {len(entries)} / {len(entries)} 项</span></div>
<noscript><p class="muted">当前浏览器未启用脚本，全部记录仍可阅读；搜索与筛选需要启用本地脚本。</p></noscript>{_matrix(entries, memberships, target_label=selection_unit)}</section>
<div class="runtime-grid">{_runtime_card(report, "before", "基线版本的实际运行")}{_runtime_card(report, "after", "对比版本的实际运行")}</div>
{_source(report)}{_changes(report)}{_environment(report)}
<section class="card section"><p class="eyebrow">EVIDENCE & SCOPE</p><h2>证据与能力范围</h2>
{_facts([("运行编号", report.get("run_id")), ("会话编号", report.get("project_id")), ("记录时间", report.get("created_at")), ("原始状态", status)])}
<div class="links">{_link("report.json", "结构化报告 JSON")}{_link("before.log", "BEFORE 完整日志")}{_link("after.log", "AFTER 完整日志")}{_link("before.events.json", "BEFORE 事件")}{_link("after.events.json", "AFTER 事件")}{_link("evidence/", "全部固定输入证据")}</div>
<ul class="scope-list"><li>仅运行明确选中的{selection_unit}；未选中的项目功能不在本报告结论中。</li><li>只有两版实际通过，或旧版失败而新版通过，且记录完整、没有收集或执行错误，才构成全局回归通过。</li><li>源码快照、普通文件权限、目标测试与日志事件可核对；一致性检查不代表作者身份认证。</li><li>仅用于可信项目。独立运行进程与固定副本不是安全沙箱。{code_notice}</li><li>HTML 在本地使用，无外部字体、脚本、网络请求或数据上传。证据链接均相对于报告目录，导出时请保留目录结构。</li></ul>
<details><summary>引擎记录的统计</summary><pre>{_e(_json(report.get("counts", {})))}</pre></details></section>
<footer><span>ReproLab · Evidence before conclusions.</span><span>离线项目回归报告 · 固定输入，可核对，可明确重放</span></footer></main><script>{_SCRIPT}</script></body></html>'''
    directory = Path(report["report_dir"]).expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "report.html"
    from .persistence import atomic_write
    atomic_write(path, html)
    return str(path)
