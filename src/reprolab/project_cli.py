"""Command-line entry point for saved, multi-test project regression sessions.

Keep engine imports inside dispatch: help and argument errors must also work in
the desktop distribution without importing any GUI modules.
"""

import argparse
import json
import math
import re
from pathlib import Path
import sys


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _UsageError(message)


def _timeout(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("超时必须是 0.1–3600 之间的秒数。") from None
    if not math.isfinite(seconds) or not 0.1 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("超时必须是 0.1–3600 之间的秒数。")
    return seconds


def _clock_options(parser):
    parser.add_argument('--freeze-clock', help='固定带 UTC 偏移的 Python 墙钟时间')
    parser.add_argument('--timezone', help='Python 工作进程使用的 IANA 时区')
    parser.add_argument('--locale', dest='worker_locale', help='Python 工作进程使用的系统区域设置')


def _runtime_options(parser, *, inherit_timeout=False):
    parser.add_argument("--progress", action="store_true", help="在标准错误输出显示实时测试进度，标准输出保持 JSON")
    parser.add_argument("--python", help="项目环境中的 Python 解释器路径")
    default_help = "默认沿用来源报告（无记录时为 120）" if inherit_timeout else "默认 120"
    parser.add_argument("--timeout", type=_timeout, default=None if inherit_timeout else 120,
                        help=f"每版全部所选测试的总超时秒数，{default_help}，范围 0.1–3600")


def _selection_options(parser, *, allow_plan=False):
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--test', action='append', help='完整测试 ID，可重复')
    selection.add_argument('--selection-file', type=Path, help='JSON 或逐行文本测试选择清单')
    if allow_plan:
        selection.add_argument('--acceptance-plan', type=Path, metavar='FILE',
                               help='使用 pytest 验收计划的精确测试目标')


def _acceptance_run_options(parser):
    parser.add_argument('--acceptance-plan', type=Path, metavar='FILE',
                        help='使用计划的目标，或与显式范围和 --verify-plan 一起使用')
    parser.add_argument('--verify-plan', action='store_true',
                        help='运行后校验保存的报告并按计划验收结果返回退出码')
    parser.add_argument('--summary-file', type=Path, metavar='FILE',
                        help='将本次比较的 Markdown 摘要保存到新文件')
    parser.add_argument('--require-receipt', type=Path, metavar='DIRECTORY', help='验收时核对已有回传回执')
    parser.add_argument('--require-stability', type=Path, metavar='CHECK', help='验收时核对已有稳定性检查')


def _npm_options(parser, *, replay=False):
    parser.add_argument('--node', help='Node.js 可执行文件路径，默认使用 PATH')
    parser.add_argument('--npm', help='npm 可执行文件路径，默认使用 PATH')
    parser.add_argument('--npm-cache', type=Path, help='可选的 npm 缓存目录')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--offline', action='store_true', default=None if replay else False,
                      help='依赖安装只使用本机缓存')
    if replay:
        mode.add_argument('--online', action='store_false', dest='offline',
                          help='允许 npm 从 registry 下载所需依赖')


def _expected_revision(value):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64}', value):
        raise argparse.ArgumentTypeError('须使用完整 Git 提交，或 sha256: 加上冻结源码指纹。')
    return value


def _review_summary_options(parser):
    parser.add_argument('--acceptance-plan', type=Path, metavar='FILE',
                         help='从 JSON 加载可复用的验收条件')
    parser.add_argument('--failure-waivers', type=Path, help='允许指定 pytest 已知故障的有期限豁免')
    parser.add_argument('--waiver-date', help='评估豁免使用的日期，YYYY-MM-DD')
    parser.add_argument('--max-report-age-hours', type=int, help='报告允许的最长小时数')
    parser.add_argument('--report-as-of', help='用于核对时效的带偏移 ISO 8601 时间')
    parser.add_argument('--expect-before', type=_expected_revision, help='要求报告匹配该基线提交或源码指纹')
    parser.add_argument('--expect-after', type=_expected_revision, help='要求报告匹配该候选提交或源码指纹')
    parser.add_argument('--expect-plan-sha256', help='要求当前使用的验收计划与已批准指纹相同')
    parser.add_argument('--require-approval', type=Path, help='要求绑定本报告及计划的具名审查者签名')
    parser.add_argument('--approval-key', type=Path, help='独立取得的可信审查者公钥')
    parser.add_argument('--expect-reviewer', help='预期审查者身份')
    names = parser.add_mutually_exclusive_group()
    names.add_argument('--include-test-ids', action='store_true', help='附上最多 20 个需关注或改善的测试名称')
    names.add_argument('--public-summary', action='store_true', help='只生成可有限分享的统计和门禁摘要')
    parser.add_argument('--gate', choices=('strict', 'no-new-failures'),
                         help='按报告结论返回退出码 0、1 或 2；使用验收要求时默认为 strict')
    parser.add_argument('--require-improvement', action='append', default=[], metavar='TEST_ID',
                         help='要求指定 pytest 或 Go 测试从失败变为通过，可重复')
    parser.add_argument('--require-vitest-case', action='append', nargs=2, default=[], metavar=('FILE', 'CASE'),
                         help='要求指定 Vitest 文件中的完整用例名从失败变为通过，可重复')
    parser.add_argument('--require-receipt', type=Path, metavar='DIRECTORY',
                         help='要求回传材料一致，且另一轮逐项结果完整复现')
    parser.add_argument('--require-stability', type=Path, metavar='CHECK',
                         help='从稳定性摘要及轮次证据离线复核每轮逐项结果')
    parser.add_argument('--min-stability-rounds', type=int, metavar='N',
                         help='验收所需的最少复现轮数，默认 2，范围 2–10')


def _parser():
    parser = _Parser(
        prog="reprolab project",
        description="冻结项目版本、发现并选择测试、比较前后结果，保存可核对的项目回归报告。",
        epilog="review、collect、run、recheck 和明确确认的 replay 会加载并运行可信项目代码；独立进程不是安全沙箱。",
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    demo = commands.add_parser('demo', help='实际运行内置购物车项目并生成报告，预期检测到一个新增失败')
    demo.add_argument('--output', type=Path, required=True, help='教学项目、会话与报告的保存根目录')
    demo.add_argument('--review-handoff', action='store_true', help='另存证据 ZIP、验收计划和供接收方核对的摘要')
    prepare = commands.add_parser("prepare", help="冻结两个版本或当前工作区，创建项目会话")
    review = commands.add_parser("review", help="一次冻结、发现并运行 pytest 项目对比")
    for command in (prepare, review):
        command.add_argument("repository", type=Path, help="本地 Git 仓库目录")
        command.add_argument("--before", help="基线 Git 提交或引用")
        revisions = command.add_mutually_exclusive_group(required=True)
        revisions.add_argument("--after", help="对比 Git 提交或引用")
        revisions.add_argument("--working-tree", action="store_true", help="冻结当前工作区作为对比版本")
        revisions.add_argument('--github-event', type=Path, help='使用保存的 GitHub PR 事件自动绑定两个版本')
        command.add_argument('--pr-mode', choices=('head', 'merge'))
        command.add_argument('--merge-commit', help='显式提供本地 PR 合并结果 SHA')
        command.add_argument("--title", help="项目会话名称")
        command.add_argument("--issue", default="", help="本次改动的问题与检查说明")
        command.add_argument("--path", default="", help="发现范围，项目内的相对测试文件或目录")
    prepare.add_argument("--output", type=Path, required=True, help="新项目会话的保存根目录")
    prepare.add_argument("--python", help="项目环境中的 Python 解释器路径")
    review.add_argument("--output", type=Path, required=True, help="项目外的输出根目录，分别保存 sessions 和 reports")
    review.add_argument('--audit-discovery', action='store_true', help='核对两版原生发现清单，即使已显式选择测试')
    _selection_options(review)
    _acceptance_run_options(review)
    for command in (prepare, review):
        command.add_argument('--source-path', action='append', default=[], help='只冻结指定的仓库文件或目录，可重复')

    _runtime_options(review)
    _clock_options(review)
    review.epilog = "省略测试选择时仅运行完整发现清单（最多 2000 项）。仅对可信项目发起执行。"

    vitest = commands.add_parser('vitest', help='对比 Vitest 4 项目中所选测试文件的两版结果')
    vitest.add_argument('repository', type=Path, help='本地 Git 仓库目录')
    vitest.add_argument('--before', required=True, help='基线 Git 提交或引用')
    candidate = vitest.add_mutually_exclusive_group(required=True)
    candidate.add_argument('--after', help='对比 Git 提交或引用')
    candidate.add_argument('--working-tree', action='store_true', help='以当前工作区作为对比版本')
    vitest.add_argument('--project-dir', default='.', help='仓库内的独立 npm 项目或 workspace 成员目录，默认根目录')
    vitest_selection = vitest.add_mutually_exclusive_group()
    vitest_selection.add_argument('--file', action='append', help='相对于 Git 仓库根目录的测试文件，可重复')
    vitest_selection.add_argument('--selection-file', type=Path, help='JSON 或逐行文本测试文件清单')
    vitest_selection.add_argument('--list-files', action='store_true', help='列出候选配置匹配的测试文件，输出 JSON')
    vitest_selection.add_argument('--all-files', action='store_true', help='发现并比较候选配置匹配的全部测试文件')
    _acceptance_run_options(vitest)
    vitest.add_argument('--output', type=Path, help='报告保存根目录，比较时必填')
    vitest.add_argument('--timeout', type=_timeout, default=120, help='发现及两版运行各自的总时限，默认 120 秒')
    _npm_options(vitest)

    go = commands.add_parser('go', help='比较单个 Go 包中所选测试的两版结果')
    go.add_argument('repository', type=Path, help='本地 Git 仓库目录')
    go.add_argument('--before', required=True, help='基线 Git 提交或引用')
    candidate = go.add_mutually_exclusive_group(required=True)
    candidate.add_argument('--after', help='对比 Git 提交或引用')
    candidate.add_argument('--working-tree', action='store_true', help='以当前工作区作为对比版本')
    go.add_argument('--package', default='.', help='仓库内包目录，默认根目录')
    go_selection = go.add_mutually_exclusive_group()
    go_selection.add_argument('--test', action='append', help='顶层 Go 测试函数名称，可重复')
    go_selection.add_argument('--selection-file', type=Path, help='JSON 或逐行文本测试函数清单')
    go_selection.add_argument('--list-tests', action='store_true', help='列出候选包的顶层测试，输出 JSON')
    go_selection.add_argument('--all-tests', action='store_true', help='发现并对比候选包的全部顶层测试')
    _acceptance_run_options(go)
    go.add_argument('--go', help='Go 可执行文件路径，默认使用 PATH')
    go.add_argument('--output', type=Path, help='报告保存根目录，比较时必填')
    go.add_argument('--timeout', type=_timeout, default=120, help='发现及两版运行各自的限时，默认 120 秒')

    show = commands.add_parser("show", help="读取已保存项目会话，不执行项目代码")
    show.add_argument("directory", type=Path, help="项目会话目录")

    refresh = commands.add_parser('refresh', help='沿用固定基线，另存当前工作区为新会话，不发现或运行测试')
    refresh.add_argument('directory', type=Path, help='来源项目会话目录；原会话与报告保留')
    refresh.add_argument('--output', required=True, type=Path, help='新会话的保存根目录')
    refresh.add_argument('--python', help='新会话使用的 Python；省略时沿用来源环境')
    _selection_options(refresh)
    refresh.epilog = '明确提供清单可带入原选择；否则仅沿用会话已有的继承选择。原基线不随 HEAD 移动。'

    mutants = commands.add_parser('mutation-check', help='验证 pytest 选择能否发现应用源码的布尔变异')
    mutants.add_argument('report', type=Path)
    mutants.add_argument('--source-path', required=True)
    mutants.add_argument('--max-mutants', type=int, default=10)
    mutants.add_argument('--confirm-run', action='store_true')
    mutants.add_argument('--output', required=True, type=Path)
    _runtime_options(mutants, inherit_timeout=True)
    mutation_review = commands.add_parser('check-mutations', help='离线核对源码变异和实际试探报告')
    mutation_review.add_argument('directory', type=Path)

    shrink = commands.add_parser('minimize-input', help='逐步缩减捕获的 JSON 失败样本')
    shrink.add_argument('report', type=Path)
    shrink.add_argument('--test', required=True)
    shrink.add_argument('--confirm-run', action='store_true')
    shrink.add_argument('--output', type=Path, required=True)
    shrink.add_argument('--max-probes', type=int, default=40)
    _runtime_options(shrink, inherit_timeout=True)
    shrink_check = commands.add_parser('check-input-reduction', help='核对每个缩减样本与实际试探证据')
    shrink_check.add_argument('directory', type=Path)
    shrink_check.add_argument('--source', type=Path)

    reduction = commands.add_parser('minimize-order', help='实际复跑并逐项移除指定失败前的 pytest 测试')
    reduction.add_argument('report', type=Path)
    reduction.add_argument('--test', required=True)
    reduction.add_argument('--confirm-run', action='store_true')
    reduction.add_argument('--output', type=Path, required=True)
    reduction.add_argument('--max-probes', type=int, default=40)
    _runtime_options(reduction, inherit_timeout=True)

    locate = commands.add_parser('locate-regression', help='逐个实际运行第一父提交线，定位最先观察到的 pytest 回归')
    locate.add_argument('repository', type=Path)
    locate.add_argument('--before', required=True, help='已知通过的 Git 版本')
    locate.add_argument('--after', required=True, help='已知失败的 Git 版本')
    locate.add_argument('--test', action='append', required=True, help='所选 pytest 完整测试 ID，可重复')
    locate.add_argument('--max-commits', type=int, default=40, help='最多运行提交数，范围 2–200')
    locate.add_argument('--output', type=Path, required=True, help='新定位材料根目录')
    _runtime_options(locate)

    runs = commands.add_parser('runs', help='从已保存报告核对、列出或重建项目运行历史')
    runs.add_argument('directory', type=Path, help='项目会话目录')
    runs.add_argument('--reports', type=Path, required=True, action='append', help='报告根目录，可重复')
    runs.add_argument('--rebuild', action='store_true', help='将最近 200 份可核对报告重新写入会话索引')

    recheck = commands.add_parser('recheck', help='明确复查并运行：冻结当前修改、发现并核对原范围，再执行')
    recheck.add_argument('report', type=Path, help='来源项目报告目录')
    recheck.add_argument('--sessions', type=Path, required=True, help='新会话保存根目录')
    recheck.add_argument('--output', type=Path, required=True, help='新报告保存根目录')
    recheck.add_argument('--scope', choices=('all', 'attention'), default='all')
    recheck.add_argument('--test', action='append', help='只复查来源报告中的指定 ID，可重复；与 attention 范围互斥')
    _runtime_options(recheck, inherit_timeout=True)
    recheck.epilog = '缺失测试、发现不完整或测试／插件配置变化时保留新会话并返回 2，等待核对；不会自动缩小范围。'

    selection_check = commands.add_parser('selection-check', help='只读核对选择与已保存的实际发现结果，不启动 Python 或测试')
    selection_check.add_argument('directory', type=Path, help='项目会话目录')
    _selection_options(selection_check, allow_plan=True)
    selection_check.epilog = '省略清单时核对会话的继承选择；缺失项返回退出码 2。核对匹配不代表测试通过。'

    collect = commands.add_parser("collect", help="在固定快照中实际收集测试和参数化 node ID")
    collect.add_argument("directory", type=Path, help="项目会话目录")
    discovery_scope = collect.add_mutually_exclusive_group()
    discovery_scope.add_argument("--path", help="仅收集指定相对测试文件或目录；省略时沿用会话范围")
    discovery_scope.add_argument("--default-scope", dest="path", action="store_const", const="",
                                 help="恢复项目的 pytest 默认发现范围")
    _runtime_options(collect)
    _clock_options(collect)

    run = commands.add_parser("run", help="明确执行所选测试，保存新的项目回归报告")
    run.add_argument("directory", type=Path, help="项目会话目录")
    run.add_argument("--output", type=Path, required=True, help="新运行报告的保存根目录")
    run.add_argument('--audit-discovery', action='store_true', help='核对两版原生发现清单，即使已显式选择测试')
    _selection_options(run)
    _acceptance_run_options(run)
    _runtime_options(run)
    _clock_options(run)
    run.epilog = '省略选择时先使用继承选择，否则使用已收集测试（最多 2000 项）。'

    note = commands.add_parser('repair-note', help='从所选报告准备本地修复说明，不运行或发送')
    note.add_argument('report', type=Path)
    note.add_argument('--test', required=True, help='所选失败的完整测试 ID')
    note.add_argument('--problem', required=True)
    note.add_argument('--expected-behavior', required=True)
    note.add_argument('--allowed-changes', required=True)
    note.add_argument('--omit-test', action='store_true', help='明确省略测试代码')
    note.add_argument('--log', choices=('before', 'after'), action='append', default=[])
    note.add_argument('--diff', action='append', default=[], help='选择报告保存的相对路径差异，可重复')
    note.add_argument('--current-material', type=Path, help='可选：用户另行提供的文本，明确标为当前材料')
    note.add_argument('--output', type=Path, help='保存与输出 text 完全一致的本地文本')

    repair_prepare = commands.add_parser('repair-prepare', help='冻结报告实际故障版及当前候选工作区，只准备固定输入')
    repair_prepare.add_argument('report', type=Path)
    repair_prepare.add_argument('--test', required=True, help='变更后实际主体失败的单个完整测试 ID')
    repair_prepare.add_argument('--repository', type=Path, help='候选工作区；默认使用报告保存的原仓库路径')
    repair_prepare.add_argument('--output', type=Path, required=True, help='准备材料根目录，须位于仓库及报告之外')

    repair_show = commands.add_parser('repair-show', help='核对并重开自带的修复准备，不依赖原报告、不执行')
    repair_show.add_argument('directory', type=Path)

    repair_save = commands.add_parser('repair-save', help='确认条件并保存正式案例；不会执行测试')
    repair_save.add_argument('directory', type=Path, help='repair-prepare 保存的准备目录')
    repair_save.add_argument('--case', type=Path, required=True, help='新的正式案例 JSON，不能覆盖已有案例')
    repair_save.add_argument('--problem', required=True)
    repair_save.add_argument('--expected-behavior', required=True)
    repair_save.add_argument('--failure-type', required=True, help='准备记录中的实际异常类型')
    repair_save.add_argument('--message-contains', required=True, help='实际异常中的稳定非空消息片段')
    repair_save.add_argument('--confirm-conditions', action='store_true', help='明确确认问题、期望行为、来源与稳定消息片段')
    repair_save.add_argument('--preparation-sha256', help='可选：绑定 repair-show 返回的准备记录指纹')

    diagnose = commands.add_parser('diagnose', help='只读归类已有报告的失败现象，保留测试范围与归类依据')
    diagnose.add_argument('report', type=Path)
    diagnose.add_argument('--output', type=Path, help='可选：另存诊断 JSON，不覆盖来源报告')

    history = commands.add_parser('history', help='只读查看多轮复查来源和本轮未复查项，不运行测试')
    history.add_argument('report', type=Path)
    history.add_argument('--output', type=Path, help='导出到来源报告之外的新 JSON 文件')
    history.add_argument('--notes', type=Path, help='可选：读取本机处理说明目录')
    history.add_argument('--source-directory', type=Path, action='append', default=[], help='明确提供已移动的来源报告目录，可重复；仍核对身份')
    history.add_argument('--max-rounds', type=int, default=20, help='最多读取轮数，默认 20，范围 1–50')

    summary = commands.add_parser('summary', help='从已校验的报告生成 PR 摘要，可选 CI 门槛')
    summary.add_argument('report', type=Path, help='项目报告目录、report.json 或证据 ZIP')
    summary.add_argument('--format', choices=('markdown', 'json'), default='markdown')
    _review_summary_options(summary)
    summary_check = commands.add_parser('check-summary', help='离线核对收到的 Markdown 是否与报告及验收条件相符')
    summary_check.add_argument('report', type=Path, help='项目报告目录、report.json 或证据 ZIP')
    summary_check.add_argument('markdown', type=Path, help='待核对的 PR 摘要文件')
    _review_summary_options(summary_check)
    summary_check.add_argument('--facts-file', type=Path, help='同时核对用相同选项导出的项目摘要 JSON')


    github_attest = commands.add_parser('check-github-attestation', help='离线核对证据 ZIP 与可信 GitHub 工作流')
    github_attest.add_argument('archive', type=Path)
    github_attest.add_argument('--bundle', required=True, type=Path)
    github_attest.add_argument('--trusted-root', required=True, type=Path)
    github_attest.add_argument('--repo', required=True)
    github_attest.add_argument('--workflow', required=True)
    github_attest.add_argument('--source-digest', required=True)

    attest = commands.add_parser('attest', help='签署对已核对报告的生产者或审查者声明')
    attest.add_argument('report', type=Path)
    attest.add_argument('--key', required=True, type=Path, help='签名 PEM 私钥')
    attest.add_argument('--actor', required=True)
    attest.add_argument('--role', required=True, choices=('producer', 'reviewer'))
    attest.add_argument('--decision', required=True, choices=('recorded', 'approved', 'rejected'))
    attest.add_argument('--workflow', default='')
    attest.add_argument('--reason', default='')
    attest.add_argument('--plan', type=Path)
    attest.add_argument('--output', required=True, type=Path)

    attest_check = commands.add_parser('check-attestation', help='用可信 PEM 公钥离线核对签名及报告绑定')
    attest_check.add_argument('report', type=Path)
    attest_check.add_argument('attestation', type=Path)
    attest_check.add_argument('--trusted-key', required=True, type=Path)
    attest_check.add_argument('--expect-actor', required=True)
    attest_check.add_argument('--expect-role', required=True, choices=('producer', 'reviewer'))
    attest_check.add_argument('--expect-workflow')
    attest_check.add_argument('--plan', type=Path)

    performance = commands.add_parser('check-performance', help='核对多轮同条件配对计时的性能门槛')
    performance.add_argument('reports', nargs='+', type=Path)
    performance.add_argument('--test', action='append')
    performance.add_argument('--warmup', type=int, default=1)
    performance.add_argument('--max-ratio', type=float, default=1.25)
    performance.add_argument('--min-delta-ms', type=float, default=2.0)

    attempts = commands.add_parser('first-attempts', help='按份统计冻结报告内已观察到的首次尝试')
    attempts.add_argument('reports', nargs='+', type=Path)

    for name in ('link-case-contract', 'check-case-contract'):
        contract = commands.add_parser(name, help='绑定或核对 issue／事故的持续验收契约')
        contract.add_argument('case', type=Path)
        contract.add_argument('report', type=Path)
        contract.add_argument('plan', type=Path)
        contract.add_argument('--failure-source', type=Path)
        contract.add_argument('--output' if name == 'link-case-contract' else '--contract', required=True, type=Path)

    projection = commands.add_parser('project-outcomes', help='以行号替代测试身份并导出可独立读取的结果投影')
    projection.add_argument('report', type=Path)
    projection.add_argument('--output', type=Path, required=True)
    projection_check = commands.add_parser('check-outcomes', help='离线重算结果投影或与具备授权的原报告核对')
    projection_check.add_argument('projection', type=Path)
    projection_check.add_argument('--source', type=Path)

    multi = commands.add_parser('compare-candidates', help='逐个冻结并运行候选提交，保持基线和范围相同')
    multi.add_argument('repository', type=Path)
    multi.add_argument('--before', required=True)
    multi.add_argument('--candidate', nargs=2, action='append', metavar=('NAME', 'REF'), required=True)
    multi.add_argument('--test', action='append')
    multi.add_argument('--plan', type=Path)
    multi.add_argument('--confirm-run', action='store_true')
    multi.add_argument('--output', type=Path, required=True)
    _runtime_options(multi)

    candidates = commands.add_parser('candidate-history', help='并排核对同一基线上的候选证据及复查边')
    candidates.add_argument('--candidate', action='append', nargs=2, metavar=('NAME', 'REPORT'), required=True)
    candidates.add_argument('--plan', type=Path)
    candidates.add_argument('--gate', choices=('strict', 'no-new-failures'), default='strict')

    wheels = commands.add_parser('verify-wheels', help='安装两份 wheel 并运行同一固定下游测试')
    wheels.add_argument('repository', type=Path)
    wheels.add_argument('--ref', default='HEAD')
    wheels.add_argument('--before-wheel', required=True, type=Path)
    wheels.add_argument('--after-wheel', required=True, type=Path)
    wheels.add_argument('--module', required=True)
    wheels.add_argument('--test', required=True, action='append')
    wheels.add_argument('--confirm-run', action='store_true')
    wheels.add_argument('--output', required=True, type=Path)
    _runtime_options(wheels)
    wheel_check = commands.add_parser('check-wheels', help='离线核对安装制品、下游源码和测试记录')
    wheel_check.add_argument('directory', type=Path)

    behavior = commands.add_parser('capture-behavior', help='冻结 Git 两版并保存实际 JSON 行为或 Python API')
    behavior.add_argument('repository', type=Path)
    behavior.add_argument('--before', required=True)
    behavior.add_argument('--after', required=True)
    source = behavior.add_mutually_exclusive_group(required=True)
    source.add_argument('--probe', type=Path)
    source.add_argument('--module')
    behavior.add_argument('--policy', choices=('exact', 'shape'), default='exact')
    behavior.add_argument('--output', type=Path, required=True)
    behavior.add_argument('--confirm-run', action='store_true')
    _runtime_options(behavior)
    behavior_check = commands.add_parser('check-behavior', help='无需执行代码即可核对保存的行为差异')
    behavior_check.add_argument('directory', type=Path)

    service = commands.add_parser('service-matrix', help='用固定本机服务版本组合核对两版消费者')
    service.add_argument('recipe', type=Path)
    service.add_argument('--confirm-run', action='store_true')
    service.add_argument('--output', type=Path, required=True)
    _runtime_options(service)
    service_check = commands.add_parser('check-service-matrix', help='离线核对服务版本与消费者报告')
    service_check.add_argument('directory', type=Path)

    sqlite = commands.add_parser('sqlite-matrix', help='在独立副本核对多条 SQLite 迁移路径与数据')
    sqlite.add_argument('recipe', type=Path)
    sqlite.add_argument('--confirm-run', action='store_true')
    sqlite.add_argument('--output', type=Path, required=True)
    sqlite.add_argument('--timeout', type=_timeout, default=120)
    sqlite_check = commands.add_parser('check-sqlite-matrix', help='离线核对 SQLite 矩阵输入和记录判定')
    sqlite_check.add_argument('directory', type=Path)

    matrix = commands.add_parser('check-environment-matrix', help='离线核对相同版本在不同 Python 环境下的结果矩阵')
    matrix.add_argument('reports', nargs='+', type=Path)
    matrix.add_argument('--gate', choices=('strict', 'no-new-failures'), default='strict')

    shards = commands.add_parser('check-shards', help='离线核对 pytest 分片的范围、固定输入及门禁')
    shards.add_argument('reports', type=Path, nargs='+', help='报告目录、report.json 或证据 ZIP')
    shards.add_argument('--selection-file', required=True, type=Path, help='全部期望测试的清单')
    shards.add_argument('--gate', choices=('strict', 'no-new-failures'), default='strict')

    protect = commands.add_parser('protect-plan', help='从已通过的修复验收固定回归范围，并将目标转为持续通过')
    protect.add_argument('report', type=Path, help='原始报告目录、report.json 或证据 ZIP')
    protect.add_argument('plan', type=Path, help='原验收计划')
    protect.add_argument('--output', type=Path, required=True, help='新的持续保护计划 JSON')
    protect.add_argument('--input-control', type=Path, help='核对并固定已确认修复的基线业务输入')
    protect.add_argument('--require-receipt', type=Path, help='原计划的回传材料')
    protect.add_argument('--require-stability', type=Path, help='原计划的多轮稳定性摘要')

    pin = commands.add_parser('pin-test-contract', help='从已通过的固定样本报告锁定测试与辅助检查')
    pin.add_argument('report', type=Path, help='成功运行原计划的报告或 ZIP')
    pin.add_argument('plan', type=Path, help='包含固定失败样本的持续保护计划')
    pin.add_argument('--file', action='append', default=[], help='额外辅助文件的相对路径，可重复')
    pin.add_argument('--output', type=Path, required=True, help='保存新计划')
    pin.add_argument('--require-receipt', type=Path, help='原计划要求的回执')
    pin.add_argument('--require-stability', type=Path, help='原计划要求的稳定性摘要')

    check = commands.add_parser("check", help="离线检查已有项目报告及证据的一致性")
    check.add_argument("report", type=Path, help="项目回归报告目录")

    verify_replay = commands.add_parser('verify-replay', help='离线核对两个报告的重放输入和逐项结果')
    verify_replay.add_argument('source', type=Path, help='来源报告目录、report.json 或证据 ZIP')
    verify_replay.add_argument('result', type=Path, help='另一次运行的报告目录、report.json 或证据 ZIP')
    verify_replay.add_argument('--format', choices=('json', 'markdown'), default='json',
                               help='默认输出完整 JSON，可选用于 PR 或 CI 的 Markdown 回执')
    verify_replay.add_argument('--include-test-ids', action='store_true',
                               help='在 Markdown 回执中附上最多 20 个逐项结果')

    check_receipt = commands.add_parser('check-receipt', help='用原始证据离线复核接收方回传的回执目录')
    check_receipt.add_argument('source', type=Path, help='来源报告目录、report.json 或证据 ZIP')
    check_receipt.add_argument('receipt', type=Path, help='接收方用 --receipt-dir 生成的目录')

    pack_receipt = commands.add_parser('pack-receipt', help='从两份已保存的项目证据制作重放回执')
    pack_receipt.add_argument('source', type=Path, help='来源报告目录、report.json 或证据 ZIP')
    pack_receipt.add_argument('result', type=Path, help='重放报告目录、report.json 或证据 ZIP')
    pack_receipt.add_argument('--output', type=Path, required=True, help='新的回执目录')

    compare = commands.add_parser('compare', help='只读比较两次保存的运行，区分结果变化与选择范围差异')
    compare.add_argument('earlier', type=Path, help='前次报告目录')
    compare.add_argument('later', type=Path, help='后次报告目录')
    compare.add_argument('--output', type=Path, help='可选：写入新的对比 JSON 文件，不覆盖来源报告')

    pack = commands.add_parser("pack", help="检查并导出项目报告与固定输入 ZIP")
    pack.add_argument("report", type=Path, help="项目回归报告目录")
    pack.add_argument("--output", type=Path, required=True, help="新的 ZIP 文件路径")
    pack.add_argument("--include-sources", action="store_true", help="连同已绑定的复查来源一并导出")
    pack.add_argument("--source-directory", type=Path, action="append", default=[], help="已移动的来源目录，可重复")

    series = commands.add_parser('pack-series', help='把完整复查来源分别打包为可核对的分段目录')
    series.add_argument('report', type=Path)
    series.add_argument('--output', type=Path, required=True)
    series.add_argument('--source-directory', type=Path, action='append', default=[])
    series.add_argument('--part-mib', type=int, default=64)

    for name in ('check-series', 'unpack-series'):
        receive = commands.add_parser(name, help='离线核对分段交付与完整报告身份')
        receive.add_argument('directory', type=Path)
        if name == 'unpack-series':
            receive.add_argument('output', type=Path)

    unpack = commands.add_parser("unpack", help="安全导入项目证据 ZIP，检查内容并恢复文件权限")
    unpack.add_argument("bundle", type=Path, help="项目报告证据 ZIP")
    unpack.add_argument("destination", type=Path, help="新的导入目录，不覆盖已有目录")

    order = commands.add_parser('order-control', help='从 pytest 证据预览或按同一顺序对照两版')
    order.add_argument('report', type=Path, help='项目报告目录、report.json 或证据 ZIP')
    order.add_argument('--order', choices=('baseline', 'requested'), default='baseline',
                       help='两版共用基线实际顺序（默认）或原请求顺序')
    order.add_argument('--output', type=Path, default=Path('outputs/order-control'), help='新对照证据保存根目录')
    order.add_argument('--confirm-run', action='store_true', help='明确同意执行可信证据代码')
    _runtime_options(order, inherit_timeout=True)

    test_source = commands.add_parser('test-source-control', help='从现有 pytest 报告改用基线测试文件复跑两版')
    test_source.add_argument('report', type=Path)
    test_source.add_argument('--output', type=Path, required=True)
    test_source.add_argument('--confirm-run', action='store_true')
    _runtime_options(test_source, inherit_timeout=True)
    test_source_check = commands.add_parser('check-test-source-control', help='离线核对旧新实现与旧新测试的四格结果')
    test_source_check.add_argument('source', type=Path)
    test_source_check.add_argument('result', type=Path)

    fixture = commands.add_parser('fixture-control', help='从固定 pytest 证据让候选使用原基线夹具文件')
    fixture.add_argument('report', type=Path, help='原报告目录、report.json 或证据 ZIP')
    fixture.add_argument('--file', required=True, action='append', help='基线夹具或辅助文件的仓库相对路径，可重复')
    fixture.add_argument('--output', type=Path, default=Path('outputs/fixture-control'), help='新对照证据保存根目录')
    fixture.add_argument('--confirm-run', action='store_true', help='明确同意执行可信证据代码')
    _runtime_options(fixture, inherit_timeout=True)

    input_control = commands.add_parser('input-control', help='使用原基线的业务输入样本复跑两版 pytest')
    input_control.add_argument('report', type=Path, help='原报告目录、report.json 或证据 ZIP')
    input_control.add_argument('--test', action='append', default=[], help='只固定指定失败测试的输入，可重复')
    input_control.add_argument('--output', type=Path, default=Path('outputs/input-control'), help='新对照报告保存根目录')
    input_control.add_argument('--confirm-run', action='store_true', help='明确同意执行可信证据代码')
    _runtime_options(input_control, inherit_timeout=True)

    input_check = commands.add_parser('check-input-control', help='离线核对原始样本和两版复跑记录')
    input_check.add_argument('source', type=Path, help='原报告目录、report.json 或证据 ZIP')
    input_check.add_argument('result', type=Path, help='对照报告目录、report.json 或证据 ZIP')

    version_order = commands.add_parser('version-order', help='从固定 pytest 证据对照 A→B 与 B→A')
    version_order.add_argument('report', type=Path, help='原始报告目录、report.json 或证据 ZIP')
    version_order.add_argument('--output', type=Path, default=Path('outputs/version-order'), help='新对照证据保存根目录')
    version_order.add_argument('--confirm-run', action='store_true', help='明确同意执行可信证据代码')
    _runtime_options(version_order, inherit_timeout=True)

    check_order = commands.add_parser('check-version-order', help='离线核对原报告与两组版本先后对照证据')
    check_order.add_argument('report', type=Path, help='原始报告目录、report.json 或证据 ZIP')
    check_order.add_argument('--ab', type=Path, help='A→B 报告目录、report.json 或证据 ZIP')
    check_order.add_argument('--ba', type=Path, help='B→A 报告目录、report.json 或证据 ZIP')
    check_order.add_argument('--markdown', type=Path, help='同时核对收到的版本先后对照 Markdown')

    replay = commands.add_parser("replay", help="默认预览已有报告的重放范围；明确确认后才运行")
    replay.add_argument("report", type=Path, help="项目回归报告目录、report.json 或证据 ZIP")
    replay.add_argument("--output", type=Path, default=Path("outputs/project-runs"),
                        help="新重放报告的保存根目录，默认 outputs/project-runs")
    replay_action = replay.add_mutually_exclusive_group()
    replay_action.add_argument("--confirm-run", action="store_true", help="明确同意加载可信证据代码并执行重放")
    replay_action.add_argument('--check-environment', action='store_true',
                               help='启动所选可信 Python，检查 pytest、依赖和插件元数据')
    replay.add_argument('--network-isolated', action='store_true', help='在系统明确限制测试网络时重放')
    replay.add_argument('--windows-container', metavar='NAME', help='在本机已断网的 Windows 容器中重放；路径相对于容器')
    replay.add_argument('--verify-result', action='store_true',
                        help='与 --confirm-run 一起使用，重放并核对逐项结果，返回核对退出码')
    replay.add_argument('--receipt-dir', type=Path,
                        help='与 --confirm-run 一起使用，核对结果并在新目录保存证据 ZIP、JSON 和 Markdown 回执')
    _runtime_options(replay, inherit_timeout=True)
    _clock_options(replay)
    _npm_options(replay, replay=True)
    replay.add_argument('--go', help='Go 报告重放使用的 Go 可执行文件')

    stability = commands.add_parser('stability', help='连续重放同一份项目证据，找出轮间结果变化')
    stability.add_argument('report', type=Path, help='来源报告目录、report.json 或证据 ZIP')
    stability.add_argument('--confirm-run', action='store_true', help='明确同意多次执行可信证据中的代码')
    stability.add_argument('--rounds', type=int, default=3, help='顺序重放轮数，默认 3，范围 2–10')
    stability.add_argument('--output', type=Path, required=True, help='稳定性摘要及各轮报告的保存根目录')
    _runtime_options(stability, inherit_timeout=True)
    _npm_options(stability, replay=True)
    stability.add_argument('--go', help='Go 报告重放使用的 Go 可执行文件')

    history = commands.add_parser('stability-history', help='列出原始报告关联的稳定性检查')
    history.add_argument('report', type=Path, help='原始报告目录、report.json 或证据 ZIP')
    history.add_argument('--output-root', type=Path, required=True, help='稳定性检查的保存根目录')
    check_stability = commands.add_parser('check-stability', help='从原报告和保存的轮次报告离线复核稳定性')
    check_stability.add_argument('report', type=Path, help='原始报告目录、report.json 或证据 ZIP')
    check_stability.add_argument('summary', type=Path, help='稳定性检查目录或 replay-stability.json')
    return parser


def _prepare(args, output):
    from .project_sessions import prepare_project
    from .workspace import WORKTREE

    before, after = args.before, WORKTREE if args.working_tree else args.after
    context = None
    if args.github_event:
        from .project_pr_context import github_pr_context

        if before:
            raise _UsageError('请由 GitHub 事件提供版本，或手工指定 --before/--after。')
        context = github_pr_context(args.repository, args.github_event, args.pr_mode, merge_commit=args.merge_commit)
        before, after = context['before'], context['after']
    elif not before or args.pr_mode or args.merge_commit:
        raise _UsageError('普通项目比较需要 --before；PR 选项需与 --github-event 一起使用。')
    return prepare_project(
        args.repository, before, after, output, python_executable=args.python, title=args.title,
        issue=args.issue, collection_path=args.path, snapshot_scope=args.source_path, pr_context=context,
    )


def _execution_plan(args, runner, *, selected=None, discovery=False):
    from .acceptance_plan import read_acceptance_plan, require_plan_runner

    if args.verify_plan and not args.acceptance_plan:
        raise _UsageError('--verify-plan 需要同时指定 --acceptance-plan。')
    if (args.require_receipt or args.require_stability) and not args.verify_plan:
        raise _UsageError('运行时附加核对材料需要 --verify-plan。')
    if args.acceptance_plan is None:
        return None
    if discovery:
        raise _UsageError('验收计划不能与列出或自动选择全部测试的选项同时使用。')
    if selected is not None and not args.verify_plan:
        raise _UsageError('同时提供验收计划和显式测试范围时，需要 --verify-plan。')
    plan = read_acceptance_plan(args.acceptance_plan)
    require_plan_runner(plan, runner)
    return plan


def _execution_result(report, args, plan, control):
    report = _report_with_scope(report)
    code = _run_exit_code(report)
    if not args.verify_plan and args.summary_file is None:
        return report, code
    from .project_review_summary import render_review_markdown, review_summary
    from .runner import RunCancelled

    try:
        summary = review_summary(report['report_dir'], plan=plan if args.verify_plan else None,
                                 receipt=args.require_receipt, stability=args.require_stability, control=control)
    except (OSError, ValueError, RunCancelled, KeyboardInterrupt) as exc:
        error = 'acceptance_error' if args.verify_plan else 'summary_error'
        return {**report, error: {'type': type(exc).__name__, 'message': str(exc)}}, 1 if code == 1 else 2

    result = dict(report)
    if args.verify_plan:
        result['acceptance'] = summary
        code = summary['gate']['exit_code']
    if args.summary_file is not None:
        from .persistence import atomic_write

        try:
            path = args.summary_file.expanduser().absolute()
            atomic_write(path, render_review_markdown(summary), replace=False)
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
            result['summary_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            return result, 1 if code == 1 else 2
        result['summary_file'] = str(path)
    return result, code


def _dispatch(args, *, network_isolation=None):
    from . import project_sessions
    from .runner import RunControl
    from .project_progress import progress_text

    def progress(phase, payload):
        if phase == 'regression_search':
            print(f'历史定位 · {payload["position"]}/{payload["total"]} · {payload["commit"][:12]}',
                  file=sys.stderr, flush=True)
        elif phase == 'version_order_group':
            print('版本先后对照 · ' + ('A→B' if payload['group'] == 'ab' else 'B→A'), file=sys.stderr, flush=True)
        elif phase == 'stability_round':
            print(f'稳定性检查 · 第 {payload["round"]}/{payload["total"]} 轮', file=sys.stderr, flush=True)
        elif phase == 'project_progress':
            text = progress_text(payload)
            if payload.get('current_test'):
                text += ' · ' + payload['current_test']
            print(text, file=sys.stderr, flush=True)
    from ._clock_control import clock_settings

    clock = clock_settings(getattr(args, 'freeze_clock', None), timezone=getattr(args, 'timezone', None),
                           locale_name=getattr(args, 'worker_locale', None))
    control = RunControl(progress=progress if getattr(args, 'progress', False) else None,
                         network_isolation=network_isolation, clock_control=clock)

    if args.command == 'demo':
        from .project_demo import create_project_demo, save_demo_review_handoff
        from .runner import RunCancelled

        prepared = create_project_demo(args.output / 'sessions', control=control)
        result = project_sessions.run_project(prepared['directory'], args.output / 'reports',
                                              confirmed=True, control=control.child())
        result = _report_with_scope(result)
        code = _run_exit_code(result)
        if not args.review_handoff or result['status'] == 'cancelled':
            return result, code
        try:
            handoff, acceptance = save_demo_review_handoff(result, args.output / 'handoffs', control=control)
        except (OSError, ValueError, RunCancelled, KeyboardInterrupt) as exc:
            return {**result, 'handoff_error': {'type': type(exc).__name__, 'message': str(exc)}}, 1 if code == 1 else 2
        return {**result, 'handoff_dir': str(handoff), 'acceptance': acceptance}, acceptance['gate']['exit_code']
    if args.command == 'check-github-attestation':
        from .project_github_attestation import verify_github_attestation

        return verify_github_attestation(args.archive, args.bundle, args.trusted_root, repository=args.repo,
                                         workflow=args.workflow, source_digest=args.source_digest, control=control), 0
    if args.command == 'attest':
        from .project_attestation import attest_report

        return attest_report(args.report, args.output, key=args.key, actor=args.actor, role=args.role,
                             decision=args.decision, workflow=args.workflow, reason=args.reason,
                             plan=args.plan, control=control), 0
    if args.command == 'check-attestation':
        from .project_attestation import check_report_attestation

        return check_report_attestation(args.report, args.attestation, key=args.trusted_key,
                                        actor=args.expect_actor, role=args.expect_role, workflow=args.expect_workflow,
                                        plan=args.plan, control=control)
    if args.command == 'check-performance':
        from .project_performance import check_performance

        return check_performance(args.reports, selected=args.test, warmup=args.warmup,
                                 max_ratio=args.max_ratio, min_delta_ms=args.min_delta_ms, control=control)
    if args.command == 'first-attempts':
        from .project_attempts import first_attempts

        return first_attempts(args.reports, control=control), 0
    if args.command in ('link-case-contract', 'check-case-contract'):
        from .project_case_contract import check_case_contract, link_case_contract

        if args.command == 'link-case-contract':
            return link_case_contract(args.case, args.report, args.plan, args.output,
                                      failure=args.failure_source, control=control)
        return check_case_contract(args.contract, args.case, args.report, args.plan,
                                   failure=args.failure_source, control=control), 0
    if args.command == 'project-outcomes':
        from .project_projection import export_projection

        return export_projection(args.report, args.output, control=control), 0
    if args.command == 'check-outcomes':
        from .project_projection import check_projection

        return check_projection(args.projection, source=args.source, control=control), 0
    if args.command in ('candidate-history', 'compare-candidates'):
        from .acceptance_plan import read_acceptance_plan
        from .project_candidates import compare_candidates, run_candidates

        plan = read_acceptance_plan(args.plan) if args.plan else None
        if args.command == 'candidate-history':
            return compare_candidates(args.candidate, policy=args.gate, control=control, plan=plan)
        return run_candidates(args.repository, args.before, args.candidate, args.output, selected=args.test,
                              plan=plan, python_executable=args.python, timeout=args.timeout,
                              confirmed=args.confirm_run, control=control)
    if args.command == 'verify-wheels':
        from .project_wheels import run_wheels

        return run_wheels(args.repository, args.ref, args.before_wheel, args.after_wheel, args.output,
                          module=args.module, selected=args.test, python_executable=args.python,
                          timeout=args.timeout, confirmed=args.confirm_run, control=control)
    if args.command == 'check-wheels':
        from .project_wheels import check_wheels

        return check_wheels(args.directory, control=control), 0
    if args.command == 'capture-behavior':
        from .project_behavior import capture_behavior

        return capture_behavior(args.repository, args.before, args.after, args.output, probe=args.probe,
                                module=args.module, python_executable=args.python, timeout=args.timeout,
                                policy=args.policy, confirmed=args.confirm_run, control=control)
    if args.command == 'check-behavior':
        from .project_behavior import check_behavior

        return check_behavior(args.directory, control=control), 0
    if args.command == 'service-matrix':
        from .project_service_matrix import run_service_matrix

        return run_service_matrix(args.recipe, args.output, confirmed=args.confirm_run,
                                  python_executable=args.python, timeout=args.timeout, control=control)
    if args.command == 'check-service-matrix':
        from .project_service_matrix import check_service_matrix

        return check_service_matrix(args.directory, control=control), 0
    if args.command == 'sqlite-matrix':
        from .project_sqlite_matrix import run_sqlite_matrix

        return run_sqlite_matrix(args.recipe, args.output, confirmed=args.confirm_run, timeout=args.timeout, control=control)
    if args.command == 'check-sqlite-matrix':
        from .project_sqlite_matrix import check_sqlite_matrix

        return check_sqlite_matrix(args.directory), 0
    if args.command == 'check-environment-matrix':
        from .project_environment_matrix import check_environment_matrix

        return check_environment_matrix(args.reports, policy=args.gate, control=control)
    if args.command == 'check-shards':
        from .project_shards import check_project_shards

        return check_project_shards(args.selection_file, args.reports, policy=args.gate, control=control)
    if args.command == 'pin-test-contract':
        from .project_test_contract import pin_test_contract

        return pin_test_contract(args.report, args.plan, args.output, additional=args.file,
                                 receipt=args.require_receipt, stability=args.require_stability, control=control)
    if args.command == 'protect-plan':
        from .acceptance_plan import promote_plan

        return promote_plan(args.report, args.plan, args.output, receipt=args.require_receipt,
                            stability=args.require_stability, input_control=args.input_control, control=control)
    if args.command in ('summary', 'check-summary'):
        from .acceptance_plan import read_acceptance_plan
        from .project_review_summary import check_review_markdown, render_review_markdown, review_summary

        plan = read_acceptance_plan(args.acceptance_plan) if args.acceptance_plan else None
        result = review_summary(args.report, include_test_ids=args.include_test_ids, policy=args.gate,
                                required_improvements=args.require_improvement,
                                required_vitest_cases=args.require_vitest_case,
                                receipt=args.require_receipt, stability=args.require_stability,
                                min_stability_rounds=args.min_stability_rounds, plan=plan, control=control,
                                expected_sources={side: value for side, value in
                                                  (('before', args.expect_before), ('after', args.expect_after)) if value},
                                expected_plan_sha256=args.expect_plan_sha256, public=args.public_summary,
                                waiver_file=args.failure_waivers, waiver_as_of=args.waiver_date,
                                max_report_age=args.max_report_age_hours, report_as_of=args.report_as_of,
                                approval=(args.require_approval, args.approval_key, args.expect_reviewer))
        if args.command == 'check-summary':
            checked = check_review_markdown(result, args.markdown, facts=args.facts_file)
            return checked, 0 if checked['status'] == 'consistent' else 2
        code = result.get('gate', {}).get('exit_code', 0)
        return (render_review_markdown(result) if args.format == 'markdown' else result), code
    if args.command == 'vitest':
        from .acceptance_plan import PlanSelectionBlocked
        from .project_selections import read_selection_values
        from .vitest_project import list_vitest_files, run_vitest_project
        from .workspace import WORKTREE

        after = WORKTREE if args.working_tree else args.after
        options = {'project_dir': args.project_dir, 'node': args.node, 'npm': args.npm, 'timeout': args.timeout,
                   'offline': args.offline, 'npm_cache': args.npm_cache, 'control': control}
        selected = read_selection_values(args.selection_file) if args.selection_file else args.file
        plan = _execution_plan(args, 'vitest', selected=selected, discovery=args.list_files or args.all_files)
        if args.list_files:
            if args.output is not None:
                raise _UsageError('--list-files 将测试清单输出到 stdout，无需 --output。')
            return list_vitest_files(args.repository, args.before, after, **options), 0
        if selected is None and plan is None and not args.all_files:
            raise _UsageError('请指定 --file、--selection-file、--list-files、--all-files 或 --acceptance-plan。')
        if args.output is None:
            raise _UsageError('Vitest 对比需要 --output 指定报告保存根目录。')
        try:
            result = run_vitest_project(args.repository, args.before, after, selected, args.output,
                                        acceptance_plan=plan, **options)
        except PlanSelectionBlocked as exc:
            return exc.review, 2
        return _execution_result(result, args, plan, control)
    if args.command == 'go':
        from .acceptance_plan import PlanSelectionBlocked
        from .go_project import list_go_tests, run_go_project
        from .project_selections import read_selection_values
        from .workspace import WORKTREE

        after = WORKTREE if args.working_tree else args.after
        options = {'package': args.package, 'go': args.go, 'timeout': args.timeout, 'control': control}
        selected = read_selection_values(args.selection_file) if args.selection_file else args.test
        plan = _execution_plan(args, 'go', selected=selected, discovery=args.list_tests or args.all_tests)
        if args.list_tests:
            if args.output is not None:
                raise _UsageError('--list-tests 将测试清单输出到 stdout，无需 --output。')
            return list_go_tests(args.repository, args.before, after, **options), 0
        if selected is None and plan is None and not args.all_tests:
            raise _UsageError('请指定 --test、--selection-file、--list-tests、--all-tests 或 --acceptance-plan。')
        if args.output is None:
            raise _UsageError('Go 对比需要 --output 指定报告保存根目录。')
        try:
            result = run_go_project(args.repository, args.before, after, args.output, selected=selected,
                                    acceptance_plan=plan, **options)
        except PlanSelectionBlocked as exc:
            return exc.review, 2
        return _execution_result(result, args, plan, control)
    if args.command == "prepare":
        return _prepare(args, args.output), 0
    if args.command == 'mutation-check':
        from .project_mutations import mutation_check

        return mutation_check(args.report, args.source_path, args.output, max_mutants=args.max_mutants,
                              python_executable=args.python, timeout=args.timeout,
                              confirmed=args.confirm_run, control=control)
    if args.command == 'check-mutations':
        from .project_mutations import check_mutations

        return check_mutations(args.directory, control=control), 0
    if args.command == 'minimize-input':
        from .project_input_reduction import reduce_input

        return reduce_input(args.report, args.output, test_id=args.test, budget=args.max_probes,
                            python_executable=args.python, timeout=args.timeout, confirmed=args.confirm_run,
                            control=control)
    if args.command == 'check-input-reduction':
        from .project_input_reduction import check_input_reduction

        return check_input_reduction(args.directory, source=args.source, control=control), 0
    if args.command == 'minimize-order':
        from .project_order_reduction import minimize_order

        return minimize_order(args.report, args.output, test_id=args.test, confirmed=args.confirm_run,
                              python_executable=args.python, timeout=args.timeout, budget=args.max_probes,
                              control=control)
    if args.command == 'locate-regression':
        from .project_regression_search import locate_regression

        return locate_regression(args.repository, args.before, args.after, args.output, selected=args.test,
                                 python_executable=args.python, timeout=args.timeout, max_commits=args.max_commits,
                                 control=control)
    if args.command == 'runs':
        result = project_sessions.scan_project_runs(args.directory, args.reports, rebuild=args.rebuild, control=control)
        return result, 2 if result['issues'] else 0
    if args.command == "show":
        return project_sessions.read_project(args.directory), 0
    if args.command in ('refresh', 'selection-check'):
        from .project_refresh import refresh_project, review_project_selection
        from .project_selections import read_selection
        selected = read_selection(args.selection_file, allow_empty=True) if args.selection_file else args.test
        if args.command == 'refresh':
            return refresh_project(args.directory, args.output, selected_ids=selected,
                                   python_executable=args.python), 0
        from .acceptance_plan import read_acceptance_plan

        plan = read_acceptance_plan(args.acceptance_plan) if args.acceptance_plan else None
        result = review_project_selection(args.directory, selected_ids=selected, acceptance_plan=plan)
        return result, 0 if result['ready'] else 2
    if args.command == 'recheck':
        from .project_refresh import recheck_and_run
        result = recheck_and_run(args.report, args.sessions, args.output, scope=args.scope,
                                python_executable=args.python, timeout=args.timeout, selected_ids=args.test, control=control)
        if result.get('report'):
            from .project_history import read_recheck_progress
            result['review_progress'] = read_recheck_progress(result['report']['report_dir'], control=control)
        if result.get('report'):
            result['report'] = _report_with_scope(result['report'])
            return result, _run_exit_code(result['report'])
        return result, 2
    if args.command == "collect":
        result = project_sessions.collect_project(
            args.directory, python_executable=args.python, timeout=args.timeout, control=control,
            collection_path=args.path,
        )
        return result, 0 if result.get("status") == "collected" and not (result.get("partial") or result.get("truncated")) else 2
    if args.command in ("run", "review"):
        from .acceptance_plan import PlanSelectionBlocked, pytest_plan_targets
        from .project_selections import read_selection

        selected = read_selection(args.selection_file) if args.selection_file else args.test
        plan = _execution_plan(args, 'pytest', selected=selected)
        if plan is not None and selected is None:
            pytest_plan_targets(plan)
        output = args.output
        if args.command == "review":
            prepared = _prepare(args, output / "sessions")
            args.directory = prepared["directory"]
            output /= "reports"
        try:
            result = project_sessions.run_project(
                args.directory, output, selected_ids=selected, python_executable=args.python,
                timeout=args.timeout, confirmed=True, control=control, acceptance_plan=plan,
                audit_discovery=args.audit_discovery,
            )
        except PlanSelectionBlocked as exc:
            return exc.review, 2
        return _execution_result(result, args, plan, control)
    if args.command == 'repair-note':
        from .project_failure import load_repair_material, render_repair_brief, export_repair_brief
        from .draft_context import MAX_FILE_BYTES
        material = load_repair_material(args.report, args.test)
        keys = ([] if args.omit_test else ['test']) + ['log:' + side for side in args.log] + ['diff:' + name for name in args.diff]
        current = ''
        if args.current_material:
            source = args.current_material.expanduser()
            if source.is_symlink() or source.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('当前补充材料须为不超过 24 KiB 的普通 UTF-8 文本。')
            current = source.read_text(encoding='utf-8')
        text = render_repair_brief(material, problem=args.problem, expected_behavior=args.expected_behavior,
            allowed_changes=args.allowed_changes, selected_blocks=keys, current_material=current)
        result = {'kind': 'project_repair_note', 'text': text, 'source_report': material['reference'],
                  'notice': '本地文本；输出 text 与导出完全一致。可编辑本地文件后由用户自行发送。'}
        if args.output:
            result['export'] = export_repair_brief(args.output, text, protected=(material['reference']['report_dir'],
                material.get('path_roots', {}).get('repository')))
        return result, 0
    if args.command == 'repair-prepare':
        from .project_failure import prepare_report_repair
        return prepare_report_repair(args.report, args.test, args.output, repository=args.repository), 0
    if args.command == 'repair-show':
        from .project_failure import read_repair_preparation
        return read_repair_preparation(args.directory), 0
    if args.command == 'repair-save':
        from .project_failure import save_report_repair
        return save_report_repair(args.directory, args.case, problem=args.problem,
            expected_behavior=args.expected_behavior, failure_type=args.failure_type,
            message_contains=args.message_contains, confirmed=args.confirm_conditions,
            expected_preparation=args.preparation_sha256), 0
    if args.command == 'diagnose':
        from .project_diagnostics import diagnose_project_report
        from .persistence import save_json
        result = diagnose_project_report(args.report)
        if args.output:
            path = args.output.expanduser().resolve()
            if path.exists() or path.is_relative_to(Path(result['report_dir'])):
                raise ValueError('请选择来源报告目录之外的新文件保存诊断摘要。')
            path.parent.mkdir(parents=True, exist_ok=True)
            save_json(path, result)
            result['diagnostics_file'] = str(path)
        return result, 0
    if args.command == 'history':
        from .processing_history import read_processing_history, export_processing_history
        locations = _source_locations(args.source_directory)
        result = read_processing_history(args.report, locations=locations, notes_root=args.notes, max_rounds=args.max_rounds)
        if args.output:
            result['export'] = export_processing_history(result, args.output)
        return result, 0 if result['status'] == 'complete' else 2
    if args.command == "check":
        result = project_sessions.check_project_report(args.report)
        return result, 0 if result.get("status") == "consistent" else 2
    if args.command == 'verify-replay':
        from .replay_verification import replay_verification_exit_code, verify_replay

        if args.include_test_ids and args.format != 'markdown':
            raise _UsageError('--include-test-ids 需要与 --format markdown 一起使用。')
        result = verify_replay(args.source, args.result, control=control)
        code = replay_verification_exit_code(result)
        if args.format == 'markdown':
            from .project_review_summary import render_replay_markdown

            return render_replay_markdown(result, include_test_ids=args.include_test_ids), code
        return result, code
    if args.command == 'check-receipt':
        from .replay_receipt import check_replay_receipt
        from .replay_verification import replay_verification_exit_code

        result = check_replay_receipt(args.source, args.receipt, control=control)
        code = replay_verification_exit_code(result['replay_verification']) if result['status'] == 'consistent' else 2
        return result, code
    if args.command == 'pack-receipt':
        from .replay_receipt import export_replay_receipt
        from .replay_verification import replay_verification_exit_code

        result = export_replay_receipt(args.source, args.result, args.output, control=control)
        return result, replay_verification_exit_code(result['replay_verification'])
    if args.command == 'compare':
        from .project_history import compare_project_reports
        from .persistence import save_json
        result = compare_project_reports(args.earlier, args.later)
        if args.output:
            path = args.output.expanduser().resolve()
            if path.exists() or any(path.is_relative_to(Path(result[key]['report_dir'])) for key in ('before', 'after')):
                raise ValueError('请选择来源报告目录之外的新文件保存对比摘要。')
            path.parent.mkdir(parents=True, exist_ok=True)
            save_json(path, result)
            result['comparison_file'] = str(path)
        return result, 0
    if args.command == 'pack-series':
        from .project_series import pack_series
        from .project_history import read_report_snapshot

        locations = {}
        for path in args.source_directory:
            report = read_report_snapshot(path)
            locations[report['_report_sha256']] = report['report_dir']
        return pack_series(args.report, args.output, part_mib=args.part_mib, locations=locations, control=control), 0
    if args.command in ('check-series', 'unpack-series'):
        from .project_series import receive_series

        return receive_series(args.directory, output=getattr(args, 'output', None), control=control), 0
    if args.command == "pack":
        if args.source_directory and not args.include_sources:
            raise ValueError('--source-directory 需要与 --include-sources 一起使用。')
        return project_sessions.pack_project_report(args.report, args.output, include_sources=args.include_sources,
            locations=_source_locations(args.source_directory), control=control), 0
    if args.command == "unpack":
        return project_sessions.unpack_project_report(args.bundle, args.destination, control=control), 0
    if args.command == 'stability':
        from .replay_stability import replay_stability, stability_exit_code

        if not args.confirm_run:
            raise _UsageError('稳定性检查需要 --confirm-run。可先用 project replay 预览固定范围。')
        result = replay_stability(args.report, args.output, rounds=args.rounds, confirmed=True, control=control,
                                  python_executable=args.python, timeout=args.timeout, go=args.go,
                                  node=args.node, npm=args.npm, offline=args.offline, npm_cache=args.npm_cache)
        return result, stability_exit_code(result)
    if args.command == 'stability-history':
        from .stability_history import list_stability_history

        return list_stability_history(args.report, args.output_root, control=control), 0
    if args.command == 'check-stability':
        from .replay_stability import stability_exit_code
        from .stability_history import check_saved_stability

        result = check_saved_stability(args.report, args.summary, control=control)
        return result, stability_exit_code(result)
    if args.command == 'test-source-control':
        from .project_test_source import run_test_source_control

        result, code = run_test_source_control(args.report.expanduser(), args.output,
                                               python_executable=args.python, timeout=args.timeout,
                                               confirmed=args.confirm_run, control=control)
        return (_report_with_scope(result) if args.confirm_run else result), code
    if args.command == 'check-test-source-control':
        from .project_test_source import check_test_source_control

        return check_test_source_control(args.source, args.result, control=control)
    if args.command == 'input-control':
        from .project_input_control import run_input_control

        result, code = run_input_control(args.report.expanduser(), args.output, requested=args.test,
                                         python_executable=args.python, timeout=args.timeout,
                                         confirmed=args.confirm_run, control=control)
        return (_report_with_scope(result) if args.confirm_run else result), code
    if args.command == 'check-input-control':
        from .project_input_control import check_input_control

        return check_input_control(args.source, args.result, control=control)
    if args.command == 'version-order':
        from .project_version_order import version_order_experiment

        return version_order_experiment(args.report, args.output, python_executable=args.python,
                                        timeout=args.timeout, confirmed=args.confirm_run, control=control)
    if args.command == 'check-version-order':
        from .project_version_order import check_version_order

        result = check_version_order(args.report, ab=args.ab, ba=args.ba, markdown=args.markdown, control=control)
        return result, 0 if result['status'] == 'completed' and result.get('markdown_status') != 'different' else 2
    if args.command == 'fixture-control':
        from .replay_verification import open_replay_evidence

        location = args.report.expanduser()
        with open_replay_evidence(location, control=control) as source:
            result = project_sessions.replay_project_report(
                source, args.output, python_executable=args.python, timeout=args.timeout,
                confirmed=args.confirm_run, fixture_files=args.file, control=control,
            )
            if not args.confirm_run:
                result['kind'] = 'project_fixture_control_preview'
                if source != location:
                    del result['report_dir']
                    result['source_archive'] = str(location.absolute())
                return result, 0
        completed = (result['status'] != 'cancelled' and not result['counts'].get('inconclusive')
                     and all(result[side]['status'] == 'completed' for side in ('before', 'after')))
        result['fixture_control_status'] = 'completed' if completed else 'blocked'
        return _report_with_scope(result), 0 if completed else 2
    if args.command == 'order-control':
        from .project_order import order_control_completed
        from .replay_verification import open_replay_evidence

        location = args.report.expanduser()
        with open_replay_evidence(location, control=control) as source:
            result = project_sessions.replay_project_report(
                source, args.output, python_executable=args.python, timeout=args.timeout,
                confirmed=args.confirm_run, order_mode=args.order, control=control,
            )
            if not args.confirm_run:
                result['kind'] = 'project_order_control_preview'
                result['notice'] = '已核对可采用的对照顺序。明确启动后将另存一次运行。'
                if source != location:
                    del result['report_dir']
                    result['source_archive'] = str(location.absolute())
                return result, 0
        completed = order_control_completed(result)
        result['order_control_status'] = 'completed' if completed else 'blocked'
        return _report_with_scope(result), 0 if completed else 2

    options = dict(python_executable=args.python, timeout=args.timeout, confirmed=args.confirm_run,
                   control=control, node=args.node, npm=args.npm, offline=args.offline,
                   npm_cache=args.npm_cache, go=args.go, check_environment=args.check_environment)
    from .replay_receipt import replay_receipt_destination, save_replay_receipt
    from .replay_verification import open_replay_evidence, replay_verification_exit_code

    verify_result = args.verify_result or args.receipt_dir is not None
    if verify_result and not args.confirm_run:
        raise _UsageError('--verify-result 和 --receipt-dir 均需要与 --confirm-run 一起使用。')
    location = args.report.expanduser()
    receipt_dir = None
    with open_replay_evidence(location, control=control) as source:
        if args.receipt_dir is not None:
            receipt_dir = replay_receipt_destination(args.receipt_dir, source, args.output)
        result = project_sessions.replay_project_report(source, args.output, verify_result=verify_result, **options)
        if not args.confirm_run and source != location:
            del result['report_dir']
            result['source_archive'] = str(location.absolute())
    if verify_result:
        verification = result['replay_verification']
        if receipt_dir is not None and verification['status'] != 'unavailable':
            try:
                save_replay_receipt(result, receipt_dir, control=control)
            except (OSError, ValueError) as exc:
                result['receipt_error'] = {'type': type(exc).__name__, 'message': str(exc)}
                return result, 2
            result['receipt_dir'] = str(receipt_dir)
        return result, replay_verification_exit_code(verification)
    if args.confirm_run:
        result = _report_with_scope(result)
        return result, _run_exit_code(result)
    checked = result.get('environment_check')
    return result, 2 if checked and checked['status'] == 'blocked' else 0


def _source_locations(directories):
    from .project_history import read_report_snapshot
    from .processing_history import MAX_ROUNDS

    if len(directories) > MAX_ROUNDS:
        raise ValueError(f'一次最多提供 {MAX_ROUNDS} 个来源目录。')
    locations = {}
    for directory in directories:
        snapshot = read_report_snapshot(directory)
        locations[snapshot['_report_sha256']] = snapshot['report_dir']
    return locations


def _report_with_scope(report):
    from .project_comparison import comparison_scope, comparison_verdict

    scope = comparison_scope(report)
    if scope:
        return {**report, 'comparison_scope': scope, 'comparison_verdict': comparison_verdict(report, scope)}
    return report


def _run_exit_code(report):
    status = report.get('comparison_verdict', report.get('status'))
    return {"regression_passed": 0, "regression_detected": 1}.get(status, 2)


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"无法输出为 JSON 的类型：{type(value).__name__}")


def _emit(result, *, stream=None):
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default),
          file=stream or sys.stdout)


def main(argv=None, *, _network_isolation=None) -> int:
    """Run arguments after ``reprolab project`` and return a shell exit code."""
    args = None
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        args = _parser().parse_args(arguments)
        if args.command == 'replay' and args.windows_container is not None and not args.network_isolated:
            raise _UsageError('--windows-container 需要 --network-isolated。')
        if args.command == 'replay' and args.network_isolated:
            if not args.confirm_run or args.offline is False:
                raise _UsageError('--network-isolated 需要 --confirm-run，不能同时允许在线安装。')
            if _network_isolation is None:
                if args.windows_container is not None:
                    from .project_windows_network import run_container
                    return run_container(args.windows_container, arguments)
                from .project_network import run_isolated

                return run_isolated(arguments)
        result, code = _dispatch(args, network_isolation=_network_isolation)
        if isinstance(result, str):
            sys.stdout.write(result)
        else:
            _emit(result)
        return code
    except SystemExit as exc:
        # argparse uses SystemExit for --help; keep main callable by the root CLI.
        return int(exc.code or 0)
    except (Exception, KeyboardInterrupt) as exc:
        command = getattr(args, "command", None)
        cancelled = isinstance(exc, KeyboardInterrupt)
        error = {
            "status": "cancelled" if cancelled else "error",
            "command": command,
            "error": {
                "type": "argument_error" if isinstance(exc, _UsageError) else type(exc).__name__,
                "message": "用户中断了当前命令。" if cancelled else str(exc),
            },
        }
        if command == "review" and hasattr(args, "directory"):
            error["project_directory"] = args.directory
        _emit(error, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
