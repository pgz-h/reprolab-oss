# 安装 ReproLab

ReproLab 仍处于 Alpha 阶段。源码安装需要 Python 3.11 或以上。macOS arm64 使用下方安装流程，Windows x64 的源码试用和构建步骤见[Windows 指南](docs/windows.md)。两端当前验证范围见[平台说明](docs/platforms.md)。

## 从已取得的源码安装

在包含 `pyproject.toml` 的源码根目录打开终端。安装到独立环境后，可以直接体验内置示例。

```sh
python3 -m venv ~/.venvs/reprolab
~/.venvs/reprolab/bin/python -m pip install .
~/.venvs/reprolab/bin/reprolab --version
~/.venvs/reprolab/bin/reprolab demo --output ~/reprolab-reports
```

安装桌面依赖并启动界面可使用下面的命令。

```sh
~/.venvs/reprolab/bin/python -m pip install ".[desktop]"
~/.venvs/reprolab/bin/reprolab-desktop
```

pip 安装可能需要网络。已有测试和内置示例可以直接使用，AI 是可选功能。源码必须来自你有访问权限并且信任的项目来源。

## 使用 macOS 应用 ZIP

已取得相应版本的 `ReproLab-<版本>-macOS-arm64.zip` 时，解压，将 `ReproLab.app` 放到合适目录，再打开应用。应用自带 Python 和 pytest。打开后可从“开始使用”运行内置示例。

公开下载以仓库 Releases 实际列出的附件为准。只有源码压缩包或本地构建记录时，请使用上面的源码安装方法。GitHub 自动生成的源码 ZIP 需要另行安装或构建。

应用目前使用 ad-hoc 签名，尚未完成 Apple Developer ID 公证。若 macOS 提示无法验证开发者，请先核对下载来源和校验值。确认信任后，可按系统“隐私与安全性”中的单个应用提示处理。[Apple 的打开应用说明](https://support.apple.com/zh-cn/102445)

## 使用 Python wheel

如果已取得 `reprolab_local-<版本>-py3-none-any.whl`，将实际路径代入下面的命令。

```sh
python3 -m venv ~/.venvs/reprolab
~/.venvs/reprolab/bin/python -m pip install /path/to/reprolab_local-VERSION-py3-none-any.whl
~/.venvs/reprolab/bin/reprolab --version
```

`none-any` 表示发行包没有编译扩展，具体系统支持范围仍以上面的说明为准。需要源码构建时，可对取得的 `.tar.gz` 使用 `pip install`，也可使用源码目录。

## 接入自己的项目

在项目工作台选择可信的本地 Git 仓库，并选择已经具备该项目依赖的 Python。应用自带环境适合内置示例，项目自己的虚拟环境更适合实际项目。

CLI 可以先检查解释器和已安装依赖的元数据。

```sh
~/.venvs/reprolab/bin/reprolab doctor --python /path/to/project-env/bin/python --require requests
```

`--require` 使用发行包名称，可重复指定。元数据检查只能说明解释器和已安装版本情况，实际收集与运行还会检查导入、配置和 fixture。完整操作见[第一次使用](docs/first-run.md)和[失败修复指南](docs/failure-repair.md)。

## 升级、校验和反馈

安装新的 wheel 可使用 `python -m pip install --upgrade /path/to/new-release.whl`。升级源码安装时，在新源码根目录重新运行 `python -m pip install --upgrade .`。桌面版本在“设置与诊断”页底部查看，新项目报告也记录生成它的工具版本。打开新的应用包后先核对版本，避免继续使用原来安装的旧应用。

有 `SHA256SUMS.txt` 时，使用 `shasum -a 256 文件名` 与对应记录核对。校验值用于发现文件变化。反馈问题时，请附上应用版本、系统与芯片、入口、操作步骤和已检查的错误片段。

卸载 CLI 可以使用 `~/.venvs/reprolab/bin/python -m pip uninstall reprolab-local`。案例、报告和用户设置保存在各自选择的目录，可按需保留。
