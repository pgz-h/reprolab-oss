# AI项目验收助手

ReproLab 把模型连接、检查规划、本地实际验收和结果解读放在同一工作流中。

## 连接模型

在设置或 AI 项目验收中选择“选择 / 连接模型…”。常用服务预填地址与协议；填写 Key 后获取目录，勾选多个模型，更新列表并选择默认模型，可测试并保存。自定义网关保留地址与协议字段。保存后在 AI 入口直接切换已添加模型，不必重填连接。

模型目录与当前请求分开：可保存多个模型，每次生成使用一个明确模型。目录获取、模型切换不会发送项目材料，连接测试只发送固定公开短句。已有测试无需模型。具体步骤、凭据保存方式及错误处理见[模型连接指南](model-connections.md)。

## 从问题到多项检查

1. 在案例验收页点击“AI项目验收”。
2. 选择仓库、修复前基线及修复后版本，也可使用当前工作区。
3. 写出问题、预期行为和希望保护的相关功能，选择项目Python环境。
4. 准备验收计划，查看将发送的完整有限上下文，选择模型并明确发送。
5. 审阅模型生成的检查用途、代码和假设，勾选本次检查后开始项目验收。
6. 在批次窗口查看真实结果，需要时只重跑需处理项。报告页或计划窗口可以预览并请求AI解读结果。

一个计划含1至20项检查，至少一个目标修复项。每个检查执行一个明确测试。目标修复要求旧版出现预声明故障、新版同一测试通过。回归保护比较同一检查在两版的实际表现。

| 旧版 | 新版 | 回归检查状态 |
|---|---|---|
| 通过 | 通过 | regression_passed，回归通过 |
| 通过 | 失败 | regression_detected，新增失败 |
| 失败 | 失败 | baseline_failure，两版均失败，需比较异常 |
| 失败 | 通过 | improved，表现改善 |
| 受阻或未完成 | 任意 | inconclusive或取消状态 |

整体verified需要所选目标修复全部验证通过，并且回归项全部为通过或改善。已有失败、受阻或未完成项使总览显示需要处理。只重跑回归项时使用checks_passed表示这批所选检查通过。

## 修正检查与离线恢复（dev2）

审阅清单会显示检查用途、精确测试目标、目标修复的异常类型和消息片段。发现模型选择或假设有误时，可点击“编辑检查 / 另存审阅版本…”。逐项修改标题、用途、预期行为、假设、独立测试代码或已有项目测试目标，目标修复还可修改预声明异常。可以添加和移除检查；完整计划仍需 1–20 项和至少一个目标修复项。参数化测试应填写具体实例。

保存会重新校验检查，在原计划旁复制固定输入并生成独立版本，记录父计划 ID、父计划摘要和修订时间。原计划及已有报告保留；新版本清除旧结果入口，需要重新实际验收。保存与修订不联网、不运行测试。新报告明确区分人工审阅来源和模型来源。无效输入会保留编辑表单；没有变化的计划不会重复另存。

生成前后都会核对固定源码、上下文及已记录的普通文件权限；模型请求期间输入变化时，不发布可运行计划。完整响应与来源保存为 `response.json` 和 `response-record.json`，绑定当次输入。写入计划失败后，窗口会立即启用“恢复已保存响应”并保留错误说明；进程中断留下部分产物时，也可之后重新打开目录恢复，或用 CLI：

    reprolab suite recover /path/to/suite-ID

恢复不会调用模型，只使用能与输入和来源记录匹配的完整响应。未完成的 tests/cases/plan.json 会先移到 `.incomplete-*` 目录保留，恢复失败时还原。已经完成的计划不会覆盖。旧版本只保存了响应、没有可核对来源记录时，须用 `suite import --response ... --model ...` 明确声明来源；无效模型输出仍需修正后导入或重新生成。

CLI 可以导出可编辑 JSON，再创建独立修订；导出不会覆盖已有文件：

    reprolab suite export-plan /path/to/suite-ID --output /path/to/review-plan.json
    reprolab suite revise /path/to/suite-ID --plan /path/to/review-plan.json --output /path/to/reviewed-suites
    reprolab suite run /path/to/reviewed-suites/new-suite-ID --output /path/to/reports

导出只含 title、overview、checks 及可审阅字段；不要手工修改原计划的摘要或案例文件。修订版使用新的计划 ID，可以移动后独立打开、运行和导出实际执行证据。权限信息缺失的旧计划及其修订继续提示历史检查范围。

## 计划内旧版试运行（dev3）

已有计划的故障条件不够确定时，选中检查后“试运行所选检查的旧版”。工具从该计划的固定输入复制源码和同一份测试，使用所选 Python 与正式 worker，只执行旧版。支持生成的独立测试及精确项目测试目标；每次仍只选中一个测试。运行可取消，关闭窗口会等待清理。此步骤不调用模型。

界面展示实际阶段诊断、异常、测试标识、解释器、日志节选与完整材料位置。只有实际测试主体失败、不是已识别的缺模块错误且消息非空时才提供条件建议；旧版通过、收集/fixture/清理错误、跳过、xfail、超时和取消均不可作为可采用故障。

目标修复项需先明确确认观察对应问题，再将条件带入既有编辑窗口。预填仍需审阅，用户可以挑选稳定消息片段；保存会另存独立计划，原版保留。正式验收继续重新执行两版。切换当前检查、Python 或计划会清除观察，采用前还会重新校验来源、实际事件和日志。独立观察用于准备检查，不是修复通过结论。

确认绑定到当时展示的记录指纹；之后即使路径被另一份有效观察替换，也需要重新查看并确认。重开时从校验后的原始事件生成诊断，从真实日志生成节选，缓存摘要不作为故障依据。取消或保存未完成的记录，即使保留完整 worker 输出，也不会升级为可采用故障。

试运行目录包含 `inputs/before`、`inputs/after`、`inputs/regression.py`、`before.log`、`before.events.json` 和 `probe.json`，绑定来源计划、案例、测试及源码/权限。旧计划缺失冻结时权限信息的提醒保留；新试运行实际记录到的权限仍会与当前输入核对。目录移动后可以重开，但必须对应同一份来源计划。

    reprolab suite probe /path/to/suite-ID --check repair --output /path/to/probes --python /path/to/env/bin/python
    reprolab suite probe-show /path/to/suite-ID --probe /path/to/probes/probe-ID

CLI 的试运行时限默认 15 秒，允许 0.1–300 秒。观察到可用主体故障时退出码为 0，其余状态为 2；0 只表示观察完成并提供故障建议。`probe-show` 只读取并校验本地记录，不执行代码。桌面的“打开旧版观察记录…”提供同一功能。

旧观察证据独立保存在试运行目录。正式报告记录人工审阅版本及正式两版运行证据，不宣称已将独立观察目录打包进报告。

## 环境检查与准备

“检查 / 准备项目环境”读取所选Python的版本元数据和项目静态依赖。支持pyproject.toml中的project.dependencies、requires-python，以及受限项目内requirements.txt引用。候选.venv/venv/env只列路径，选择后才检查解释器。

需要新环境时选择完整外部Python及项目外的新目录，先预览，再明确创建。工具从公开PyPI安装预览中可解析的具名wheel依赖，保留准备记录。动态安装脚本、URL、editable或额外索引声明需要人工处理。应用内置分派器用于内置运行，创建虚拟环境应选择完整Python。

ready表示所检查的版本声明满足要求，实际导入、fixture和测试结果继续由正式运行检查。

## 保存与解读

计划目录包含固定inputs、有限context.json、prompt.txt、模型response.json、plan.json和独立案例。可再次打开计划并继续使用保存的环境，也可明确改选环境。源码、测试或条件被改动时会要求重新准备。

生成的检查需要审阅。已有项目test_target支持原项目配置和fixture，生成的独立测试遵循原有保守结构约束。上下文最多12个文本文件，单版本24KiB，总计96KiB，默认优先选择变更Python文件及项目配置。

AI解读只发送预览中显示的实际状态、异常片段和检查说明。它保存在独立explanation.json中，保留来源摘要和模型信息。报告更新时要求重新预览。正式状态继续保存在原始报告和汇总中。

## CLI

    reprolab suite prepare /path/to/repo --before BEFORE --after AFTER --issue '问题与期望' --output /path/to/work/suites --python /path/to/env/bin/python
    reprolab suite generate /path/to/suite-ID --config /path/to/model.json
    reprolab suite show /path/to/suite-ID
    reprolab suite run /path/to/suite-ID --output /path/to/reports
    reprolab suite explain /path/to/batch-report
    reprolab suite explain /path/to/ai-explanation-ID --config /path/to/model.json --send

suite import可导入外部模型JSON响应。prepare支持--working-tree和--context，run支持重复--check选择检查。生成、实际验收和结果解读是独立操作。

    reprolab environment inspect /path/to/project --python /path/to/env/bin/python --json
    reprolab environment plan /path/to/project /path/to/new-env --python /path/to/full/python --output /path/to/plan.json
    reprolab environment create /path/to/plan.json

环境创建前会重新核对依赖声明和计划，取消及失败保留未完成记录。

## 接入设计参考

模型协议、配置和认证分层的设计参考了DeepSeek Harness的多供应商适配层及ZCode的Provider与账号分离结构。实现为本项目的Python接口与Qt窗口。供应商授权能力以各服务官方接入要求为准。

- https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm-pi-ai/src/discovery.ts
- https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/client/ui-settings-models/src/client/ModelListEditor.tsx
- https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/docs/usage/03-config.md
- https://developers.openai.com/siwc/token-sharing-open-source
