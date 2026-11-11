# 管理、恢复和验收草稿

v0.7 将 v0.6.2 实测中需要人工处理的“复用已返回响应”变成正式命令。恢复、查看状态、验收都不调用模型；只有 draft generate 会请求模型服务。

## 查看当前进度

    python -m reprolab draft list /path/to/drafts
    python -m reprolab draft status /path/to/drafts/draft-ID

list 只读取直接子目录中的草稿记录，不递归扫描。坏 JSON 和符号链接单独列为无效，不阻止其他条目显示；不导入项目或运行测试。status 显示生成状态、是否具备恢复材料、最近一次通过 draft verify 保存的验收及报告地址。均支持 --json。

生成状态只有 prepared、generation_failed、draft_generated。它们与 verified 等验收结论分开，查看到“草稿已生成”不代表修复验证通过。

## 不重复调用模型，恢复已经收到的响应

    python -m reprolab draft recover /path/to/drafts/draft-ID

v0.7 在完整响应返回后、校验和生成测试文件前保存 response.json 以及 response-record.json。后者将响应、提示词、上下文和案例输入（仓库、固定提交、Python 路径）关联起来，并保存当时的模型来源。

如果模型响应已经返回，但解析或落盘阶段失败，可在问题修复后 recover。命令重新校验同一份响应并保存草稿，不联网，不重新生成测试，不覆盖已经成功生成的草稿。无效测试仍会被拒绝，recover 不是“让错误输出自动变正确”。原始响应按字节保留。

如果返回过程本身超时、截断或无效，没有保存完整响应，则无法恢复。准备材料仍在，可以修复模型配置后明确执行 generate。若之前有一份完整且匹配的响应，即使后续一次网络请求失败，recover 仍可使用最后保存的完整响应；来源以响应记录为准。

更改 response.json、prompt.txt、context.json 或固定版本/解释器信息，会导致核对失败。不要为了绕过校验而手工修改记录；需要新的输入时重新 prepare。这里的指纹用于发现本地材料意外错配，不是第三方签名，也不能证明模型陈述正确。

v0.6 的旧响应没有 response-record.json，不能追补成已经核对的 API 来源。仍可使用原有导入命令，明确标为外部提供的响应：

    python -m reprolab draft import /path/to/drafts/draft-ID --response /path/to/saved-response.json --model "原模型或来源名称"

该导入只适用于尚未成功生成的草稿；已有成功草稿不会被覆盖。

## 审阅后直接验收

    python -m reprolab draft verify /path/to/drafts/draft-ID --output /path/to/reports

先阅读 regression.py 与 explanation.md，确认代码可信、真正调用目标函数、断言与问题对应。verify 复用原有验收器：两个版本各运行同一份测试，最多选择一个测试，保留环境与故障匹配规则。可以用 --python 和 --timeout 覆盖本次环境和时限。

每次报告仍放在独立报告目录，草稿目录另存 verification.json，记录最近一次结果、报告链接、测试及案例指纹。draft.json 只记录生成阶段，不改成 verified。配置/环境错误也记为 inconclusive，不生成虚假的报告链接。

测试、案例文件或案例指向的 Git 提交改变后，status/list 把旧结果标为 stale（历史结果，文件已修改），同时保留上次结论和报告地址。重新验收才能得到针对新文件的结果。查看状态会静态解析 Git 引用，项目测试模式会从 after 提交读取选中测试文件计算指纹；不会导入代码、重算整个源码树、检查解释器依赖变化或重跑测试；最近通过始终只代表那一次运行。

原来的 verify CASE.json 和 batch 流程继续可用；它们不会反向更新草稿目录的 verification.json。要在草稿列表中查看最新验收，使用 draft verify。

## 起草说明随报告保存

v0.7 新生成或新导入的案例包含 draft_review：检查目标、预期行为、假设。它会进入报告 JSON、HTML 和 evidence/case.json，分享/重放证据时不必依赖原草稿目录。

这些说明来自起草时，不会随着手工编辑的测试自动重写。报告继续显示实际测试指纹，并在与原草稿不一致时提示。说明会作类型/大小校验和 HTML 转义，不参与 verified 的判断。旧版本案例没有这个字段也可以照常运行，不自动编造说明。

## 当前本机用法

    reprolab draft list
    reprolab draft status /完整草稿目录
    reprolab draft recover /完整草稿目录
    reprolab draft verify /完整草稿目录

本机入口为 list 自动提供 work/reprolab/drafts，为 draft verify 自动提供 outputs/reprolab/reports。源码仍在唯一 projects/reprolab 仓库，API 配置仍单独位于 work，不进入报告或 Git。
