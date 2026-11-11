# CLI、报告与 CI 接入

CLI 与桌面共用执行和证据核心；不需要安装桌面依赖或配置模型。当前 Alpha 的字段仍可能扩展。消费者应检查 kind 和 schema_version，保留原 JSON，允许未知的附加字段；遇到未知状态或不支持的 schema，停止给出通过结论。

## 直接比较与分步检查

- `reprolab project review` 一次冻结两版、发现并运行所选测试，输出与 `project run` 相同的报告 JSON 和退出码。
- `reprolab project prepare` 固定 Git 基线与候选，返回具体会话目录 `directory`；不执行项目测试。
- `reprolab project run` 明确执行检查。没有可用的发现结果时先实际收集，再执行同一份所选测试。需要先浏览测试清单时，单独执行 `project collect`。

`prepare` 会检查所选解释器；`review`、`collect` 和 `run` 会执行可信项目代码。普通项目未提供选择时，仅在发现完整时使用已发现项，范围限于 1–2000 项；继承选择的缺失项不会被静默丢弃。自动化建议明确传入 `--test` 或 `--selection-file`。

默认发现遵循冻结项目的 pytest `testpaths`（未设置时使用 pytest 默认范围），仍清空 `addopts` 并保留显式插件加载规则。`project collect --path` 将一个相对测试文件或目录传给 pytest 并保存到会话。此后发现、刷新和复查沿用该范围，除非再次指定路径或用 `--default-scope` 恢复默认。明确执行的测试 ID 须来自实际发现清单。

发现有收集错误或超过 10000 项截断时，`project collect` 返回 2，JSON 保留 `partial`／`truncated`、可用 `tests` 和诊断。`status: collected` 只表示存在可用项，不代表清单完整。`project run` 不会隐式运行这份不完整范围；用户可修复后重新发现，或用 `--test`／`--selection-file` 明确选择可用项。报告保留发现限制，结论只覆盖实际选择。

发现记录新增 `worker_status`、`exit_code`、`timed_out`、`worker_record_complete` 和 `diagnosis`，不改变原 schema 或移除已有字段；旧记录缺少这些字段时只能给出保守诊断。

## 在 CI 中比较两个提交

先用项目原有方式准备依赖，将仓库、解释器和选择清单换成实际路径。选择清单支持 JSON 或每行一个完整测试 ID 的文本。

```sh
reprolab project review /path/to/repository --before BASE --after CANDIDATE \
  --python /path/to/project-env/bin/python --selection-file /path/to/selection.txt \
  --output /tmp/reprolab-review --progress > /tmp/reprolab-result.json
```

会话与报告分别保存在输出根目录下的 `sessions` 和 `reports`，输出根目录应位于被测仓库外。报告 JSON 提供 `project_directory` 和 `report_dir`。如果已冻结源码但未形成报告，stderr 的错误 JSON 会附带 `project_directory`，可用 `project show` 查看已保存会话，或调整后用 `project collect`、`project run` 继续。

本地检查未提交改动时可用 `--before HEAD --working-tree`。`--path` 可缩小发现范围，`--test` 可重复指定精确测试 ID。需要在执行前查看清单时，用 `project prepare` 创建会话，从其 JSON 的 `directory` 取得后续命令所需的目录。多轮检查可使用[报告复查](report-recheck.md)沿用最初固定基线。

## 输出和退出码

`project` 命令的正常结果写入 stdout，格式为单个 JSON 对象。`--progress` 将实时进度写入 stderr，不混入最终 JSON。参数错误、异常和没有形成报告的中断也可能在 stderr 返回错误 JSON；启用进度时 stderr 是混合诊断流，不应当整体解析成 JSON。

| 执行结果 | 退出码 | 自动化处理 |
| --- | --- | --- |
| project review／run 的全局 regression_passed | 0 | 本次范围两版通过或改善，且执行记录完整 |
| 全局 regression_detected | 1 | 存在新增失败；查看逐项状态，同时保留其他受阻信息 |
| attention、cancelled、无可用结果、参数／环境错误 | 2 | 不作通过结论，保留材料并检查原因 |
| prepare、show 等非验收操作成功；collect 获得完整可用清单 | 0 | 只表示该操作成功，不表示修复通过 |
| collect 失败、无测试、部分发现或清单截断 | 2 | 检查诊断和范围；部分清单中的可用测试仍可明确选择 |
| project check 返回 consistent | 0 | 材料相互一致；报告本身仍可能记录失败 |

以上退出码针对 project 命令；严格案例 verify 有自己的结论状态，不能把两个入口的字符串混用。CI 应保留原退出码，不用 `|| true` 隐藏检查失败。即使返回 1 或 2，也可能有完整报告，应保存 result.json 指向的 report_dir。

## 程序读取报告

项目报告的 kind 为 project_regression，schema_version 当前为 1。常用字段：

| 字段 | 含义 |
| --- | --- |
| status、counts、entries | 总体结果、分类数量和逐项结果 |
| selected_tests、shared_tests | 本次所选精确 ID 和实际共享测试文件指纹 |
| collection.collection_path | 实际发现范围的相对路径，空字符串表示 pytest 默认范围，旧报告可缺省 |
| before_commit、after_commit、source_sha256 | 基线／候选来源与冻结源码指纹 |
| before、after | 两版阶段记录、日志、退出情况及中断观察 |
| runtime、environment、pytest_profile、timeout | 已记录的解释器、发行包元数据、插件配置与时限 |
| review_source、refresh_origin | 明确保存的复查／刷新来源；旧报告可能没有 |
| persistence | 保存材料的完整性记录 |

先以 `project check REPORT_DIR` 校验，再读取报告。report.json 是正式报告标记；operation.json 或单独 HTML 不能替代完成的报告。CLI 返回值可以带本机操作提示，完整证据以保存的报告目录为准。

## 三个不同的保证

1. **材料可核对**：指纹和结构检查帮助发现内容变化，不证明作者身份。
2. **检查可重放**：保存输入可在合适环境再次执行，重放另存新记录。
3. **结果可重复**：仍取决于依赖、系统库、外部服务、时间、随机性和测试自身。版本清单不是完整环境锁。

两版共享候选的所选测试文件及声明的 pytest 插件／导入方式，各自保留源码、项目配置和 fixture。测试文件相同并不代表所有执行条件相同。自动加载插件和项目 addopts 被关闭；可明确声明普通值和必须从本机提供的变量，见[运行变量](runtime-variables.md)。runtime_environment 记录普通值和本机名称，不保存本机值；项目重放预览的 required_local_variables 列出需重新提供的名称。

## CI 的使用范围

在已有可信项目的 CI 中，先按项目原有方式准备解释器和依赖，再调用上述流程；上传 result.json 和报告目录时遵循项目的数据公开范围。ReproLab 本身不负责部署数据库、启动服务或提供安全沙箱。

仓库自带工作流用于验证 ReproLab 自身：两组 Python／pytest 核心检查、Python 包构建、干净环境安装后的 CLI 检查。它不等同于每个接入项目的测试流程，也没有自动发布 Release。

## 复查输出中的自动进展

`project recheck` 产生报告后，输出附加 `review_progress`。桌面与 CLI 使用同一证据校验和比较函数：`status` 为 `compared` 时，`comparison` 保留完整 schema 3 比较；`summary`、`scope_summary` 提供简短进展与范围。来源丢失或不一致返回 `unavailable`，停止核对返回 `cancelled`，未绑定来源返回 `not_applicable`；这些情况的 `comparison` 均为 `null`，不能推断已解决数量。

这是读取保存证据得出的派生信息，不写回 `report.json`。命令退出码仍取决于本次真实执行报告；跨轮核对不可用不会改写执行结果。消费者应分别读取执行状态和 `review_progress.status`，不能只凭一个退出码推断跨轮修复已确认。尚未生成报告、需先核对测试范围时不返回此字段。手动 `project compare` 入口保持兼容。


## 已有环境的静态检查与继续使用

`environment inspect` 保留原 `status` 和退出码：`review`／`attention` 返回 2，不能当作检查通过。新增 `reuse` 对象说明桌面采用已有解释器的条件：`status` 为 `ready`、`review`（需核对未确认声明）或 `blocked`（存在已知环境／配置问题），`reasons` 保留待处理原因。它不是测试结论，也不是自动准备环境的许可。

CLI 原有显式 `--python` 用法不变；确认已有环境后可用同一个会话路径重新 `project collect`，无需重新冻结源码。采用解释器不自动启用 `addopts` 或环境内其他插件；收集失败、部分发现、截断与测试执行仍按各自原契约判断。
