# macOS 桌面构建与实测

源码入口 desktop_entry.py 按参数分别分派 Qt UI、--reprolab-worker、--cli 和显式 --desktop-smoke。worker 不导入 Qt；打包应用启动测试时使用专用 worker 参数，外部 Python 继续以 -I 执行 _worker.py。虚拟环境路径不 resolve。

Qt 仅用 Essentials 的 Widgets/Gui/Core/Test；不使用 WebEngine。desktop.spec 包含内置案例完整源码、pytest 元数据和第三方许可。使用动态 Qt 库，未改动 Qt；用户可从公开上游获取对应源码并重建/替换组件。

固定构建依赖见 requirements-macos.txt，本轮 CPython 3.14.7、PySide6 6.12.0、PyInstaller 6.22.3、pytest 9.2.0。现有 CLI 环境不需要安装这些桌面依赖。

所有以下路径都应在源码仓库外；不要覆盖旧版本目录：

    python3 -m venv /path/to/work/desktop-build/.venv
    /path/to/work/desktop-build/.venv/bin/python -m pip install -r packaging/requirements-macos.txt
    /path/to/work/desktop-build/.venv/bin/python packaging/build_macos.py \
      --work-root /path/to/work/desktop-build \
      --output-dir /path/to/outputs/releases/VERSION

构建脚本生成 .app、包含 .app 的 ZIP、DMG；拒绝覆盖同名发行附件。使用本地 ad-hoc 签名，没有 Developer ID 或公证。不要将构建成功当成可运行证明。

只需要应用和 ZIP，或构建环境无法创建磁盘镜像时，可追加 `--skip-dmg`。此选项只省略 DMG，不运行任何验收流程。

## v0.18 项目工作台集中流程

    PYTHONPATH=src python packaging/project_workbench_smoke.py /path/to/new/workbench-demo
    /path/to/ReproLab.app/Contents/MacOS/ReproLab --project-smoke /path/to/new/package-check --session /path/to/demo-project-session

集中演示使用新建教学项目，源流程一次执行 263 项前后比较，核对混合结果矩阵、会话恢复、ZIP 导出/导入、明确重放及固定输入 AI 桥接。打包流程只选择其中两项，核对内置项目 worker 分派和原生界面，不重复大批自动化回归。新增项目 worker 有单独分派入口，外部环境使用随包的 _project_worker.py。

## 显式打包实测

以下命令打开真实 Qt 窗口，通过 Qt Test 点击实际验收按钮，启动真实测试工作进程；只有判定 verified、两版真实结果完整、报告存在才通过。每轮保存 JSON 记录与窗口截图。它不调用模型，也不使用真实模型配置。适合不具备系统辅助功能权限的构建环境，但不等同于人工拖拽/点击 Finder 的记录。

    /path/to/ReproLab.app/Contents/MacOS/ReproLab --desktop-smoke /path/to/qa/internal --open-report
    /path/to/ReproLab.app/Contents/MacOS/ReproLab --desktop-smoke /path/to/qa/internal --restart-only
    /path/to/ReproLab.app/Contents/MacOS/ReproLab --desktop-smoke /path/to/qa/external \
      --python /path/to/project-env/bin/python --case /path/to/cases/example.json

--open-report 记录操作系统接受打开本地报告 URL，不宣称自动检查了浏览器显示内容。可用 macOS 的 open -n -W /path/to/ReproLab.app --args --desktop-smoke ... 同时检查 Launch Services 应用启动路径。

还应挂载发行 DMG，用其中只读的 .app 执行 smoke，重启读取设置与报告，再卸载；最后检查 hdiutil verify 和 SHA-256。应用不写入包内，运行材料与设置保持在应用外。

--state-dir /path/to/state 可用于隔离人工测试的桌面设置；正常双击无需任何参数。

## Windows x64 桌面构建

Windows x64 在 Windows 主机使用 `build_windows.py`，与 macOS 共用桌面打包配方。脚本验收实际 `.exe` 后再生成便携 ZIP。见[Windows 指南](../docs/windows.md)。

## 第三方组件

应用的 Contents/Resources/licenses 保存随构建组件分发的许可文件。ReproLab 自身是 MIT；humanize 示例保留原许可证。Qt/PySide6/Shiboken 使用各自 LGPL/GPL/商业多重许可，当前构建使用开源发行的动态组件；应用不限制用户为调试修改这些库而进行的反向工程。

- Python 3.14.7 源码：https://www.python.org/downloads/source/
- Qt/PySide 源码：https://download.qt.io/official_releases/QtForPython/
- Qt 源码：https://download.qt.io/official_releases/qt/
- PyInstaller 源码和引导器例外许可：https://github.com/pyinstaller/pyinstaller
- pytest：https://github.com/pytest-dev/pytest

最终发行应保留源码包、构建配方、固定依赖和这些许可材料。

## 新工作流打包实测

    /path/to/ReproLab.app/Contents/MacOS/ReproLab --workflow-smoke /path/to/qa/new-workflows

该显式测试在输出目录新写一个非私有教学 Git 项目，通过真实窗口创建案例并验收、准备草稿、搜索勾选后批量运行、取消第二项并保留第一项结果。不会读取用户模型配置或调用模型。只读挂载 DMG 后也应使用此入口检查新增界面与打包工作进程。

## 核心诊断与恢复实测

    /path/to/ReproLab.app/Contents/MacOS/ReproLab --diagnostics-smoke /path/to/qa/core --python /path/to/qa-env/bin/python

QA环境需包含pytest及新写的教学模块reprolab_qa_dependency（VALUE=42）；应独立放在work目录，不改用户项目环境。此显式流程用真实窗口/按钮检查预检阻止执行、缺模块、更换环境后verified、只重跑未通过、取消后继续未完成，以及缺失worker记录。所有测试代码为本地新写样例，不读取用户模型配置或调用模型。

## v0.13 执行边界和保存实测

    /path/to/ReproLab.app/Contents/MacOS/ReproLab --reliability-smoke /path/to/qa/reliability
    /path/to/ReproLab.app/Contents/MacOS/ReproLab --reliability-smoke /path/to/qa/external --python /path/to/env/bin/python

真实 Qt 窗口/按钮和 worker 检查动态多测试在 fixture/body 前停止、零收集拒绝、完整证据导出/导入/明确重放、受控写入失败不留通过记录、旧报告保留、日志变化检测和未完成材料提示。失败注入只用于显式 smoke，不调用模型。最终只读 DMG 也执行同一实测。

## v0.14 当前工作区与参数化选择实测

    /path/to/ReproLab.app/Contents/MacOS/ReproLab --workspace-smoke /path/to/qa/workspace
    /path/to/ReproLab.app/Contents/MacOS/ReproLab --workspace-smoke /path/to/qa/external --python /path/to/env/bin/python

新写本地教学Git项目，原窗口勾选当前工作区、实际收集参数、保存新案例、修改原仓库后仍按冻结内容verified，并检查完整证据。复用真实Qt按钮和worker。输出目录必须不存在；不读取模型配置，不调用模型。

## v0.15 故障识别实测

    /path/to/ReproLab.app/Contents/MacOS/ReproLab --probe-smoke /path/to/qa/probe
    /path/to/ReproLab.app/Contents/MacOS/ReproLab --probe-smoke /path/to/qa/external --python /path/to/env/bin/python

使用教学Git项目实际操作测试推荐、旧版试运行、明确采用和保存，随后修改原仓库并完成固定案例的正式两版验收。截图和JSON保存到指定新目录。

## v0.17 模型目录与AI项目验收实测

    /path/to/ReproLab.app/Contents/MacOS/ReproLab --suite-smoke /path/to/new/qa-directory
    /path/to/ReproLab.app/Contents/MacOS/ReproLab --suite-smoke /path/to/new/external-qa --python /path/to/env/bin/python

实测使用新写教学Git项目和本地HTTP模拟模型，验证自定义服务URL/Key表单、GET模型目录、网关别名点选、保存重开、更换服务时旧Key需要明确确认、Responses请求、发送预览、多项计划、真实worker新增回归、仅重跑选择、AI解读及重开记录。目录获取2次、模型调用3次，均发往本机模拟服务；更换服务前先检查未确认时没有发出请求。环境真实下载安装另由work目录中的公开pytest教学项目验证。

dev2 在同一入口增加结构化编辑、另存独立人工审阅计划、原计划保留、审阅条件展示，以及模拟进程中断后从完整本地响应恢复并保留未完成产物。修订与恢复前后核对请求数，确认没有额外调用模型。

dev3 故意让模拟模型提出错误的消息条件，再在固定计划内真实试运行旧版、明确确认后预填实际故障条件、另存并立即验收，最后针对原计划重开观察记录。观察、采用与恢复均不增加模型请求；两版正式运行仍保留实际新增回归。

## macOS 输入与前台激活验证

    open -n -W /path/to/ReproLab.app --args --input-smoke /path/to/new/input-qa

此入口使用隔离的测试配置，通过设置页的正常“连接模型”按钮打开模态窗口。先读取 macOS activationPolicy，要求为普通前台应用，再检查窗口激活、鼠标焦点、Tab 导航、Qt 键盘事件逐字输入、中文输入法提交事件及保存。使用合成地址与凭据，模型服务请求为0。结果保存在 input-result.json。

spec 显式设置 LSBackgroundOnly=false。EXE 保留 console=True 以支持验收 worker 的标准输入输出。构建脚本检查实际产物的应用标记，拒绝后台或 agent 类型的桌面包。系统级键盘注入需要额外辅助功能权限，本入口检查 AppKit 激活状态和应用内部键盘事件。

## 公开源码与发布材料

Python 发行包和应用 ZIP 构建到同一仓库外目录后，可执行：

    python packaging/prepare_release.py --output-dir /path/to/releases/VERSION

脚本整理 SOURCE_REVIEW 副本、公开源码 ZIP、相对路径内容清单、发布说明和校验文件。它排除 Git 历史、缓存、构建输出、模型环境文件及内部过程文档，并对常见凭据格式和本机用户绝对路径做基础检查；这不等于完整安全审计。脚本不运行应用、项目测试或模型请求，也不自动创建仓库或发布。

## 逐项进度与中断记录验证

    ReproLab.app/Contents/MacOS/ReproLab --progress-smoke /path/to/new/progress-qa

使用独立四项受控项目，经正常桌面按钮检查实时测试阶段与数量、取消后的观察保存、证据一致性、环境处理入口和修复说明。可设置 QT_QPA_PLATFORM=offscreen 做无屏检查；该方式不等同于系统级输入验证。不调用模型或联网安装依赖。

## 可撤销的项目插件配置验证

    ReproLab.app/Contents/MacOS/ReproLab --config-smoke /path/to/new/config-qa

使用独立受控 Git 项目，验证缺少 fixture 时受阻、配置预览取消、应用、重开后撤销、原快照保留、刷新后实际两版通过和原报告一致性。记录截图与 config-result.json。该入口不安装项目依赖或请求模型；可用 QT_QPA_PLATFORM=offscreen 核对应用内交互。

## 多轮处理记录验证

    ReproLab.app/Contents/MacOS/ReproLab --history-smoke /path/to/new/history-qa

使用独立三项受控项目完成三轮检查，核对组内复查、普通刷新来源关联、跨轮未复查项、本机说明保存／导出／重开、来源丢失与重新定位，以及原证据保持一致。可设置 QT_QPA_PLATFORM=offscreen，记录截图与 history-result.json；不请求模型。

## 当前候选的安装与发布一致性

构建完成后，在新的虚拟环境安装候选 wheel，运行 `python -I packaging/verify_install.py --output /path/to/new/install-check`。检查在仓库外执行，覆盖内置教学／历史案例、项目两版比较、导出／导入和重放；拒绝源码直跑及 editable 安装。CI 同样执行这个入口。

`prepare_release.py` 从当前 CHANGELOG 生成更新说明，逐文件核对 Python 发行包源码，校验可选 validation-summary.json 的版本与源码指纹。摘要格式和首次公开仓库的准备见[发布指南](../docs/github-release.md)。脚本不自动验证桌面包，也不会把旧测试记录当作新候选已通过。

## 首次接入与连续准备验收

    ReproLab.app/Contents/MacOS/ReproLab --onboarding-smoke /path/to/new/onboarding-qa

使用独立受控项目检查连续冻结和发现、运行前范围核对、空选择保护、明确执行、失败优先展示、新建项目导航、项目偏好保存恢复和发现错误入口。保存五张流程截图与 onboarding-result.json；可通过 QT_QPA_PLATFORM=offscreen 运行，不调用模型。

## 运行变量端到端验收

    ReproLab.app/Contents/MacOS/ReproLab --runtime-smoke /path/to/new/runtime-qa

使用受控项目和合成的本机值，经实际环境窗口配置、预览和刷新，检查发现受阻、两版一致输入、记录不序列化本机值、证据导出导入、缺值重放停止、重新提供值后重放、跨轮条件未知及严格快照案例。保存截图与 runtime-result.json，不调用模型或真实外部服务。


## 修复工作连续性与中断恢复验收

先后启动两个独立进程，第二次使用相同目录：

    ReproLab.app/Contents/MacOS/ReproLab --repair-smoke /path/to/new/repair-qa --phase interrupt
    ReproLab.app/Contents/MacOS/ReproLab --repair-smoke /path/to/new/repair-qa --phase resume

第一进程用内置购物车产生真实失败，填写并手工修改修复说明，等待自动保存后直接退出，不调用窗口关闭保存。第二进程从实际桌面入口恢复，检查冲突、来源缺失、放弃、同源严格预填、明确确认与真实执行，随后复查项目并校验证据。保存截图和两个阶段的 JSON。只运行教学代码，不调用模型。`--phase interrupt` 的直接退出仅在这个显式 QA 入口执行。


## 受信任真实项目适配验收

`--adaptation-smoke OUTPUT --repository REPO --python PYTHON --plugin MODULE --count N --case CASE` 使用调用方明确提供的可信本地项目，检查缺少插件时的收集阻断、配置预览应用、刷新、完整范围执行、复查及固定历史案例。可重复传入 --plugin；输出目录须不存在，原项目仅由配置预览确认后写入 ReproLab 配置。不会下载项目或联系上游，外部代码执行须事先获用户授权。
