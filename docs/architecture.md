# 架构与贡献入口

ReproLab 的核心输出是带明确范围的执行记录。先选择要改善的用户任务，再定位入口；桌面、CLI 和打包 worker 应保持相同判定语义。

## 模块地图

| 职责 | 主要文件 |
| --- | --- |
| CLI 分派 | src/reprolab/cli.py、project_cli.py |
| 项目冻结、发现、执行与证据 | project_sessions.py、_project_worker.py |
| 严格单案例与实际 worker | runner.py、_worker.py、case.py |
| 解释器与 pytest 配置 | runtime.py、pytest_profile.py、execution_settings.py |
| 环境准备与原项目可撤销配置 | project_environment.py、project_config.py |
| 选择、刷新与报告复查 | project_selections.py、project_refresh.py |
| 条件比较与多轮记录 | project_history.py、processing_history.py |
| 报告与持久化 | project_report.py、report.py、persistence.py、evidence.py、evidence_bundle.py |
| 桌面协调 | desktop.py、desktop_project.py、desktop_tasks.py |
| 模型连接与 AI | desktop_models.py、desktop_model_cards.py、model_definitions.py、desktop_reasoning.py、model_reasoning.py、model_catalog.py、ai.py、desktop_suite.py、repair_suite.py |
| 专项桌面窗口 | desktop_environment.py、desktop_processing_history.py、desktop_failure.py |
| 构建与发行核对 | packaging/build_macos.py、prepare_release.py、verify_install.py |

项目的正常路径是：原仓库 → 冻结会话 → 实际发现 → 明确选择 → 两版独立副本运行 → 完整保存报告 → 检查／交付／复查。继续编辑原仓库不修改已有输入。刷新另存会话，重放另存报告。

## 修改时保留的契约

- 准备／主体／清理、退出状态和所选测试必须吻合；缺记录、收集错误、超时、跳过与取消不能转为通过。
- 已有失败、改善和新增失败分别记录。未复查项不算已解决；条件变化和实际转绿分别呈现。
- 原始证据不被界面状态、用户说明或来源重新定位改写。
- CLI／桌面复用核心判定；AI 只辅助准备与解释，不产生执行结论。
- 环境只记录已有范围；源码冻结和子进程不构成完整环境锁或安全沙箱。

## 本地验证

安装开发环境见 [CONTRIBUTING](../CONTRIBUTING.md)。核心修改运行相关现有测试；涉及报告、选择或重放时同时覆盖调用方与持久化边界。

```sh
python -m pytest -q tests/test_runner.py tests/test_execution_boundary.py tests/test_result_persistence.py
python -m pytest -q tests/test_project_recheck.py tests/test_project_conditions.py tests/test_processing_history.py
python -m pytest -q tests/test_project_config.py tests/test_release_preparation.py
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_project_config_ui.py tests/test_processing_history_ui.py
```

测试使用受控项目和本地模拟服务，不能替代真实外部项目适配。offscreen 只检查应用内部界面行为，不能代替原生输入和陌生机器安装验收。包改动还应通过[安装包检查](github-release.md)。

## 适合第一批贡献的工作

- 为一个可信真实项目提交可重放接入步骤，记录配置改动和失败原因。
- 为插件、特殊导入布局或已有失败场景补明确的兼容性案例。
- 改善错误信息和首次使用说明，附实际使用入口和预期行为。
- 随具体功能拆分 desktop_project.py 中独立职责，保留当前调用关系与验证，不以全面重写作为前提。

运行变量配置已支持普通值和本机值；更多系统支持与完整环境重建仍有明确边界。先提供真实场景，再决定接口，避免为尚未验证的需求建立通用平台。

## 界面文案

默认界面用功能名称、操作和真实状态说明用途。宣传口号、重复解释按钮的句子和常驻教程不进入主界面；补充解释放到相关控件提示或按需展开的帮助。没有实际内容的最近项目卡片不占位。

精简不能隐藏会影响操作判断的信息：错误、运行进度、选择范围缺失、被筛选隐藏的勾选项，以及普通值／本机值的保存差别仍需呈现。版本与构建阶段放在设置中。
