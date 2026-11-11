"""Command-line entry point for saved, multi-test project regression sessions.

Keep engine imports inside dispatch: help and argument errors must also work in
the desktop distribution without importing any GUI modules.
"""

import argparse
import json
import math
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


def _runtime_options(parser, *, inherit_timeout=False):
    parser.add_argument("--progress", action="store_true", help="在标准错误输出显示实时测试进度，标准输出保持 JSON")
    parser.add_argument("--python", help="项目环境中的 Python 解释器路径")
    default_help = "默认沿用来源报告（无记录时为 120）" if inherit_timeout else "默认 120"
    parser.add_argument("--timeout", type=_timeout, default=None if inherit_timeout else 120,
                        help=f"每版全部所选测试的总超时秒数，{default_help}，范围 0.1–3600")


def _selection_options(parser):
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--test', action='append', help='完整测试 ID，可重复')
    selection.add_argument('--selection-file', type=Path, help='JSON 或逐行文本测试选择清单')


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


def _parser():
    parser = _Parser(
        prog="reprolab project",
        description="冻结项目版本、发现并选择测试、比较前后结果，保存可核对的项目回归报告。",
        epilog="review、collect、run、recheck 和明确确认的 replay 会加载并运行可信项目代码；独立进程不是安全沙箱。",
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    prepare = commands.add_parser("prepare", help="冻结两个版本或当前工作区，创建项目会话")
    review = commands.add_parser("review", help="一次冻结、发现并运行 pytest 项目对比")
    for command in (prepare, review):
        command.add_argument("repository", type=Path, help="本地 Git 仓库目录")
        command.add_argument("--before", required=True, help="基线 Git 提交或引用")
        revisions = command.add_mutually_exclusive_group(required=True)
        revisions.add_argument("--after", help="对比 Git 提交或引用")
        revisions.add_argument("--working-tree", action="store_true", help="冻结当前工作区作为对比版本")
        command.add_argument("--title", help="项目会话名称")
        command.add_argument("--issue", default="", help="本次改动的问题与检查说明")
        command.add_argument("--path", default="", help="发现范围，项目内的相对测试文件或目录")
    prepare.add_argument("--output", type=Path, required=True, help="新项目会话的保存根目录")
    prepare.add_argument("--python", help="项目环境中的 Python 解释器路径")
    review.add_argument("--output", type=Path, required=True, help="项目外的输出根目录，分别保存 sessions 和 reports")
    _selection_options(review)
    _runtime_options(review)
    review.epilog = "省略测试选择时仅运行完整发现清单（最多 2000 项）。仅对可信项目发起执行。"

    vitest = commands.add_parser('vitest', help='对比 Vitest 4 项目中所选测试文件的两版结果')
    vitest.add_argument('repository', type=Path, help='本地 Git 仓库目录')
    vitest.add_argument('--before', required=True, help='基线 Git 提交或引用')
    candidate = vitest.add_mutually_exclusive_group(required=True)
    candidate.add_argument('--after', help='对比 Git 提交或引用')
    candidate.add_argument('--working-tree', action='store_true', help='以当前工作区作为对比版本')
    vitest.add_argument('--file', action='append', required=True, help='项目内的相对测试文件，可重复')
    vitest.add_argument('--output', type=Path, required=True, help='报告保存根目录')
    vitest.add_argument('--timeout', type=_timeout, default=120, help='每版安装依赖和测试的总时限，默认 120 秒')
    _npm_options(vitest)

    go = commands.add_parser('go', help='比较单个 Go 包中所选测试的两版结果')
    go.add_argument('repository', type=Path, help='本地 Git 仓库目录')
    go.add_argument('--before', required=True, help='基线 Git 提交或引用')
    candidate = go.add_mutually_exclusive_group(required=True)
    candidate.add_argument('--after', help='对比 Git 提交或引用')
    candidate.add_argument('--working-tree', action='store_true', help='以当前工作区作为对比版本')
    go.add_argument('--package', default='.', help='仓库内包目录，默认根目录')
    go.add_argument('--test', action='append', required=True, help='顶层 Go 测试函数名称，可重复')
    go.add_argument('--go', help='Go 可执行文件路径，默认使用 PATH')
    go.add_argument('--output', type=Path, required=True, help='报告保存根目录')
    go.add_argument('--timeout', type=_timeout, default=120, help='每版编译和测试总时限，默认 120 秒')

    show = commands.add_parser("show", help="读取已保存项目会话，不执行项目代码")
    show.add_argument("directory", type=Path, help="项目会话目录")

    refresh = commands.add_parser('refresh', help='沿用固定基线，另存当前工作区为新会话，不发现或运行测试')
    refresh.add_argument('directory', type=Path, help='来源项目会话目录；原会话与报告保留')
    refresh.add_argument('--output', required=True, type=Path, help='新会话的保存根目录')
    refresh.add_argument('--python', help='新会话使用的 Python；省略时沿用来源环境')
    _selection_options(refresh)
    refresh.epilog = '明确提供清单可带入原选择；否则仅沿用会话已有的继承选择。原基线不随 HEAD 移动。'

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
    _selection_options(selection_check)
    selection_check.epilog = '省略清单时核对会话的继承选择；缺失项返回退出码 2。核对匹配不代表测试通过。'

    collect = commands.add_parser("collect", help="在固定快照中实际收集测试和参数化 node ID")
    collect.add_argument("directory", type=Path, help="项目会话目录")
    discovery_scope = collect.add_mutually_exclusive_group()
    discovery_scope.add_argument("--path", help="仅收集指定相对测试文件或目录；省略时沿用会话范围")
    discovery_scope.add_argument("--default-scope", dest="path", action="store_const", const="",
                                 help="恢复项目的 pytest 默认发现范围")
    _runtime_options(collect)

    run = commands.add_parser("run", help="明确执行所选测试，保存新的项目回归报告")
    run.add_argument("directory", type=Path, help="项目会话目录")
    run.add_argument("--output", type=Path, required=True, help="新运行报告的保存根目录")
    selection = run.add_mutually_exclusive_group()
    selection.add_argument("--test", action="append", help="完整 node ID，可重复；省略时先使用继承选择，否则使用已收集测试（最多 2000 个）")
    selection.add_argument('--selection-file', type=Path, help='从 JSON 或逐行文本清单读取要运行的测试，可使用桌面导出的清单')
    _runtime_options(run)

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

    check = commands.add_parser("check", help="离线检查已有项目报告及证据的一致性")
    check.add_argument("report", type=Path, help="项目回归报告目录")

    compare = commands.add_parser('compare', help='只读比较两次保存的运行，区分结果变化与选择范围差异')
    compare.add_argument('earlier', type=Path, help='前次报告目录')
    compare.add_argument('later', type=Path, help='后次报告目录')
    compare.add_argument('--output', type=Path, help='可选：写入新的对比 JSON 文件，不覆盖来源报告')

    pack = commands.add_parser("pack", help="检查并导出项目报告与固定输入 ZIP")
    pack.add_argument("report", type=Path, help="项目回归报告目录")
    pack.add_argument("--output", type=Path, required=True, help="新的 ZIP 文件路径")
    pack.add_argument("--include-sources", action="store_true", help="连同已绑定的复查来源一并导出")
    pack.add_argument("--source-directory", type=Path, action="append", default=[], help="已移动的来源目录，可重复")

    unpack = commands.add_parser("unpack", help="安全导入项目证据 ZIP，检查内容并恢复文件权限")
    unpack.add_argument("bundle", type=Path, help="项目报告证据 ZIP")
    unpack.add_argument("destination", type=Path, help="新的导入目录，不覆盖已有目录")

    replay = commands.add_parser("replay", help="默认预览已有报告的重放范围；明确确认后才运行")
    replay.add_argument("report", type=Path, help="项目回归报告目录")
    replay.add_argument("--output", type=Path, default=Path("outputs/project-runs"),
                        help="新重放报告的保存根目录，默认 outputs/project-runs")
    replay.add_argument("--confirm-run", action="store_true", help="明确同意加载可信证据代码并执行重放")
    _runtime_options(replay, inherit_timeout=True)
    _npm_options(replay, replay=True)
    replay.add_argument('--go', help='Go 报告重放使用的 Go 可执行文件')
    return parser


def _prepare(args, output):
    from .project_sessions import prepare_project
    from .workspace import WORKTREE

    return prepare_project(
        args.repository, args.before, WORKTREE if args.working_tree else args.after, output,
        python_executable=args.python, title=args.title, issue=args.issue, collection_path=args.path,
    )


def _dispatch(args):
    from . import project_sessions
    from .runner import RunControl
    from .project_progress import progress_text

    def progress(phase, payload):
        if phase == 'project_progress':
            text = progress_text(payload)
            if payload.get('current_test'):
                text += ' · ' + payload['current_test']
            print(text, file=sys.stderr, flush=True)
    control = RunControl(progress=progress if getattr(args, 'progress', False) else None)

    if args.command == 'vitest':
        from .vitest_project import run_vitest_project
        from .workspace import WORKTREE
        result = run_vitest_project(args.repository, args.before, WORKTREE if args.working_tree else args.after,
            args.file, args.output, node=args.node, npm=args.npm, timeout=args.timeout,
            offline=args.offline, npm_cache=args.npm_cache, control=control)
        return result, _run_exit_code(result)
    if args.command == 'go':
        from .go_project import run_go_project
        from .workspace import WORKTREE
        result = run_go_project(args.repository, args.before, WORKTREE if args.working_tree else args.after,
                                args.output, package=args.package, selected=args.test, go=args.go,
                                timeout=args.timeout, control=control)
        return result, _run_exit_code(result)
    if args.command == "prepare":
        return _prepare(args, args.output), 0
    if args.command == "show":
        return project_sessions.read_project(args.directory), 0
    if args.command in ('refresh', 'selection-check'):
        from .project_refresh import refresh_project, review_project_selection
        from .project_selections import read_selection
        selected = read_selection(args.selection_file, allow_empty=True) if args.selection_file else args.test
        if args.command == 'refresh':
            return refresh_project(args.directory, args.output, selected_ids=selected,
                                   python_executable=args.python), 0
        result = review_project_selection(args.directory, selected_ids=selected)
        return result, 0 if result['ready'] else 2
    if args.command == 'recheck':
        from .project_refresh import recheck_and_run
        result = recheck_and_run(args.report, args.sessions, args.output, scope=args.scope,
                                python_executable=args.python, timeout=args.timeout, selected_ids=args.test, control=control)
        if result.get('report'):
            from .project_history import read_recheck_progress
            result['review_progress'] = read_recheck_progress(result['report']['report_dir'], control=control)
        return result, _run_exit_code(result['report']) if result.get('report') else 2
    if args.command == "collect":
        result = project_sessions.collect_project(
            args.directory, python_executable=args.python, timeout=args.timeout, control=control,
            collection_path=args.path,
        )
        return result, 0 if result.get("status") == "collected" and not (result.get("partial") or result.get("truncated")) else 2
    if args.command in ("run", "review"):
        from .project_selections import read_selection
        selected = read_selection(args.selection_file) if args.selection_file else args.test
        output = args.output
        if args.command == "review":
            prepared = _prepare(args, output / "sessions")
            args.directory = prepared["directory"]
            output /= "reports"
        result = project_sessions.run_project(
            args.directory, output, selected_ids=selected, python_executable=args.python,
            timeout=args.timeout, confirmed=True, control=control,
        )
        return result, _run_exit_code(result)
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
    if args.command == "pack":
        if args.source_directory and not args.include_sources:
            raise ValueError('--source-directory 需要与 --include-sources 一起使用。')
        return project_sessions.pack_project_report(args.report, args.output, include_sources=args.include_sources,
            locations=_source_locations(args.source_directory), control=control), 0
    if args.command == "unpack":
        return project_sessions.unpack_project_report(args.bundle, args.destination, control=control), 0
    result = project_sessions.replay_project_report(
        args.report, args.output, python_executable=args.python, timeout=args.timeout,
        confirmed=args.confirm_run, control=control, node=args.node, npm=args.npm,
        offline=args.offline, npm_cache=args.npm_cache, go=args.go,
    )
    return result, _run_exit_code(result) if args.confirm_run else 0


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


def _run_exit_code(report):
    return {"regression_passed": 0, "regression_detected": 1}.get(report.get("status"), 2)


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"无法输出为 JSON 的类型：{type(value).__name__}")


def _emit(result, *, stream=None):
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default),
          file=stream or sys.stdout)


def main(argv=None) -> int:
    """Run arguments after ``reprolab project`` and return a shell exit code."""
    args = None
    try:
        args = _parser().parse_args(argv)
        result, code = _dispatch(args)
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
