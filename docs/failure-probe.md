# 故障识别引导

v0.15帮助用户选择相关测试并获得实际故障信息，再保存为正式验收条件。

## 桌面操作

1. 在新建案例窗口选择项目、基线和修复后版本，也可勾选当前工作区。
2. 默认列表优先显示本次改动相关的测试。推荐原因在条目提示中显示，参数化测试可使用实际收集入口选择具体实例。
3. 点击“试运行旧版，识别故障”。应用固定两份代码与修复后测试，在旧版独立副本实际运行一个测试。
4. 查看实际错误，确认它对应要修复的问题后点击“确认并采用此故障”。异常类型和消息自动填入。
5. 需要调整消息时展开手工条件，保留能识别问题的稳定片段，然后保存案例并开始正式验收。

采用后的案例使用试运行时冻结的输入，后续编辑原仓库不会改变它。切换仓库、版本、测试或Python会使旧观察结果失效。关闭正在试运行的窗口会请求取消并清理worker。

## 结果解释

完整测试主体失败时可产生failure_observed，并提供故障条件建议。旧版通过显示before_passed。收集、fixture准备或清理失败、缺依赖、跳过、超时及记录不完整会显示相应诊断。异常消息为空时需要进一步审阅测试。

用户确认发生在正式验收前，正式验收重新运行旧版和新版并核对条件。verified仍由正式两版结果决定。推荐依据文件改动和命名关系，用户需确认测试覆盖目标问题。

临时路径、随机值等内容可能在每次运行时变化，消息条件应采用实际错误中稳定的片段。自动建议最多取前4000字符，完整观察保存在试运行记录和日志中。

## CLI

    reprolab list-tests /path/to/repo --ref AFTER --before BEFORE
    reprolab probe-failure /path/to/repo --before BEFORE --after AFTER --project-test tests/test_fix.py::test_fix --python /path/to/env/bin/python --output /path/to/work/probes --json
    reprolab case-from-probe /path/to/work/probes/probe-ID /path/to/cases/fix.json --confirm-failure
    reprolab verify /path/to/cases/fix.json --output /path/to/reports

当前工作区使用--working-tree替代--after。case-from-probe支持--failure-message指定已观察到的稳定消息片段。缺少明确确认时会拒绝创建案例。

## 证据与边界

试运行目录保存probe.json、before.log、before.events.json及inputs源码。桌面默认放在案例目录旁的probes目录。取消或异常中断会记录过程状态，建议为空。

正式案例保存failure_origin，包括试运行编号与记录指纹、确认的故障片段、精确测试目标、源码和测试指纹。手动更改目标或故障条件导致与确认来源不一致时，会要求重新识别与确认。正式报告与证据包保留确认摘要和正式两版运行证据，原始试运行日志另保存在probes目录。

所有实际运行都要求可信本地代码，子进程拥有当前用户权限。使用原有Python/pytest环境、单测试边界、after同一测试覆盖两版临时副本及现有依赖策略。真实项目通常需要选择已准备好的外部Python环境。
