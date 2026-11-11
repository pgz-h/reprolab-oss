"""Offline batch overview linking to individual evidence reports."""
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .report import CSS


LABELS = {"regression_passed": "回归通过", "regression_detected": "新增失败", "baseline_failure": "两版均失败", "improved": "已改善", "verified": "验证通过", "still_failing": "修复后仍失败",
          "not_reproduced": "未复现问题", "inconclusive": "无法验收", "pending": "未运行", "cancelled": "已取消"}
STATES = {"checks_passed": "所选回归检查通过", "running": "正在运行", "verified": "本批次全部验证通过",
          "attention": "本批次有需要关注的案例", "interrupted": "本批次已中断",
          "cancelled": "本批次已取消"}

EXTRA_CSS = """
.batch-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:24px 0}
.batch-stat{padding:20px;border:1px solid var(--line);border-radius:12px;background:#fff}
.batch-stat strong{display:block;font-size:32px;line-height:1.3;margin-bottom:5px}.batch-stat span{font-size:13px;color:var(--muted)}
.batch-stat.good strong{color:#13785b}.batch-stat.bad strong{color:#ad4139}.batch-stat.warn strong{color:#996a1a}
.table-wrap{overflow-x:auto}.batch-table{width:100%;border-collapse:collapse;text-align:left;font-size:14px}
.batch-table th{font-size:12px;color:var(--muted);padding:12px;border-bottom:2px solid var(--line)}
.batch-table td{padding:17px 12px;vertical-align:top;border-bottom:1px solid var(--line)}
.batch-table td:first-child{min-width:180px;max-width:280px}.batch-table td:nth-child(3){min-width:200px;max-width:430px}
.case-name{font-weight:650;overflow-wrap:anywhere}.case-file{font-size:11px;color:var(--muted);overflow-wrap:anywhere;margin-top:5px}
.batch-table .pill{white-space:nowrap}.batch-table .verified{background:#e7f6ed;color:#127753}
.batch-table .not_reproduced,.batch-table .inconclusive,.batch-table .pending,.batch-table .cancelled{background:#fff5df;color:#936817}
.batch-table a{white-space:nowrap}.progress-note{font-size:15px;color:var(--muted)}
@media(max-width:700px){.batch-stats{grid-template-columns:1fr 1fr}.batch-stat{padding:16px}}
"""


def write_batch_report(summary):
    e = lambda value: escape(str(value), quote=True)
    rows = []
    for entry in summary["entries"]:
        report = entry.get("report")
        link = f'<a href="{e(quote(report, safe="/"))}">查看报告 ↗</a>' if report else '<span class="note">—</span>'
        target = f'<div class="case-file">{e(entry["test"])}</div>' if entry.get("test") else ""
        target += '<div class="case-file">' + ("回归保护" if entry.get("check_mode") == "regression" else "目标修复") + "</div>"
        rows.append(
            f'<tr><td><div class="case-name">{e(entry["title"])}</div>'
            f'<div class="case-file">{e(Path(entry["file"]).name)}</div>{target}</td>'
            f'<td><span class="pill {e(entry["status"])}">{e(LABELS[entry["status"]])}</span></td>'
            f'<td>{e(entry["explanation"])}</td><td>{link}</td></tr>'
        )
    counts = summary["counts"]
    cards = "".join(
        f'<div class="batch-stat {style}"><strong>{counts[key]}</strong><span>{label}</span></div>'
        for key, label, style in (("verified", "验证通过", "good"),
                                   ("still_failing", "修复后仍失败", "bad"),
                                   ("not_reproduced", "未复现问题", "warn"),
                                   ("inconclusive", "无法验收", "warn"))
    )
    if any(entry.get("check_mode") == "regression" for entry in summary["entries"]):
        cards += "".join(f'<div class="batch-stat {style}"><strong>{counts.get(key, 0)}</strong><span>{title}</span></div>'
                         for key, title, style in (("regression_passed", "回归通过", "good"), ("regression_detected", "新增失败", "bad"),
                                                   ("baseline_failure", "两版均失败", "warn"), ("improved", "表现改善", "good")))
    timestamp = datetime.fromisoformat(summary["updated_at"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    headline = STATES[summary["status"]]
    note = "每个案例只运行一次；单个案例出错不会阻止其他案例。"
    if summary["status"] == "running":
        note += " 本页面按案例完成情况保存，刷新可查看最新进度。"
    if summary["status"] in ("interrupted", "cancelled"):
        note += " 已完成的结果已保存，未运行的案例没有验收结论。"
    provenance = ""
    if summary.get("suite"):
        suite = summary["suite"]
        provenance = '<section class="card section"><h2>' + e(suite.get("title", "AI 项目验收")) + '</h2><p>' + e(suite.get("issue", "")) + '</p><p>目标修复检查核对预声明故障，回归保护检查比较前后表现。</p></section>'

    origin = summary.get("retry_origin")
    if origin:
        mode = "重跑未通过项" if origin["mode"] == "not_verified" else "继续未完成项"
        selected = "、".join(f"#{entry['index']} {entry['title']}" for entry in origin["entries"])
        provenance += (f'<section class="card section"><h2>重跑来源</h2><dl class="facts">'
                      f'<dt>来源批次</dt><dd>{e(origin["source_batch_id"])}</dd>'
                      f'<dt>来源汇总</dt><dd>{e(origin["source_summary"])}</dd>'
                      f'<dt>来源 SHA-256</dt><dd>{e(origin["source_sha256"])}</dd>'
                      f'<dt>选择方式</dt><dd>{e(mode)}</dd>'
                      f'<dt>原批次案例</dt><dd>{e(selected)}</dd></dl>'
                      '<p class="note">原始报告保留；本批次使用当前案例内容和环境重新验收。</p></section>')
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>ReproLab · 案例批量总览</title><style>{CSS}{EXTRA_CSS}</style></head><body>
<header><div class="brand">Repro<span>Lab</span></div><div class="version">案例总览 · v{e(__version__)}</div></header>
<main><p class="eyebrow">把每个案例的结果放在一起</p><h1>{e(headline)}</h1>
<p class="progress-note">已处理 {summary['completed']} / {summary['total']} 个案例 · 用时 {summary['duration_seconds']:.2f} 秒</p>
<p class="lead">{e(note)}</p><div class="batch-stats">{cards}</div>
<section class="card"><h2>案例结果</h2><div class="table-wrap"><table class="batch-table">
<thead><tr><th>案例 / 测试目标</th><th>结论</th><th>说明</th><th>详细证据</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></section>
{provenance}
<section class="card section"><h2>本批次记录</h2><dl class="facts">
<dt>批次编号</dt><dd>{e(summary['batch_id'])}</dd><dt>更新时间</dt><dd>{e(timestamp)}</dd>
<dt>案例目录</dt><dd>{e(summary['case_directory'])}</dd></dl>
<div class="links"><a href="summary.json">结构化汇总 JSON</a></div>
<p class="note" style="margin-top:16px">验证通过只覆盖各案例定义的检查，不代表整个项目没有其他问题。无法验收可能来自配置、环境或执行问题，请查看对应说明。</p></section>
<footer class="footer"><span>ReproLab · 本地批量验收</span><span>离线报告 · 无外部脚本</span></footer></main></body></html>"""
    path = Path(summary["summary_dir"]) / "summary.html"
    temp = path.with_suffix(".html.tmp")
    temp.write_text(html, encoding="utf-8")
    temp.replace(path)
    return path
