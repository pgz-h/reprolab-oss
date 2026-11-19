# ReproLab

**0.19.0a25 Alpha** · [已发布版本与下载](https://github.com/pgz-h/reprolab-oss/releases)

**用实际测试核对一次修复，或比较一次项目改动，并留下可重放的证据。**

ReproLab 冻结改动前后的源码，在两版上运行同一份选定测试，并保存可校验、可重放的报告、日志与快照。项目对比区分新增失败、原有失败、改善和受阻。严格修复验收先核对旧版是否出现声明的故障，再检查新版是否通过。AI 可以辅助准备和解读，结论来自实际执行，范围限于所选测试。

[English](README.en.md) · [文档导航](docs/README.md) · [第一次使用](docs/first-run.md) · [安装](INSTALL.md) · [项目工作台](docs/project-workbench.md) · [CLI 与 CI](docs/cli-contract.md) · [更新记录](CHANGELOG.md)

Rust、C 和 C++ 项目可用 Cargo 与 CMake/CTest 入口。见[支持范围与真实 zlib 复跑](docs/native-languages.md)。

## 给自己的 PR 选测试

在有 pytest 测试的可信 Git 仓库中，先把当前工作区改动与 `HEAD` 对比，预览测试清单：

```sh
reprolab project suggest . --before HEAD --working-tree --output ../reprolab-review
```

使用独立测试环境时加 `--python /path/to/env/bin/python`。预览会指出测试定义改在哪里、哪些测试可能间接受影响，并给出使用本次快照的实际运行命令。先审阅清单，测试主体在下一步运行。已有报告的读者可直接看下节。[两个真实 PR 的复跑方法与结果](docs/suggest-upstream.md)

## 第一次评审：这盏绿灯足以合入吗？

一个收据测试在改动前后都失败了，另一个测试两次都通过。旧报告按“允许原有失败”放行。作为接收者，你可以先用[公开发行版的 CLI](https://github.com/pgz-h/reprolab-oss/releases/tag/v0.19.0a24) 核对这个合成案例，无需准备测试环境或配置模型。

下载[示例证据包 evidence.zip](https://raw.githubusercontent.com/pgz-h/reprolab-oss/e682f9de5d3ba923d273d7a776c74fa6640bd39d/tests/fixtures/review_summary_cb56b04/evidence.zip)，在下载目录运行：

```sh
reprolab project summary evidence.zip --gate no-new-failures --include-test-ids
```

**预期显示 `CI gate · Blocked`，退出码为 `2`。** 两版的收据测试都抛出断言错误，但异常消息指纹发生了变化。现有证据不足以将它认作同一个旧故障。报告中可按测试 ID 找到这项观察；读取 ZIP 不会运行其中的项目。

若要核对当时发出的绿灯，把[原摘要 public.md](https://raw.githubusercontent.com/pgz-h/reprolab-oss/e682f9de5d3ba923d273d7a776c74fa6640bd39d/tests/fixtures/review_summary_cb56b04/public.md) 和[配套记录 public.json](https://raw.githubusercontent.com/pgz-h/reprolab-oss/e682f9de5d3ba923d273d7a776c74fa6640bd39d/tests/fixtures/review_summary_cb56b04/public.json) 下载到同一目录：

```sh
reprolab project check-summary evidence.zip public.md --public-summary --gate no-new-failures --facts-file public.json
```

这一步退出码为 `0`：历史材料核对一致。`historical_gate.exit_code` 是当时的 `0`，`gate.exit_code` 是今天用 v0.19.0a24 重算的 `2`。想从真实上游改动开始，可按 [async-lru PR #799 复跑指南](docs/replay-handoff-case.md) 核对三个上游回归测试。

## 检查自己的项目

用户安装包只提供工作台和 CLI。选择可信 Git 仓库、基线提交和一项已有测试，即可运行对比。将下面的路径和测试 ID 换成项目中的实际值，输出目录放在仓库之外。

```sh
reprolab project review /path/to/repo --before HEAD --working-tree \
  --test tests/test_feature.py::test_feature --output /path/to/reports
```

教学案例与贡献者测试仅随完整公开源码提供。[CLI 与 CI](docs/cli-contract.md) · [首次使用](docs/first-run.md)

## 适合什么任务

适合已有 pytest 测试、需要反复修复、审查补丁或交接执行证据的可信本地项目。与直接运行一次 pytest 相比，ReproLab 额外固定两版输入、整理结果变化并保留各轮检查范围。已有 tox／Nox／CI 流程可以继续负责环境准备和日常测试。

## 当前版本

本地开发功能见对应使用指南。公开预发行包支持的功能以 GitHub Releases 的版本说明为准。见[更新记录](CHANGELOG.md)、[平台状态](docs/platforms.md)和[验证说明](docs/release-readiness.md)。

桌面面向 macOS arm64，采用 ad-hoc 签名，尚未完成 Apple 公证。下载入口和发行校验值以 GitHub Releases 中的实际附件为准。

## 按任务开始

| 想完成的任务 | 使用入口 |
| --- | --- |
| 检查一次修改有没有让相关功能失败 | 项目检查：冻结版本、发现测试、选择并对比 |
| 确认一个具体问题已经修好 | 案例验收：预先声明旧版故障，用同一测试验收新版 |
| 还没写好检查 | AI 验收计划：准备材料、审阅生成内容，再实际执行 |
| 查看、交付或重放检查结果 | 报告与证据：查看、校验、导出、导入和明确重放 |


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

macOS arm64 用于本地开发与验证。0.19.0a22 的 Windows x64 便携包有单独的 GitHub Actions 原生和实际 exe 验收记录。当前工作树的验证范围见[验证状态](docs/release-readiness.md)、[Windows 指南](docs/windows.md)和[安全边界](SECURITY.md)。

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
