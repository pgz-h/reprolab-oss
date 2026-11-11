"""Local readiness diagnostics; never runs project tests or contacts a model."""
from datetime import datetime, timezone
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys

from . import __version__
from .ai import load_api_config
from .runtime import choose_runtime, child_environment


_DISTRIBUTIONS = """import importlib.metadata as metadata, json, sys
result = {}
for name in json.loads(sys.argv[1]):
    try:
        result[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        result[name] = None
print(json.dumps(result))
"""


def diagnose(*, python_executable=None, requirements=None, config_file=None):
    requirements = list(dict.fromkeys(requirements or []))
    if len(requirements) > 20 or any(not isinstance(name, str) or len(name) > 100 or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in requirements):
        raise ValueError("--require 只接受发行包名称（例如 PyYAML），最多 20 个；不接受路径或版本安装表达式。")
    checks, dependencies = [], {}
    runtime = None
    runtime_ok = False
    try:
        runtime = choose_runtime(python_executable)
        if any(not isinstance(runtime.get(name), str) or not runtime[name] for name in ("python_version", "pytest_version")):
            raise ValueError("无法识别 Python/pytest 版本元数据；请修复或重建所选环境。")
        supported = (tuple(int(p) for p in runtime["python_version"].split(".")[:2]) >= (3, 11)
                     and 8 <= int(runtime["pytest_version"].split(".")[0]) < 10)
        if not supported:
            raise ValueError("需要 Python >=3.11 和 pytest >=8,<10。")
        runtime_ok = True
        checks.append({"id": "runtime", "status": "ok", "required": True,
                       "message": f"Python {runtime['python_version']} / pytest {runtime['pytest_version']} 可读取。"})
    except (ValueError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        message = "当前解释器缺少 pytest，请安装 pytest>=8,<10。" if isinstance(exc, importlib.metadata.PackageNotFoundError) else str(exc)
        checks.append({"id": "runtime", "status": "error", "required": True, "message": message,
                       "suggestion": "选择已安装 pytest 和项目依赖的 Python 环境；可以使用 --python 指定。"})
    dependencies_ok = runtime_ok
    if runtime_ok and requirements:
        try:
            if getattr(sys, "frozen", False) and runtime["executable"] == sys.executable:
                for name in requirements:
                    try:
                        dependencies[name] = importlib.metadata.version(name)
                    except importlib.metadata.PackageNotFoundError:
                        dependencies[name] = None
            else:
                completed = subprocess.run([runtime["executable"], "-I", "-X", "utf8", "-c", _DISTRIBUTIONS, json.dumps(requirements)],
                                           capture_output=True, text=True, timeout=10, env=child_environment())
                if completed.returncode:
                    raise ValueError()
                dependencies = json.loads(completed.stdout)
            if not isinstance(dependencies, dict) or set(dependencies) != set(requirements) or any(
                    value is not None and not isinstance(value, str) for value in dependencies.values()):
                raise ValueError()
            for name, version in dependencies.items():
                if version is None:
                    dependencies_ok = False
                checks.append({"id": "dependency:" + name, "status": "ok" if version is not None else "error",
                               "required": True, "message": f"{name}：{version if version is not None else '未找到安装记录'}",
                               "suggestion": "" if version is not None else "在所选 Python 环境安装或确认这个依赖。"})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            dependencies_ok = False
            checks.append({"id": "dependencies", "status": "error", "required": True,
                           "message": "无法读取所选环境的依赖安装记录。"})
    elif requirements:
        checks.append({"id": "dependencies", "status": "not_checked", "required": True,
                       "message": "先修复 Python/pytest 环境，再检查指定依赖。"})
    git_ok = False
    git_program = shutil.which("git")
    if git_program:
        try:
            result = subprocess.run([git_program, "--version"], capture_output=True, text=True,
                                    timeout=5, env=child_environment())
            git_ok = result.returncode == 0 and result.stdout.startswith("git version ")
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks.append({"id": "git", "status": "ok" if git_ok else "warning", "required": False,
                   "message": "Git 可用。" if git_ok else "Git 不可用；独立快照验收仍可使用，Git 版本相关命令需要 Git。"})
    model = None
    model_ok = None
    if config_file is not None:
        try:
            settings = load_api_config(config_file)
            key = settings["api_key"]
            model = {"protocol": settings["protocol"], "model": settings["model"].replace(key, "[已隐藏]"),
                     "endpoint": settings["endpoint"].replace(key, "[已隐藏]"), "connection_tested": False}
            model_ok = True
            checks.append({"id": "model_config", "status": "ok", "required": True,
                           "message": "模型配置格式可读取；未连接服务，未检查额度或模型权限。"})
        except (OSError, ValueError) as exc:
            model_ok = False
            checks.append({"id": "model_config", "status": "error", "required": True, "message": str(exc),
                           "suggestion": "修正本机配置。已有测试的本地验收不依赖模型配置。"})
    else:
        checks.append({"id": "model_config", "status": "not_checked", "required": False,
                       "message": "未选择模型配置；如需离线检查可加 --config，本地验收不需要模型。"})
    ready = runtime_ok and dependencies_ok and model_ok is not False
    return {"schema_version": 1, "kind": "environment_diagnostics", "status": "ready" if ready else "attention",
            "reprolab_version": __version__, "checked_at": datetime.now(timezone.utc).isoformat(),
            "runtime": runtime, "dependencies": dependencies, "model": model, "checks": checks,
            "capabilities": {"snapshot_verification": runtime_ok and dependencies_ok,
                             "git_verification": runtime_ok and dependencies_ok and git_ok,
                             "model_configuration": model_ok},
            "tests_executed": False, "network_used": False,
            "notice": "这里只检查所选环境和明确指定的依赖元数据；就绪不代表项目测试可运行或修复已通过。"}
