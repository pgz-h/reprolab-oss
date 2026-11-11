"""An offline, escaped evidence report. No scripts or external assets."""

from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
import tokenize

from . import __version__
from .diagnostics import diagnose_result


STATUS = {
    "regression_passed": ("回归检查通过", "verified", "✓"),
    "regression_detected": ("发现新增失败", "failed", "!"),
    "baseline_failure": ("两版均有失败", "warning", "!"),
    "improved": ("此项表现已改善", "verified", "✓"),
    "verified": ("本案例验证通过", "verified", "✓"),
    "not_reproduced": ("本次未复现问题", "warning", "—"),
    "still_failing": ("修复后仍未通过", "failed", "!"),
    "inconclusive": ("证据不足，无法验收", "warning", "?"),
}
KINDS = {"passed": "测试通过", "failed": "测试失败", "inconclusive": "无法判定"}

CSS = """
:root{color-scheme:light;--ink:#182c43;--muted:#617080;--line:#dce4e9;--paper:#f4f7f9;--teal:#117b66;--blue:#285ddb}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.75 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}
a{color:var(--blue);text-underline-offset:4px}header{background:#12273c;color:white;padding:22px max(24px,calc((100vw - 1120px)/2));display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{font-size:23px;font-weight:750;letter-spacing:-.8px}.brand span{color:#7cdac3}.version{font-size:12px;color:#bbcad7;letter-spacing:1px}main{max-width:1176px;margin:auto;padding:38px 28px 56px}
.eyebrow{font-size:12px;letter-spacing:1.5px;font-weight:700;color:var(--teal);margin:0 0 10px}h1{font-size:clamp(26px,3.5vw,38px);line-height:1.4;margin:0 0 12px;letter-spacing:-1px}h2{font-size:19px;margin:0 0 16px}h3{font-size:17px;margin:0 0 10px}p{margin:0 0 12px}.lead{max-width:850px;color:var(--muted);font-size:16px}
.intro{background:#eaf0ff;border-left:4px solid #648aef;border-radius:0 10px 10px 0;padding:17px 21px;margin:24px 0}.intro p{margin:0}.demo{display:inline-block;border:1px solid #cbd7e2;border-radius:20px;padding:3px 11px;font-size:12px;margin-bottom:16px;color:#53677b;background:white}
.result{display:flex;gap:18px;align-items:flex-start;border:1px solid #b9dacf;background:#eaf7f1;border-radius:15px;padding:24px;margin:25px 0}.result.warning{background:#fff7e8;border-color:#ecd8aa}.result.failed{background:#fff0ee;border-color:#ecc8c3}.result h2{margin:0 0 7px}.result p{margin:0;color:#466359}.result.warning p,.result.failed p{color:#6c5c45}.icon{flex:0 0 36px;height:36px;border-radius:50%;background:var(--teal);color:white;text-align:center;font-size:22px;font-weight:700}.warning .icon{background:#a27522}.failed .icon{background:#ac4a43}
.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0}.step{background:white;border:1px solid var(--line);border-radius:12px;padding:17px}.step b{display:block;font-size:14px}.step span{font-size:12px;color:var(--muted)}.num{display:inline-block;font-size:11px;color:#51729a;margin-bottom:7px;letter-spacing:1px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.card{background:white;border:1px solid var(--line);border-radius:14px;padding:24px;min-width:0}.label{font-size:12px;letter-spacing:1px;color:var(--muted);margin-bottom:12px}.pill{display:inline-block;font-size:12px;padding:3px 11px;border-radius:30px;background:#fff0ed;color:#aa4038}.pill.passed{background:#e7f6ed;color:#127753}.pill.inconclusive{background:#fff5df;color:#936817}.metric{margin:12px 0 6px;font-size:13px;color:var(--muted)}.error{padding:12px 15px;background:#f6f8fa;border-radius:8px;font:12px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;margin:14px 0}
details{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}summary{cursor:pointer;font-size:13px;font-weight:600;color:#345478}summary:focus-visible,a:focus-visible{outline:3px solid #9dbaff;outline-offset:4px}pre{background:#152b40;color:#d6e7f2;padding:18px;border-radius:10px;overflow:auto;font:12px/1.75 ui-monospace,SFMono-Regular,Consolas,monospace;max-height:350px;white-space:pre-wrap;overflow-wrap:anywhere}
.section{margin-top:26px}.evidence{display:grid;grid-template-columns:1.1fr 1fr;gap:20px}.facts{margin:0;display:grid;grid-template-columns:110px 1fr;gap:9px 12px;font-size:13px}.facts dt{color:var(--muted)}.facts dd{margin:0;overflow-wrap:anywhere}.hash{font:11px/1.7 ui-monospace,Consolas,monospace}.links{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 0}.links a{display:inline-block;padding:7px 12px;background:#eff4fb;border-radius:8px;text-decoration:none;font-size:12px}.note{font-size:13px;color:var(--muted)}ul{padding-left:19px;margin:0}li{margin:5px 0}.footer{border-top:1px solid var(--line);margin-top:30px;padding-top:18px;display:flex;justify-content:space-between;gap:14px;font-size:12px;color:var(--muted)}
.observations{display:grid;grid-template-columns:85px 1fr;gap:10px 12px;margin:18px 0;padding:17px;background:#f4f7fb;border-radius:10px;font-size:14px}.observations dt{color:var(--muted)}.observations dd{margin:0;overflow-wrap:anywhere;font-weight:650}.observations dd.actual{font-size:23px;line-height:1.35;color:#a53f38}.passed .observations dd.actual{color:#127753}.diff-add{display:block;background:#174539;color:#a6efc9}.diff-remove{display:block;background:#4c303a;color:#ffc2bf}.diff-line{display:block}.file-head{display:flex;justify-content:space-between;gap:10px;font:13px/1.7 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}.origin{background:#f0f4fa;padding:20px;border-radius:10px;margin-top:22px}.origin p:last-child{margin:0}
@media(max-width:700px){header{padding:18px 20px}.version{max-width:150px;text-align:right;font-size:10px}main{padding:25px 18px}.grid,.evidence{grid-template-columns:1fr}.flow{grid-template-columns:1fr 1fr}.card{padding:19px}.result{padding:19px;gap:12px}.facts{grid-template-columns:90px 1fr}.footer{display:block}}
@media print{header{background:white;color:var(--ink)}body{background:white}main{padding:15px}.card,.result{break-inside:avoid}.links{display:none}}
"""


def write_report(result: dict) -> Path:
    # Existing callers may request HTML after run_case has already committed it.
    if result.get("persistence", {}).get("status") == "complete":
        path = Path(result["report_dir"]) / "report.html"
        if not path.is_file():
            raise ValueError("已保存的 HTML 报告缺失；请检查证据并重新验收。")
        return path
    e = lambda value: escape(str(value), quote=True)
    heading, color, symbol = STATUS[result["status"]]
    report_dir = Path(result["report_dir"])
    expected = result.get("expected_failure") or {"type": "回归保护", "message_contains": "比较同一检查的前后表现"}
    source_note = ""
    try:
        with tokenize.open(report_dir / "evidence/regression.py") as test_file:
            test_text = test_file.read()
    except (SyntaxError, UnicodeError, LookupError):
        test_text = (report_dir / "evidence/regression.py").read_bytes().decode("utf-8", errors="replace")
        source_note = "测试源码编码无效，以下按替代字符显示；原始字节仍保存在测试文件中。"
    diagnosis = result.get("diagnosis") or diagnose_result(result)
    steps = diagnosis.get("next_steps", [])
    if result["status"] == "verified" and steps:
        steps = steps[-1:]
    diagnostic_html = '<section class="card section"><h2>结果解释与下一步</h2>'
    diagnostic_html += '<p>' + e(diagnosis.get("summary", result["explanation"])) + '</p>'
    diagnostic_html += '<ul>' + ''.join('<li>' + e(step) + '</li>' for step in steps) + '</ul></section>'
    environment = result.get("environment") or {}
    environment_html = '<section class="card section"><h2>环境版本记录</h2>'
    if environment.get("status") == "recorded":
        packages = environment.get("distributions", {})
        environment_html += '<p>本次运行前记录 ' + str(len(packages)) + ' 个发行包的版本元数据。</p>'
        environment_html += '<dl class="facts"><dt>Python 路径</dt><dd>' + e(environment.get("executable")) + '</dd>'
        environment_html += '<dt>系统 / 架构</dt><dd>' + e(environment.get("platform")) + ' / ' + e(environment.get("machine")) + '</dd>'
        environment_html += '<dt>环境指纹</dt><dd class="hash">' + e(environment.get("fingerprint")) + '</dd></dl>'
        environment_html += '<details><summary>展开实际环境版本清单</summary><pre>' + e('\n'.join(f"{name} == {version}" for name, version in sorted(packages.items()))) + '</pre></details>'
    else:
        environment_html += '<p>本报告没有可用的环境版本清单，不能据此确认与其他环境一致。</p>'
    comparison = result.get("environment_comparison") or {}
    if comparison.get("status") == "changed":
        environment_html += '<p>与参考记录比较：' + e(comparison.get("notice")) + '</p>'
        changes = [f"新增 {p['name']} {p['version']}" for p in comparison.get("added", [])]
        changes += [f"移除 {p['name']} {p['version']}" for p in comparison.get("removed", [])]
        changes += [f"{p['name']}: {p['before']} → {p['after']}" for p in comparison.get("changed", [])]
        if comparison.get("path_changed"): changes.insert(0, "Python 解释器路径发生变化。")
        if comparison.get("python_changed"): changes.insert(0, "Python 版本发生变化。")
        if comparison.get("platform_changed"): changes.insert(0, "系统或架构发生变化。")
        environment_html += '<ul>' + ''.join('<li>' + e(change) + '</li>' for change in changes) + '</ul>'
    elif comparison.get("status") == "same" or (comparison.get("status") == "unknown" and result.get("environment_reference_present")):
        environment_html += '<p>' + e(comparison.get("notice")) + '</p>'
    from .runtime_environment import describe_variables
    environment_html += '<p class="note">' + e(describe_variables(result.get('runtime_environment'))) + '</p>'
    environment_html += '<p class="note">版本清单不是依赖锁，不检查所有包能否导入，也不保证环境内文件没有改变。</p></section>'
    cards = []
    for label, name in (("before", "修复前的代码"), ("after", "修复后的代码")):
        run = result[label]
        failure = run.get("failure")
        error = f"{failure['type']}: {failure['message']}" if failure else run["reason"]
        observations = "".join(f'<dt>{e(p["name"])}</dt><dd class="{"actual" if p["name"] == "实际输出" else ""}">{e(p["value"])}</dd>'
                               for p in run.get("observations", []))
        observation_html = f'<dl class="observations">{observations}</dl>' if observations else ""
        diagnostic = f'<div class="error">{e(error)}</div>' if failure else ""
        cards.append(f"""<article class="card {e(run['kind'])}"><div class="label">{'BEFORE' if label == 'before' else 'AFTER'}</div>
<h3>{name}</h3><span class="pill {e(run['kind'])}">{e(diagnosis.get(label, {}).get('summary', KINDS[run['kind']]))}</span>
{observation_html}
<p class="metric">实际耗时 {run['duration_seconds']:.2f} 秒 · 收集 {e(run['collected'])} 个测试 · 退出码 {e(run['exit_code'])}</p>
{diagnostic}<p class="note">{e(run['reason'])}</p>
<details><summary>查看这次运行的实际日志</summary><pre>{e(run['log_excerpt'])}</pre><a href="{label}.log">打开完整日志</a></details></article>""")
    origin = result.get("provenance", {})
    origin_html = ""
    if origin:
        origin_html = f'''<section class="origin"><h2>来源与版本</h2><p><strong>{e(origin['project'])}</strong> · <a href="{e(origin['issue_url'])}" rel="noreferrer">查看官方问题或修复记录 ↗</a></p><dl class="facts"><dt>修复前</dt><dd>{e(origin['before_ref'])}</dd><dt>修复后</dt><dd>{e(origin['after_ref'])}</dd></dl><p class="note" style="margin-top:12px">{e(origin['scope'])}</p><p class="note">以上来源由案例作者声明；本次程序负责运行测试。</p></section>'''
    demo = '<span class="demo">内置教学案例 · 故障由我们构造 · 两次测试均真实运行</span>' if result["demo"] else '<span class="demo">历史问题复现 · 保留来源记录 · 实际执行结果</span>' if origin else '<span class="demo">用户提供的本地案例 · 实际执行结果</span>'

    workspace = result.get("workspace_origin", {})
    if workspace:
        origin_html += '<section class="origin"><h2>当前工作区快照</h2><p>基线提交：' + e(workspace.get("before_commit")) + '</p><p>修复后采用保存时冻结的 ' + e(workspace.get("file_count")) + ' 个文件；之后编辑原仓库不会改变这份证据。</p><p class="hash">工作区指纹：' + e(workspace.get("after_sha256")) + '</p></section>'
    if result.get("failure_origin"):
        origin_html += '<section class="origin"><h2>目标故障的来源</h2><p>目标故障来自旧版试运行，并经用户确认。本报告依据正式两版运行记录给出验收结论。</p><p>试运行编号 ' + e(result['failure_origin'].get('probe_id')) + '</p></section>'
    project_failure = result.get("project_failure_origin") or {}
    if project_failure:
        reference = project_failure['source_report']
        observation = project_failure['observation']
        confirmation = project_failure['confirmation']
        origin_html += '<section class="origin"><h2>项目报告中的实际故障与确认条件</h2><p>故障版来自来源报告真正失败的变更后快照；候选版为另外冻结的当前工作区。本报告结论仅依据本次两版正式运行。</p>'
        origin_html += '<dl class="facts">' + ''.join('<dt>' + e(name) + '</dt><dd class="hash">' + e(value) + '</dd>' for name, value in (
            ('来源会话 / 运行', reference['project_id'] + ' / ' + reference['run_id']),
            ('报告 SHA-256', reference['report_sha256']), ('完整测试 ID', project_failure['test_target']),
            ('历史两版状态', observation['before']['status'] + ' / ' + observation['after']['status']),
            ('故障快照冻结时间', project_failure['failure_snapshot']['frozen_at']),
            ('候选冻结时间', project_failure['candidate']['frozen_at']),
            ('共享测试 SHA-256', project_failure['test_sha256']),
            ('当前测试文件变化', project_failure['test_change']['status']),
            ('确认的问题', confirmation['problem']), ('确认的期望行为', confirmation['expected_behavior']),
            ('确认的异常', confirmation['type'] + ': ' + confirmation['message_contains']))) + '</dl>'
        origin_html += '<p>原报告基线仅作背景：' + e(project_failure['baseline_background']['sha256']) + '；本次运行不会用它替代实际故障版。历史批量失败若本次单项通过，结论是本次未复现。</p></section>'
    if result.get("input_integrity_notice"):
        origin_html += '<section class="origin"><h2>固定输入的权限记录</h2><p>' + e(result['input_integrity_notice']) + '</p></section>'
    revision = result.get("git_revision", {})
    if revision:
        origin_html += f"""<section class="origin"><h2>Git 版本记录</h2><p>仓库：{e(revision['repository'])}</p><dl class="facts"><dt>修复前</dt><dd>{e(revision['before_ref'])}<br><span class="hash">{e(revision['before_commit'])}</span></dd><dt>修复后</dt><dd>{e(revision['after_ref'])}<br><span class="hash">{e(revision['after_commit'])}</span></dd></dl><p class="note">使用已提交的文件内容；未提交修改没有参与比较。</p></section>"""


    draft_origin = result.get("draft_origin", {})
    if draft_origin:
        edited = "与起草时不同，运行的是人工修改后的测试。" if draft_origin.get("test_sha256") != result["test_sha256"] else "与保存的草稿指纹一致。"
        if draft_origin.get('provider') == 'human-review':
            origin_html += f"""<section class="origin"><h2>人工审阅的检查来源</h2><dl class="facts"><dt>父计划</dt><dd>{e(draft_origin.get('parent_suite_id', ''))}</dd><dt>父计划指纹</dt><dd>{e(draft_origin.get('parent_plan_sha256', ''))}</dd><dt>修订时间</dt><dd>{e(draft_origin.get('reviewed_at', ''))}</dd><dt>测试文件</dt><dd>{e(edited)}</dd></dl><p class="note">这是另存的人工审阅版本。修订不代表通过，验收结论只由本次实际运行决定。</p></section>"""
        else:
            origin_html += f"""<section class="origin"><h2>AI 测试草稿来源</h2><dl class="facts"><dt>接入方式</dt><dd>{e(draft_origin.get('provider', ''))}</dd><dt>模型</dt><dd>{e(draft_origin.get('model', ''))}</dd><dt>测试文件</dt><dd>{e(edited)}</dd></dl><p class="note">模型信息是生成记录或导入者声明，不代表测试质量。验收结论只由本次实际运行决定。</p></section>"""

    review = result.get("draft_review", {})
    if review:
        assumptions = "".join(f"<li>{e(item)}</li>" for item in review["assumptions"])
        review_title = '人工审阅的检查说明' if draft_origin.get('provider') == 'human-review' else 'AI 草稿检查说明'
        origin_html += f"""<section class="origin"><h2>{review_title}</h2><h3>检查目标</h3><p>{e(review['checks'])}</p><h3>预期行为</h3><p>{e(review['expected_behavior'])}</p><h3>检查假设</h3><ul>{assumptions}</ul><p class="note">以上为准备检查时的说明；测试编辑后仍以实际执行文件和本次运行证据为准。</p></section>"""

    runtime = result.get("runtime", {})
    target = result.get("test_target")
    from .pytest_profile import describe_pytest_profile
    if runtime:
        mode = "项目内测试" if target else "独立测试"
        explanation = ("两边运行副本都在此位置使用同一份共享测试文件；各版本的 conftest 和配置分别保留。源码证据仍保留原始版本。"
                       if target else "在独立控制目录运行提供的测试文件。")
        if target:
            explanation += e(describe_pytest_profile(result.get("pytest_profile")))
        origin_html += f"""<section class="origin"><h2>测试与运行环境</h2><dl class="facts"><dt>模式</dt><dd>{mode}</dd><dt>测试目标</dt><dd>{e(target or 'regression.py')}</dd><dt>Python</dt><dd>{e(runtime['python_version'])}</dd><dt>pytest</dt><dd>{e(runtime['pytest_version'])}</dd><dt>解释器</dt><dd class="hash">{e(runtime['executable'])}</dd></dl><p class="note">{explanation}</p></section>"""

    changes = result.get("source_changes", {"files": [], "total": 0, "truncated": False})
    diff_blocks = []
    for item in changes["files"]:
        lines = []
        for line in item["diff"].splitlines():
            style = "diff-add" if line.startswith("+") and not line.startswith("+++") else "diff-remove" if line.startswith("-") and not line.startswith("---") else "diff-line"
            lines.append(f'<span class="{style}">{e(line) or " "}</span>')
        preview = f'<pre>{"".join(lines)}</pre>' if lines else ""
        kind = {"added": "新增", "removed": "删除", "modified": "修改"}[item["kind"]]
        diff_blocks.append(f'<details open><summary class="file-head">{e(item["path"])} <span>{kind}</span></summary>{preview}<p class="note">{e(item["note"])}</p></details>')
    diff_html = f'<section class="card section"><h2>修复改了哪里</h2><p class="note">两份源码快照中有 {changes["total"]} 个文件发生变化。绿色为新增内容，红色为删除内容。</p>{"".join(diff_blocks)}'
    if changes["truncated"]:
        diff_html += '<p class="note">这里只展示前 20 个变更文件，完整内容保存在源码快照中。</p>'
    diff_html += '</section>'
    timestamp = datetime.fromisoformat(result["generated_at"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>ReproLab · {e(result['title'])}</title><style>{CSS}</style></head><body>
<header><div class="brand">Repro<span>Lab</span></div><div class="version">修复验收 · LOCAL / v{e(__version__)}</div></header>
<main><p class="eyebrow">一次问题，一份测试，两次验证</p><h1>{e(result['title'])}</h1>{demo}
<p class="lead">{e(result['description'])}</p>
<aside class="intro"><p><strong>这个项目到底做什么？</strong> 它像一位“修复验收员”：把同一份检查分别交给旧代码和新代码执行，再把证据放在一起，让你判断这次修复是否通过了检查。</p></aside>
<div class="flow"><div class="step"><small class="num">01 / 输入</small><b>提供同一份测试</b><span>先写清应该发生什么</span></div><div class="step"><small class="num">02 / 复现</small><b>检查修复前的代码</b><span>核对是否出现指定故障</span></div><div class="step"><small class="num">03 / 验证</small><b>检查修复后的代码</b><span>观察同一份测试是否通过</span></div><div class="step"><small class="num">04 / 留证</small><b>保存结果与源码快照</b><span>其他人可以重新运行核对</span></div></div>
<section class="result {color}" aria-label="本次结论"><div class="icon" aria-hidden="true">{symbol}</div><div><h2>{e(heading)}</h2><p>{e(result['explanation'])}</p><p class="note">结论仅覆盖本案例，不能据此判断整个软件已经没有其他问题。</p></div></section>
<div class="grid">{''.join(cards)}</div>
{diagnostic_html}{origin_html}{diff_html}{environment_html}
<section class="card section"><h2>这份测试检查了什么</h2><p class="note">下面的文件在两次运行中保持相同内容。测试中的断言定义了通过条件；工具没有自动更改测试来让新版通过。</p>
<details><summary>展开实际执行的测试文件</summary><p class="note">{e(source_note)}</p><pre>{e(test_text)}</pre></details>
<div class="links"><a href="evidence/regression.py">测试文件</a><a href="report.json">结构化结果 JSON</a><a href="evidence/case.json">可重新运行的案例配置</a></div></section>
<div class="evidence section"><section class="card"><h2>可核对的运行记录</h2><dl class="facts">
<dt>记录时间</dt><dd>{e(timestamp)}</dd><dt>运行编号</dt><dd>{e(result['run_id'])}</dd><dt>Python</dt><dd>{e(result['python_version'])}</dd>
<dt>目标异常</dt><dd>{e(expected['type'])}</dd><dt>消息须包含</dt><dd>{e(expected['message_contains'])}</dd>
<dt>测试 SHA-256</dt><dd class="hash">{e(result['test_sha256'])}</dd></dl><details><summary>查看两份源码的内容指纹</summary><p class="hash">修复前：{e(result['source_sha256']['before'])}<br>修复后：{e(result['source_sha256']['after'])}</p></details></section>
<section class="card"><h2>当前能力范围</h2><ul class="note"><li>已实现：现有测试接入、Python 环境选择、案例保存、版本对比与报告。</li><li>输入输出由测试记录，验收仍以预声明错误和实际测试结果为准。</li><li>可选 AI 测试起草需另行配置；不自动修复代码或接入任意 GitHub 项目。</li><li>只运行可信代码；独立进程不是安全沙箱。</li></ul></section></div>
<footer class="footer"><span>ReproLab · Evidence before conclusions.</span><span>本地离线报告 · 无外部脚本或数据上传</span></footer></main></body></html>"""
    path = report_dir / "report.html"
    from .persistence import atomic_write
    atomic_write(path, html)
    return path
