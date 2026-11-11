# ReproLab

**用实际测试核对一次修复，或比较一次项目改动，并留下可重放的证据。**

ReproLab 面向 Python／pytest 项目，提供 macOS 桌面应用和 CLI。它冻结改动前后的源码，用同一份测试分别执行，显示新增失败、原有失败、改善和受阻项，保存报告、日志与源码快照。AI 可以帮助准备检查，正式结论来自实际执行，范围限于所选场景。

[English](README.en.md) · [文档导航](docs/README.md) · [第一次使用](docs/first-run.md) · [安装](INSTALL.md) · [项目工作台](docs/project-workbench.md) · [CLI 与 CI](docs/cli-contract.md) · [更新记录](CHANGELOG.md)

## 先看一次真实修复

内置 humanize 历史案例用同一测试核对 `naturalsize("1")`：旧版返回 `1.0 Byte`，修复版返回 `1 Byte`。运行后得到两版执行记录、修复差异和离线 HTML 报告，无需模型账号或下载被测项目。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/reprolab history --output /tmp/reprolab-history
```

以上命令在已取得的源码根目录执行。案例采用上游原始单模块和 ReproLab 编写的行为检查；它不是完整 humanize 项目适配，也不是上游采用证明。见[案例与来源](docs/history-case.md)。

开发版另提供 JavaScript／TypeScript 的 [Vitest 命令行预览](docs/vitest.md)，可按文件比较两版并导出证据。

## 适合什么任务

适合已有 pytest 测试、需要反复修复、审查补丁或交接执行证据的可信本地项目。与直接运行一次 pytest 相比，ReproLab 额外固定两版输入、整理结果变化并保留各轮检查范围。已有 tox／Nox／CI 流程可以继续负责环境准备和日常测试。

## 当前版本

**0.19.0a21 Alpha 候选**以 Python／pytest 为主，包含桌面项目工作台、一条命令完成的 CLI 项目对比，以及可携带复查记录的证据 ZIP。Vitest 4 作为 CLI 预览提供。见[更新记录](CHANGELOG.md)、[平台状态](docs/platforms.md)和[验证说明](docs/release-readiness.md)。

桌面面向 macOS arm64，采用 ad-hoc 签名，尚未完成 Apple 公证。下载入口和发行校验值以 GitHub Releases 中的实际附件为准。

## 按任务开始

| 想完成的任务 | 使用入口 |
| --- | --- |
| 检查一次修改有没有让相关功能失败 | 项目检查：冻结版本、发现测试、选择并对比 |
| 确认一个具体问题已经修好 | 案例验收：预先声明旧版故障，用同一测试验收新版 |
| 还没写好检查 | AI 验收计划：准备材料、审阅生成内容，再实际执行 |
| 查看、交付或重放检查结果 | 报告与证据：查看、校验、导出、导入和明确重放 |

第一次打开可选择“运行内置示例”；想了解完整项目流程，可选择“创建项目示例”。购物车示例只准备独立项目和冻结会话，由你决定发现和执行测试。两者均为人为构造的教学材料。

## 项目检查

- 比较两个 Git 提交，或一个 Git 基线与当前未提交工作区；准备后保留独立固定输入。
- 桌面可一次准备快照并发现测试，随后核对选择再明确运行；可采用该项目上次使用的环境与时限。
- 实际发现参数化 pytest 目标，一次选择 1–2000 项；两版各使用一个 pytest 进程和各自的正常 fixture 生命周期。
- 浏览代码差异，按目录、文件、推荐原因筛选测试，明确提示被筛选隐藏的勾选项。
- 保存并复用命名选择方案，导入导出 JSON／文本清单，重开时恢复勾选、筛选和限时。
- 修改代码后刷新当前工作区，沿用原固定基线另存会话并带入选择；逐项核对缺失测试，保留旧证据。
- 对比两版结果，定位需处理项，查看异常阶段、实际保存的测试代码和环境处理建议。
- 对比历史中的两次运行，分别显示结果变化和选择范围变化；仅一次选中的项不算作已解决。
- 导出离线 HTML、结果 CSV 或证据 ZIP，校验后导入并明确重放。复制摘要不自动发送到外部服务。

两版使用变更后版本的同一份所选测试文件，各自的配置和 fixture 分别保留。项目回归通过只覆盖所选测试；正式确认目标修复时，使用严格案例验收流程。

## 桌面安装

如果你已取得对应版本的 macOS arm64 验证 ZIP，解压并打开 `ReproLab.app`。应用内置 Python 和 pytest。自己的项目仍需选择已安装依赖的环境。

公开安装附件以仓库 Releases 页面实际列出的版本为准。尚无所需附件时，可按[安装说明](INSTALL.md)从已取得的源码安装桌面版。

## CLI 快速开始

需要 Python 3.11 或以上。在取得源码的根目录创建独立环境并安装 CLI；以下激活命令用于 macOS／Linux shell。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
reprolab demo
```

检查可信项目的当前改动时，替换仓库、解释器和测试 ID，并把输出保存到仓库外。

```sh
reprolab project review /path/to/repo --before HEAD --working-tree \
  --python /path/to/project-env/bin/python --test 'tests/test_feature.py::test_behavior' \
  --output /tmp/reprolab-review --progress
```

命令冻结两版、发现并执行所选测试。输出 JSON 中的 `report_dir` 指向报告目录，可打开其中的 `report.html`。比较两个提交时，将 `--working-tree` 换成 `--after CANDIDATE`，并把 `--before` 指向基线。想先核对测试清单时，使用 `project prepare`、`collect` 和 `run`。CI 退出码、清单和复查见 [CLI 与 CI](docs/cli-contract.md)。

## AI 与环境

AI 为可选功能，用于准备回归测试草稿、生成可审阅的验收计划，以及解读实际验收结果。它不直接修复业务代码，也不决定测试是否通过。

连接时选择 DeepSeek、OpenAI、Anthropic、智谱 GLM 或自定义 API，填写 Key，获取并勾选多个模型，选择默认模型后保存。后续在设置和 AI 验收入口切换已添加模型，不必重填连接。连接测试只验证当前所选模型；目录中的其他模型不因此算作已验证。详细操作和数据发送范围见[模型连接指南](docs/model-connections.md)。

支持 Chat Completions、Responses 和 Anthropic Messages 格式的模型服务，主要通过 API Key 接入。发送前可审阅材料；准备、已有测试验收和证据检查不调用模型。真实模型服务兼容性和生成质量仍需按实际账号与项目核对。见 [AI 工作流](docs/ai-workflow.md)。

环境工具读取基础依赖与明确选择的测试／开发依赖组，并可预览新建项目外环境。环境窗口还提供插件安装信息、配置预览和缺失依赖摘要，见[项目环境接入](docs/project-environment.md)。它不完整锁定动态或传递依赖，重放仍需要合适的运行环境。

运行变量可在“检查环境 → 运行变量”配置，普通值随快照保存，本机值运行时提供；缺值会停止，跨轮本机值的一致性保持未知。见[运行变量指南](docs/runtime-variables.md)。

## 执行与结论边界

只运行可信代码。pytest 收集、配置、fixture 和测试都可能执行本机代码；临时副本和子进程不是安全沙箱。被测代码仍可能访问当前用户的文件和网络。

保存材料可校验、可重新发起执行，并不保证另一台机器得到相同结果。依赖、外部服务、时间和随机性仍可能影响重放。

环境错误、收集或 fixture 失败、超时、跳过、xfail 和不完整记录不能计入通过。日志、源码与模型材料可能含项目数据，分享前应检查。证据哈希校验内容一致性，不证明作者身份。

macOS arm64 用于本地开发与验证。Windows x64 已纳入构建与验收流程，原生结果待验证。见[Windows 指南](docs/windows.md)和[安全边界](SECURITY.md)。

## 文档与贡献入口

| 需要什么 | 从这里开始 |
| --- | --- |
| 安装并运行第一个示例 | [安装](INSTALL.md)、[第一次使用](docs/first-run.md) |
| 接入项目、处理失败、连续复查 | [项目工作台](docs/project-workbench.md)、[失败修复](docs/failure-repair.md) |
| 配置多个模型并使用 AI | [模型连接](docs/model-connections.md)、[AI 验收助手](docs/ai-project-acceptance.md) |
| 核对兼容性与验证范围 | [真实项目适配](docs/real-project-adaptation.md)、[验证状态](docs/release-readiness.md) |
| 开发与贡献 | [贡献指南](CONTRIBUTING.md)、[架构地图](docs/architecture.md)、[文档导航](docs/README.md) |

## 参与与发布

欢迎提供可复现的问题和具体使用场景，见 [贡献指南](CONTRIBUTING.md)。构建流程只生成发行文件，不等同于运行验收；发布步骤见 [发布指南](docs/github-release.md)。

ReproLab 自身采用 [MIT 许可证](LICENSE)。humanize 历史示例保留上游许可证与来源；桌面内含组件使用各自许可证，材料与构建说明见 [packaging](packaging/README.md)。
