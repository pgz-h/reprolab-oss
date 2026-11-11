# Windows x64 指南

Windows x64 与 macOS 共用源码。当前 **0.19.0a21** 的 Windows 原生验收与 `.exe` 构建待执行。

## 已整理的适配

- 工作数据使用 `%LOCALAPPDATA%/ReproLab`，桌面模型配置共用该位置；报告默认位于用户主目录下的 `Documents/ReproLab`，可在设置中另选。
- 文件锁使用 Windows 字节区间锁，保留跨进程冲突检查；打开敏感配置时拒绝文件重解析点。
- 新私密文件使用仅当前用户和 SYSTEM 可访问的受保护 DACL；修改已有项目配置时复制其 DACL。配置仍是明文，访问控制不等于加密。
- 工作进程通过等待门控加入 kill-on-close Job Object，成功后才执行请求的命令；取消、超时和正常结束均关闭整个受管工作树。失败不降级为无 Job 执行。
- 明确文本文件和隔离 Python 的 UTF-8 设置，覆盖中文及带空格路径；虚拟环境使用 `Scripts/python.exe`。
- 增加 Windows x64 构建脚本、实际冻结 exe 验收脚本和自动构建工作流。

以上 Windows API 路径仍需在 Windows 上执行测试；当前 Mac 检查不能代替原生结果。源码副本和 Job Object 不是安全沙箱，只运行可信项目。

## 从源码试用

在 Windows x64 Python 3.11 或以上环境，从源码根目录打开 PowerShell。不要复用其他系统的虚拟环境。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -X utf8 -m pip install ".[desktop]"
.\.venv\Scripts\python.exe -X utf8 -m reprolab history --output "$env:TEMP\reprolab-history"
.\.venv\Scripts\reprolab-desktop.exe
```

无需激活脚本。安装会按平台安装 `pywin32`；无需模型账号即可运行内置案例。项目检查还需要 Git for Windows，并选择已安装项目依赖的 Python。

## 在 GitHub 构建与验收

`windows-build.yml` 会在 main 推送或 PR 时运行，也可从 Actions 手动运行 **Build and verify Windows x64 candidate**。

工作流使用 Windows x64 Python 3.14，先运行平台原生检查，再打包实际 `.exe`，检查：

1. 文件锁、受保护文件 DACL、取消和正常退出后的子进程清理。
2. 冻结应用的内置与历史案例、中文路径、项目比较、证据导出导入和重放。
3. 冻结应用的 Qt 基础、首次接入、可撤销配置，以及本地模拟模型流程。

只有验收通过才生成 `ReproLab-0.19.0a21-Windows-x64.zip` 便携包，内含 `ReproLab/ReproLab.exe` 与依赖文件。解压整个目录后启动 exe；不要只复制单个 exe。

工作流产物位于 Actions 的 Artifacts，**不会自动发布 GitHub Release**。原生失败记录单独保留。Windows runner 的 Qt offscreen 检查仍需补充实际用户桌面安装、输入与界面验收。

## 在 Windows 本机打包

安装候选桌面依赖及 PyInstaller 6.22.3，然后使用两个尚不存在的输出目录：

```powershell
.\.venv\Scripts\python.exe -m pip install "PyInstaller==6.22.3" "build>=1.2"
.\.venv\Scripts\python.exe -X utf8 packaging/build_windows.py --work-root "$env:TEMP\reprolab-build-a19" --output-dir "$env:TEMP\reprolab-release-a19"
```

脚本调用 `verify_windows.py` 检查实际 exe，失败时停止，不生成可分发 ZIP。当前构建为未签名便携包，不生成安装向导或 MSI；签名与安装器可在核心原生流程通过后增加。

PyInstaller 需要在目标系统上构建，见[官方说明](https://pyinstaller.org/en/stable/operating-mode.html)。macOS 上不能把 `.app` 改名为 `.exe` 或据此声明 Windows 可用。

## 当前边界

Windows 原生测试、构建、签名和独立用户验收尚未执行；Windows ARM64 未包含。POSIX 权限与 Windows ACL 含义不同，跨系统证据包迁移及重放兼容性尚未验证，不能承诺已有 Mac 证据在 Windows 上直接重放。当前实际结果见[验证说明](release-readiness.md)。
