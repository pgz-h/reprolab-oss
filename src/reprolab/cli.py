"""A small guided workflow around local regression verification."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .doctor import diagnose
from .preflight import check_case
from .retry import plan_retry, run_retry
from .git_source import run_git_case
from .recipes import create_recipe, list_project_tests, run_saved_case
from .workspace import WORKTREE, create_workspace_case
from .discovery import collect_project_tests, list_workspace_tests
from .failure_probe import probe_failure, save_probe_case
from .test_selection import recommend_tests
from .report import write_report
from .runner import run_case
from .batch import list_cases, run_batch
from .drafts import prepare_draft, generate_draft, recover_draft
from .draft_management import list_drafts, draft_status, verify_draft
from .evidence import check_report
from .evidence_bundle import pack_report, unpack_report


def _output_options(command):
    command.add_argument("--output", type=Path, default=Path("outputs"), help="报告保存目录")
    command.add_argument("--timeout", type=float, default=15, help="每版最长秒数，默认 15")
    command.add_argument("--python", help="项目环境中的 Python 解释器路径")
    command.add_argument("--json", action="store_true", help="输出 JSON")


def _ask(label, default=None):
    suffix = f" [{default}]" if default else ""
    print(f"{label}{suffix}：", end="", file=sys.stderr, flush=True)
    try:
        value = input().strip()
    except EOFError:
        raise ValueError("输入已结束；可以重新运行 init 或直接传入完整参数。") from None
    return value or default


def _initialize(args):
    if args.case.exists() or args.case.is_symlink():
        raise ValueError(f"案例文件已经存在，未覆盖：{args.case}")
    required = ("repository", "before", "project_test", "failure_type", "failure_message")
    missing = [key for key in required if not getattr(args, key) and not (key == "before" and args.working_tree)]
    interactive = args.interactive or (bool(missing) and sys.stdin.isatty())
    if missing and not interactive:
        raise ValueError("缺少参数：" + ", ".join("--" + k.replace("_", "-") for k in missing)
                         + "；需要逐步填写时使用 --interactive。")
    repository = args.repository or _ask("本地 Git 项目文件夹")
    if not repository:
        raise ValueError("项目目录不能为空。")
    before = args.before or ("HEAD" if args.working_tree else _ask("修复前版本", "HEAD~1"))
    after = WORKTREE if args.working_tree else args.after or (_ask("修复后版本", "HEAD") if interactive else "HEAD")
    target = args.project_test
    if not target:
        discovered = list_workspace_tests(repository) if args.working_tree else list_project_tests(repository, after)
        offered = discovered["tests"][:20]
        print("发现的测试（仅静态读取，尚未执行）：", file=sys.stderr)
        for index, name in enumerate(offered, 1):
            print(f"  {index}. {name}", file=sys.stderr)
        print(discovered["notice"], file=sys.stderr)
        answer = _ask("选择编号或输入完整测试目标")
        if answer and answer.isdigit() and 1 <= int(answer) <= len(offered):
            target = offered[int(answer) - 1]
        else:
            target = answer
    python = args.python or (_ask("Python 解释器", sys.executable) if interactive else None)
    failure_type = args.failure_type or _ask("旧版应出现的异常类型", "AssertionError")
    failure_message = args.failure_message or _ask("旧版异常消息应包含的文本")
    title = args.title or (_ask("案例名称", "项目修复验收") if interactive else None)
    if args.working_tree:
        return create_workspace_case(args.case, repository, before, target, failure_type=failure_type,
                                     failure_message=failure_message, title=title, python_executable=python)
    return create_recipe(args.case, repository, before, after, target,
                         failure_type=failure_type, failure_message=failure_message,
                         title=title, python_executable=python)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'project':
        from .project_cli import main as project_main
        return project_main(arguments[1:])
    parser = argparse.ArgumentParser(description="ReproLab：用同一份测试验收修复前后的代码。")
    parser.add_argument("--version", action="version", version=f"ReproLab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser('project', help='项目工作台：批量测试回归、结果矩阵、证据导出与重放')
    for name, help_text in (("demo", "运行教学示例"), ("history", "运行真实历史案例"),
                            ("verify", "运行已保存的案例")):
        command = sub.add_parser(name, help=help_text)
        if name == "verify":
            command.add_argument("case", type=Path)
        _output_options(command)
    command = sub.add_parser("verify-git", help="直接比较 Git 仓库的两个版本")
    command.add_argument("repository", type=Path)
    command.add_argument("--before", required=True)
    command.add_argument("--after", default="HEAD")
    tests = command.add_mutually_exclusive_group(required=True)
    tests.add_argument("--test", type=Path, help="独立回归测试文件")
    tests.add_argument("--project-test", help="项目内相对路径::测试名称")
    command.add_argument("--failure-type", required=True)
    command.add_argument("--failure-message", required=True)
    command.add_argument("--title")
    _output_options(command)
    command = sub.add_parser("probe-failure", help="固定输入并试运行旧版，观察真实故障")
    command.add_argument("repository", type=Path)
    command.add_argument("--before", required=True)
    revisions = command.add_mutually_exclusive_group()
    revisions.add_argument("--after", default="HEAD")
    revisions.add_argument("--working-tree", action="store_true")
    command.add_argument("--project-test", required=True)
    _output_options(command)
    command = sub.add_parser("case-from-probe", help="确认故障后从试运行的固定输入创建案例")
    command.add_argument("directory", type=Path)
    command.add_argument("case", type=Path)
    command.add_argument("--confirm-failure", action="store_true")
    command.add_argument("--failure-message")
    command.add_argument("--title")
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("list-tests", help="列出 Git 版本中的现有测试，不执行项目")
    command.add_argument("repository", type=Path)
    command.add_argument("--ref", default="HEAD")
    command.add_argument("--before", help="按与此基线的文件改动排序测试")
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("collect-tests", help="在独立副本中加载可信项目，列出实际测试和参数")
    command.add_argument("repository", type=Path)
    selection = command.add_mutually_exclusive_group()
    selection.add_argument("--ref", default="HEAD")
    selection.add_argument("--working-tree", action="store_true")
    command.add_argument("--target", help="缩小到测试文件或函数")
    command.add_argument("--python")
    command.add_argument("--timeout", type=float, default=15)
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("init", help="引导创建可复用案例")
    command.add_argument("case", type=Path, help="新案例 JSON 的保存位置")
    command.add_argument("--repository", type=Path)
    command.add_argument("--before")
    versions = command.add_mutually_exclusive_group()
    versions.add_argument("--after")
    versions.add_argument("--working-tree", action="store_true", help="冻结当前未提交修改；默认基线HEAD")
    command.add_argument("--project-test")
    command.add_argument("--python")
    command.add_argument("--failure-type")
    command.add_argument("--failure-message")
    command.add_argument("--title")
    command.add_argument("--interactive", action="store_true", help="逐步输入并选择测试")
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("cases", help="查看已保存的案例，不执行测试")
    command.add_argument("directory", type=Path)
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("batch", help="顺序运行多个案例并生成总览")
    command.add_argument("directory", type=Path)
    _output_options(command)
    command = sub.add_parser("draft", help="准备上下文、调用 AI 起草单个回归测试")
    draft_commands = command.add_subparsers(dest="draft_command", required=True)
    prepare = draft_commands.add_parser("prepare", help="离线准备问题和已提交代码上下文")
    prepare.add_argument("repository", type=Path)
    prepare.add_argument("--before", required=True)
    prepare.add_argument("--after", default="HEAD")
    problem = prepare.add_mutually_exclusive_group(required=True)
    problem.add_argument("--issue")
    problem.add_argument("--issue-file", type=Path)
    prepare.add_argument("--context", action="append", help="相关文件的仓库内路径，可重复")
    prepare.add_argument("--drafts", type=Path, required=True, help="独立草稿根目录，例如 work/reprolab/drafts")
    prepare.add_argument("--python", help="验收用 Python 环境")
    prepare.add_argument("--json", action="store_true")
    for name in ("generate", "import"):
        draft = draft_commands.add_parser(name, help="调用配置的模型 API 生成草稿" if name == "generate" else "导入外部模型的 JSON 响应")
        draft.add_argument("directory", type=Path)
        draft.add_argument("--model", required=(name == "import"))
        draft.add_argument("--json", action="store_true")
        if name == "generate":
            draft.add_argument("--config", type=Path, help="本机模型配置 JSON，不应放入源码仓库")
            draft.add_argument("--base-url", help="所选协议对应的模型服务基地址")
            draft.add_argument("--protocol", choices=("openai", "responses", "anthropic"), help="API 协议，默认读取配置或使用 openai")
            draft.add_argument("--api-key-env", help="保存 Key 的环境变量名，默认 REPROLAB_API_KEY")
            draft.add_argument("--ai-timeout", type=float, default=180)
        else:
            draft.add_argument("--response", type=Path, required=True)
    for name, help_text in (("list", "列出草稿，不执行代码"), ("status", "查看草稿与最近验收状态"),
                            ("recover", "复用已保存响应，不调用模型"), ("verify", "审阅后直接验收草稿")):
        draft = draft_commands.add_parser(name, help=help_text)
        draft.add_argument("directory", type=Path)
        if name == "verify":
            _output_options(draft)
        else:
            draft.add_argument("--json", action="store_true")
    command = sub.add_parser("report", help="检查、打包和导入已有验收证据，不执行测试")
    report_commands = command.add_subparsers(dest="report_command", required=True)
    for name in ("check", "pack", "unpack"):
        action = report_commands.add_parser(name)
        action.add_argument("path", type=Path, help="报告目录或证据 ZIP")
        action.add_argument("--json", action="store_true")
        if name == "pack":
            action.add_argument("--output", type=Path, required=True, help="新 ZIP 文件，不能位于原报告目录内")
        elif name == "unpack":
            action.add_argument("destination", type=Path, help="新的导入目录，不覆盖已有目录")
    command = sub.add_parser("doctor", help="检查本机环境和可选模型配置，不运行测试或联网")
    command.add_argument("--python", help="要诊断的 Python 环境")
    command.add_argument("--require", action="append", default=[], help="检查发行包安装记录，可重复指定")
    command.add_argument("--config", type=Path, help="可选模型配置，仅检查格式，不连接模型服务")
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("preflight", help="检查案例、版本和环境，不执行测试")
    command.add_argument("case", type=Path)
    _output_options(command)
    command = sub.add_parser("retry", help="从旧批次计划重跑未通过项或继续未完成项")
    command.add_argument("summary", type=Path)
    command.add_argument("--mode", choices=("not_verified", "unfinished"), default="not_verified")
    command.add_argument("--run", action="store_true", help="明确执行新批次；省略时只显示计划")
    _output_options(command)
    command = sub.add_parser("suite", help="AI项目验收计划、分组执行与结果解读")
    suite_commands = command.add_subparsers(dest="suite_command", required=True)
    prepare = suite_commands.add_parser("prepare")
    prepare.add_argument("repository", type=Path)
    prepare.add_argument("--before", required=True)
    versions = prepare.add_mutually_exclusive_group()
    versions.add_argument("--after", default="HEAD")
    versions.add_argument("--working-tree", action="store_true")
    prepare.add_argument("--issue", required=True)
    prepare.add_argument("--output", required=True, type=Path)
    prepare.add_argument("--python")
    prepare.add_argument("--context", action="append")
    prepare.add_argument("--json", action="store_true")
    for name in ("generate", "import", "recover", "show", "export-plan", "revise", "probe", "probe-show", "run", "explain"):
        action = suite_commands.add_parser(name)
        action.add_argument("path", type=Path)
        action.add_argument("--json", action="store_true")
        if name == "generate":
            action.add_argument("--config", required=True, type=Path)
        if name == "import":
            action.add_argument("--response", required=True, type=Path)
            action.add_argument("--model", default="外部响应")
        if name in ("export-plan", "revise"):
            action.add_argument("--output", required=True, type=Path)
        if name == "revise":
            action.add_argument("--plan", required=True, type=Path, help="审阅修改后的完整计划 JSON")
        if name == "probe":
            action.add_argument("--check", required=True, help="本次只试运行一个检查的旧版")
            action.add_argument("--output", required=True, type=Path)
            action.add_argument("--python")
            action.add_argument("--timeout", type=float, default=15)
        if name == "probe-show":
            action.add_argument("--probe", required=True, type=Path)
        if name == "run":
            action.add_argument("--output", required=True, type=Path)
            action.add_argument("--python")
            action.add_argument("--timeout", type=float, default=15)
            action.add_argument("--check", action="append")
        if name == "explain":
            action.add_argument("--config", type=Path)
            action.add_argument("--send", action="store_true", help="对已准备的发送目录明确请求模型")
    command = sub.add_parser("environment", help="项目依赖检查与专用环境准备")
    environment_commands = command.add_subparsers(dest="environment_command", required=True)
    inspect = environment_commands.add_parser("inspect")
    inspect.add_argument("project", type=Path)
    inspect.add_argument("--python")
    inspect.add_argument("--json", action="store_true")
    plan = environment_commands.add_parser("plan")
    plan.add_argument("project", type=Path)
    plan.add_argument("destination", type=Path)
    plan.add_argument("--python", required=True)
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--json", action="store_true")
    for group_parser in (inspect, plan):
        group_parser.add_argument("--group", action="append", default=[], help="选择 dependency-groups 中的依赖组，可重复")
        group_parser.add_argument("--extra", action="append", default=[], help="选择 project.optional-dependencies 中的可选依赖组，可重复")
    create = environment_commands.add_parser("create")
    create.add_argument("plan", type=Path)
    create.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "suite":
            from .repair_suite import prepare_suite, generate_suite, recover_suite, read_suite, run_suite, prepare_explanation, explain_suite, _read
            from .suite_review import editable_plan, revise_suite
            if args.suite_command == "prepare":
                result = prepare_suite(args.repository, args.before, WORKTREE if args.working_tree else args.after,
                                       args.issue, args.output, python_executable=args.python, paths=args.context)
            elif args.suite_command == "generate":
                result = generate_suite(args.path, config_file=args.config)
            elif args.suite_command == "import":
                result = generate_suite(args.path, response_file=args.response, model=args.model)
            elif args.suite_command == "recover":
                result = recover_suite(args.path)
            elif args.suite_command == "export-plan":
                plan = editable_plan(args.path)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open('x', encoding='utf-8') as stream:
                    json.dump(plan, stream, ensure_ascii=False, indent=2)
                result = {'status': 'exported', 'plan': str(args.output.resolve()),
                          'notice': '已导出可编辑计划；修改后用 suite revise 另存独立版本。'}
            elif args.suite_command == "revise":
                result = revise_suite(args.path, _read(args.plan), args.output)
            elif args.suite_command in ("probe", "probe-show"):
                from .suite_probe import probe_suite_check, read_suite_probe
                result = (probe_suite_check(args.path, args.check, args.output, python_executable=args.python, timeout=args.timeout)
                          if args.suite_command == "probe" else read_suite_probe(args.path, args.probe))
                result['exit_code'] = 0 if result['status'] == 'failure_observed' else 2
            elif args.suite_command == "show":
                result = read_suite(args.path)
            elif args.suite_command == "run":
                result = run_suite(args.path, args.output, confirmed=True, selected_ids=args.check,
                                   python_executable=args.python, timeout=args.timeout)
            else:
                result = explain_suite(args.path, config_file=args.config) if args.send else prepare_explanation(args.path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result.get("exit_code", 0)
        if args.command == "environment":
            from .project_environment import inspect_project, plan_environment, prepare_environment
            if args.environment_command == "inspect":
                result = inspect_project(args.project, args.python,
                    selected_groups=['group:' + name for name in args.group] + ['extra:' + name for name in args.extra])
            elif args.environment_command == "plan":
                result = plan_environment(args.project, args.destination, args.python,
                    selected_groups=['group:' + name for name in args.group] + ['extra:' + name for name in args.extra])
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
            else:
                with args.plan.open(encoding="utf-8") as stream:
                    plan = json.load(stream)
                result = prepare_environment(plan)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("status") not in ("attention", "review") else 2
        if args.command == "probe-failure":
            result = probe_failure(args.repository, args.before, WORKTREE if args.working_tree else args.after,
                                   args.project_test, args.output, python_executable=args.python, timeout=args.timeout)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result["explanation"])
                if result.get("suggestion"):
                    print(result["suggestion"]["type"])
                    print(result["suggestion"]["message_contains"])
                print(f"试运行材料 {result['probe_dir']}")
            return 0 if result["status"] == "failure_observed" else 2
        if args.command == "case-from-probe":
            result = save_probe_case(args.directory, args.case, confirmed=args.confirm_failure,
                                     failure_message=args.failure_message, title=args.title)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result["notice"])
                print(result["case"])
            return 0
        if args.command == "preflight":
            result = check_case(args.case, args.output, timeout=args.timeout, python_executable=args.python)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("预检通过，尚未执行测试。" if result["status"] == "ready" else "预检有问题，尚未执行测试。")
                for check in result["checks"]:
                    print(f"[{check['status']}] {check['message']}")
                    if check.get("suggestion"): print("  " + check["suggestion"])
                print(result["notice"])
            return 0 if result["status"] == "ready" else 2
        if args.command == "retry":
            plan = plan_retry(args.summary, mode=args.mode)
            result = run_retry(plan, args.output, timeout=args.timeout, python_executable=args.python) if args.run else plan
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.run:
                print(f"本批次已完成 {result['completed']}/{result['total']}；状态：{result['status']}")
                print(str(Path(result["summary_dir"]) / "summary.html"))
            else:
                print(f"计划选择 {len(plan['paths'])} 个案例，尚未执行。")
                for entry in plan["entries"]: print(f"- {entry['title']}：{entry['status']}")
                print("明确执行时添加 --run，并选择新报告目录。")
            return result["exit_code"] if args.run else 0
        if args.command == "doctor":
            result = diagnose(python_executable=args.python, requirements=args.require, config_file=args.config)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("本地检查通过" if result["status"] == "ready" else "有项目需要处理")
                labels = {"ok": "可用", "error": "需处理", "warning": "提示", "not_checked": "未检查"}
                for item in result["checks"]:
                    print(f"[{labels[item['status']]}] {item['message']}")
                    if item.get("suggestion"): print("  " + item["suggestion"])
                print(result["notice"])
            return 0 if result["status"] == "ready" else 2
        if args.command == "report":
            if args.report_command == "check":
                result = check_report(args.path)
            elif args.report_command == "pack":
                result = pack_report(args.path, args.output)
            else:
                result = unpack_report(args.path, args.destination)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                labels = {"consistent": "证据一致", "inconsistent": "证据不一致", "packed": "证据包已保存", "imported": "证据已导入"}
                print(labels[result["status"]])
                print(result["notice"])
                for issue in result.get("issues", []):
                    print("  " + issue)
                for key in ("archive", "report", "case"):
                    if result.get(key): print(result[key])
            return 2 if result["status"] == "inconsistent" else 0
        if args.command == "draft":
            if args.draft_command in ("list", "status"):
                result = list_drafts(args.directory) if args.draft_command == "list" else draft_status(args.directory)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    entries = result["drafts"] if args.draft_command == "list" else [result]
                    labels = {"prepared": "已准备", "generation_failed": "生成失败", "draft_generated": "草稿已生成",
                              "invalid": "记录损坏", "verified": "验证通过", "not_reproduced": "未能复现",
                              "still_failing": "修复后仍失败", "inconclusive": "无法验收", "stale": "历史结果，文件已修改"}
                    if args.draft_command == "list":
                        print(f"找到 {result['count']} 份草稿")
                    for entry in entries:
                        print(f"{Path(entry['draft_dir']).name}：{labels.get(entry['generation_status'], entry['generation_status'])}")
                        if entry.get("title"):
                            print("  " + entry["title"])
                        if entry.get("recoverable"):
                            print("  有已保存的响应，可用 draft recover 离线恢复。")
                        if entry.get("verification"):
                            verification = entry["verification"]
                            print(f"  最近验收：{labels.get(verification['status'], verification['status'])}")
                            print("  " + verification.get("notice", verification.get("error", "")))
                            if verification.get("report"):
                                print(f"  报告：{verification['report']}")
                        if entry.get("error"):
                            print("  " + entry["error"])
                        if args.draft_command == "status":
                            if entry.get("recovery_error"):
                                print("  恢复说明：" + entry["recovery_error"])
                            print("  下一步：" + entry["next_action"])
                    print(result["notice"])
                return 0
            if args.draft_command == "verify":
                result = verify_draft(args.directory, args.output, timeout=args.timeout, python_executable=args.python)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(result["explanation"])
                    if result.get("report_dir"):
                        print(f"中文报告：{Path(result['report_dir']) / 'report.html'}")
                return {"verified": 0, "regression_passed": 0, "improved": 0, "regression_detected": 1, "baseline_failure": 1, "not_reproduced": 1, "still_failing": 1, "inconclusive": 2}[result["status"]]
            if args.draft_command == "prepare":
                result = prepare_draft(args.repository, args.before, args.after, issue=args.issue,
                                       issue_file=args.issue_file, paths=args.context, drafts_root=args.drafts,
                                       python_executable=args.python)
            elif args.draft_command == "recover":
                result = recover_draft(args.directory)
            elif args.draft_command == "generate":
                result = generate_draft(args.directory, config_file=args.config, base_url=args.base_url,
                                        api_key_env=args.api_key_env, model=args.model, timeout=args.ai_timeout, protocol=args.protocol)
            else:
                result = generate_draft(args.directory, response_file=args.response, model=args.model)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result["notice"])
                print(f"草稿目录：{result['draft_dir']}")
                for note in result.get("notes", []):
                    print(note)
                if result.get("case"):
                    print(f"验收案例：{result['case']}")
                else:
                    print("查看 prompt.txt 后，用 draft generate 加草稿目录调用模型。")
            return 0
        if args.command == "cases":
            result = list_cases(args.directory)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"找到 {result['count']} 个案例配置")
                for index, item in enumerate(result["cases"], 1):
                    state = "可读取" if item["readable"] else "配置错误"
                    print(f"{index}. [{state}] {item['title']} — {Path(item['file']).name}")
                    if item["error"]:
                        print(f"   {item['error']}")
                print(result["notice"])
            return 0
        if args.command == "batch":
            def progress(index, total, entry):
                if not args.json:
                    print(f"[{index}/{total}] {entry['title']}：{entry['status']}", file=sys.stderr)
            result = run_batch(args.directory, args.output, timeout=args.timeout,
                               python_executable=args.python, progress=progress)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"已处理 {result['completed']}/{result['total']} 个案例")
                print(f"总览报告：{Path(result['summary_dir']) / 'summary.html'}")
            return result["exit_code"]
        if args.command == "collect-tests":
            result = collect_project_tests(args.repository, WORKTREE if args.working_tree else args.ref,
                                           target=args.target, python_executable=args.python, timeout=args.timeout)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result["notice"])
                print(result["explanation"])
                for target in result["tests"]:
                    print(target)
                if result["status"] == "collection_failed":
                    print(result["log_excerpt"], file=sys.stderr)
            return 0 if result["status"] == "collected" else 2
        if args.command == "list-tests":
            result = list_project_tests(args.repository, args.ref)
            if args.before:
                result.update(recommend_tests(args.repository, args.before, args.ref, result["tests"]))
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{result['repository']} · {result['commit'][:12]}")
                for index, target in enumerate(result["tests"], 1):
                    print(f"{index}. {target}")
                print(result["notice"])
                for note in result["notes"]:
                    print(note)
            return 0
        if args.command == "init":
            result = _initialize(args)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"案例已保存：{result['case']}")
                if result.get("workspace_origin"):
                    print(f"固定基线：{result['before_commit'][:12]} → 当前工作区快照")
                else:
                    print(f"固定版本：{result['before_commit'][:12]} → {result['after_commit'][:12]}")
                print("用 verify 命令和这个案例文件运行验收。")
            return 0
        if args.command == "verify-git":
            result = run_git_case(args.repository, args.before, args.after, args.test, args.output,
                                  failure_type=args.failure_type, failure_message=args.failure_message,
                                  title=args.title, timeout=args.timeout, test_target=args.project_test,
                                  python_executable=args.python)
        elif args.command == "verify":
            result = run_saved_case(args.case, args.output, timeout=args.timeout, python_executable=args.python)
        else:
            relative = "demo_case/case.json" if args.command == "demo" else "history_cases/humanize250/case.json"
            result = run_case(Path(__file__).parent / relative, args.output,
                              timeout=args.timeout, python_executable=args.python)
        write_report(result)
    except (ValueError, OSError, UnicodeError) as exc:
        if args.json:
            print(json.dumps({"status": "inconclusive", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"无法完成操作：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"ReproLab · {result['title']}")
        print(result["explanation"])
        if result.get("input_integrity_notice"):
            print(result["input_integrity_notice"])
        print(f"旧版：{result['before']['kind']}  新版：{result['after']['kind']}")
        print(f"中文报告：{Path(result['report_dir']) / 'report.html'}")
    return {"verified": 0, "regression_passed": 0, "improved": 0, "regression_detected": 1, "baseline_failure": 1, "not_reproduced": 1, "still_failing": 1, "inconclusive": 2}[result["status"]]
