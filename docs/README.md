# 文档导航

第一次使用先看[安装](../INSTALL.md)和[首次运行](first-run.md)。文档按用户任务组织；具体支持范围见[验证状态](release-readiness.md)。

## 使用项目

- [项目工作台](project-workbench.md)：版本、快照、测试范围和两版结果。
- [项目环境](project-environment.md)：已有 Python、依赖、插件和失败恢复。
- [运行变量](runtime-variables.md)：普通值、本机值和重放。
- [失败修复](failure-repair.md)：修复说明、草稿恢复和严格验收。
- [连续复查](report-recheck.md)：沿用基线、处理进展和未复查范围。
- [CLI 与 CI](cli-contract.md)：脚本调用、输出和退出码。
- [Go 项目对比](go.md)、[Vitest 项目对比](vitest.md)：非 Python 项目的 CLI 预览。

## AI 辅助（可选）

- [模型连接与多选](model-connections.md)：服务预设、自定义网关、保存多个模型和切换。
- [AI 项目验收](ai-project-acceptance.md)：生成计划、审阅、执行和结果解释。
- [AI 草稿工作流](ai-workflow.md)：单测试草稿、接口配置和离线响应。

## 证据与支持范围

- [真实项目适配](real-project-adaptation.md)：固定样本、执行范围和未支持特征。
- [历史案例来源](history-case.md)：内置 humanize 案例的来源与限制。
- [验证与发布状态](release-readiness.md)：当前候选和历史记录。
- [安全与隐私边界](../SECURITY.md)：可信代码、材料分享和问题反馈。

## 开发与发行

- [贡献指南](../CONTRIBUTING.md)：开发安装、改动与反馈。
- [架构地图](architecture.md)：模块职责、共享判定规则和验证入口。
- [打包配方](../packaging/README.md)：构建及桌面验收。
- [Alpha 发布流程](github-release.md)：源码、安装包、验证摘要与首次公开发布。

## 仓库布局

| 路径 | 内容 |
| --- | --- |
| `README.md`、`README.en.md` | 产品用途、开始方式、当前状态和主要入口 |
| `INSTALL.md` | 源码、应用包、wheel 安装与升级 |
| `src/reprolab/` | CLI、桌面、执行核心、证据与内置示例 |
| `tests/` | 受控回归检查；外部服务使用本地模拟 |
| `docs/` | 用户流程、接口契约、兼容性和开发文档 |
| `packaging/` | 构建、验收脚本、发行核对和第三方许可证 |
| `.github/` | CI、问题模板和 PR 模板 |
| `LICENSE`、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md` | 许可、贡献、安全边界和版本记录 |

环境、凭据、本机设置、工作项目、完整日志及安装产物留在源码仓库之外。公开源码审阅副本由现有发布脚本按白名单生成，不包含开发 Git 历史或本机 AGENTS.md；它是交付材料，唯一开发目录继续保留。

新公开仓库地址尚未确定，文档使用相对链接。确定地址并实际发布后再添加仓库和下载入口，不放虚构徽章、下载量或未发生的发布记录。
