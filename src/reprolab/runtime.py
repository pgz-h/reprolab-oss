"""Select an existing Python environment without losing its venv identity."""
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys


BASE_ENVIRONMENT_KEYS = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")


def child_environment():
    env = {key: os.environ[key] for key in BASE_ENVIRONMENT_KEYS if key in os.environ}
    env.update({"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def choose_runtime(executable=None, base=None):
    if executable is None:
        executable = sys.executable
    if not isinstance(executable, (str, os.PathLike)) or not str(executable):
        raise ValueError("Python 环境必须是解释器文件路径。")
    candidate = Path(executable).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base or Path.cwd()) / candidate
    # Resolving this symlink would bypass the selected virtual environment.
    candidate = Path(os.path.abspath(candidate))
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(f"Python 解释器不存在或不可执行：{candidate}")
    if str(candidate) == sys.executable:
        return {"executable": str(candidate), "python_version": sys.version.split()[0],
                "pytest_version": importlib.metadata.version("pytest")}
    code = ("import json,sys,pytest; print(json.dumps({"
            "'python_version':sys.version.split()[0],"
            "'version_info':list(sys.version_info[:2]),"
            "'pytest_version':pytest.__version__}))")
    try:
        result = subprocess.run([str(candidate), "-I", "-X", "utf8", "-c", code], capture_output=True,
                                text=True, timeout=10, env=child_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"无法启动所选 Python 环境：{exc}") from exc
    if result.returncode:
        if "No module named 'pytest'" in result.stderr:
            raise ValueError(f"所选 Python 环境缺少 pytest：{candidate}。请在该环境安装 pytest>=8,<10 后重试。")
        raise ValueError("所选 Python 环境不可用：" + result.stderr.strip()[-2000:])
    try:
        info = json.loads(result.stdout)
        valid = tuple(info["version_info"]) >= (3, 11) and 8 <= int(info["pytest_version"].split(".")[0]) < 10
    except (ValueError, KeyError, TypeError):
        raise ValueError("无法识别所选 Python/pytest 环境。") from None
    if not valid:
        raise ValueError("需要 Python >=3.11 和 pytest >=8,<10。")
    return {"executable": str(candidate), "python_version": info["python_version"],
            "pytest_version": info["pytest_version"]}


def worker_command(executable, arguments):
    """The app is a dispatcher, not a Python command-line interpreter."""
    if getattr(sys, "frozen", False) and executable == sys.executable:
        return [executable, "--reprolab-worker", *arguments]
    return [executable, "-I", "-X", "utf8", str(Path(__file__).with_name("_worker.py")), *arguments]


def snapshot_process_environment(environment, source, executable):
    """Carry this version's source and selected venv into ordinary descendants.

    The worker itself uses -I and explicitly inserts these source paths. Its
    subprocesses need the same roots even after tests change their directory.
    Never append an ambient/editable PYTHONPATH or mutate the shared run values.
    """
    result = dict(environment)
    source = Path(source).absolute()
    result['PYTHONPATH'] = os.pathsep.join((str(source / 'src'), str(source)))
    executable_dir = str(Path(executable).expanduser().absolute().parent)
    result['PATH'] = executable_dir + (os.pathsep + result['PATH'] if result.get('PATH') else '')
    return result
