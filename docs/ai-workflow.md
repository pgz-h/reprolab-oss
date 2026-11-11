# AI 辅助起草回归测试

v0.6 将问题描述、本地 Git 代码、可审阅的 AI 草稿和既有验收连接起来。模型只负责起草；真实运行决定结论。支持 OpenAI 兼容的 Chat Completions API，v0.6.1 增加 Anthropic Messages 协议，配置 URL、Key、模型名和协议。没有配置时仍可离线准备上下文，也可以导入其他模型返回的 JSON。不会用模板代替模型响应。

## 1. 准备问题与代码

    python -m reprolab draft prepare /path/to/project --before BEFORE --after AFTER --issue "描述实际问题及应该发生的行为" --context src/package/module.py --drafts /path/to/work/drafts --python /path/to/env/bin/python

`--issue-file /path/to/issue.txt` 可以代替 `--issue`，文件需为 UTF-8。`--context` 是相对仓库路径，可重复指定。省略时优先选择发生变化、名称与问题有关的 Python 文件，最多 8 个，再加根目录的 README.md、pyproject.toml、pytest.ini、setup.cfg。这是简单排序，不是语义检索；不够相关时应重新 prepare 并明确选择路径。

两个 Git 引用立即固定为完整提交编号；读取已提交的 blob，不切换分支、不读取未提交修改，也不导入项目。准备阶段不联网。选择 Python 环境时会运行该解释器检查 Python/pytest 可用性，保留虚拟环境路径，不安装依赖。

每次新建独立 `draft-时间-编号` 目录，不能位于被测仓库内部。问题最多 16 KiB，上下文最多 12 个文件、每个版本每个文件 24 KiB、总计 96 KiB。仅支持 Python/Markdown/reStructuredText 及上述配置文件；排除 .git、环境、缓存、构建目录、.env* 和非普通文件。自动选择遇到过大或非 UTF-8 文件会记录省略原因，显式选择则报错。`.env*` 排除不等于全面秘密扫描，调用模型前应查看 prompt.txt。

## 2. 配置模型服务

桌面用户优先使用[模型连接与多选](model-connections.md)，无需手写配置文件。以下为 CLI 配置方式。

默认协议要求服务支持 `POST /chat/completions`、Bearer Key、文本 messages 和非流式文本结果。基地址通常包含 `/v1`，例如 `https://api.openai.com/v1`；也可填写完整的 `/chat/completions` 地址。不会自动猜测模型名。

在源码仓库之外创建 `ai.local.json`：

```json
{
  "base_url": "https://your-provider.example/v1",
  "model": "服务商提供的准确模型名",
  "api_key_env": "REPROLAB_API_KEY",
  "json_mode": true
}
```

将 Key 放到本机环境变量 `REPROLAB_API_KEY`。也可在这个**仅限本机、不提交 Git**的文件里填写 `"api_key": "你的密钥"`；建议把文件权限设为仅自己可读写（macOS/Linux: `chmod 600 ai.local.json`）。不要把 Key 发进聊天、工单或截图。示例里的 URL 和模型名不能直接使用。

也可不使用文件，设置 `REPROLAB_API_BASE_URL`、`REPROLAB_AI_MODEL`、`REPROLAB_API_KEY` 三个环境变量。URL、模型优先级：命令参数 > 配置文件 > 环境变量。显式传入 `--api-key-env` 时只使用指定变量；如果该变量不存在，不回退到配置中的旧 Key。不提供 `--api-key` 明文命令参数。

未指定 `--api-key-env` 时，未声明凭据来源的旧配置优先读取配置的环境变量（默认 REPROLAB_API_KEY），其次读取本机配置的 api_key；桌面新保存的配置会明确记录使用文件中的 Key 还是指定环境变量。更换已保存连接的服务来源时，桌面会要求明确选择新凭据或确认复用，详见 [AI 项目验收助手](ai-project-acceptance.md)。

默认发送 `response_format: {"type":"json_object"}`。服务不支持这个字段时，在配置中设置 `"json_mode": false`，仍要求模型按提示返回纯 JSON，程序继续严格检查结构。不是所有声称兼容的服务都支持相同模型或协议；v0.16另支持Responses API。该接口和现有接口均按完整非流式文本处理。

### Anthropic Messages 协议（v0.6.1）

在本机配置中设置 `"protocol": "anthropic"`，或用命令参数 `--protocol anthropic`。未指定协议仍使用原来的 OpenAI Chat Completions，兼容已有配置。

智谱 Anthropic 接口示例（不含密钥）：

```json
{
  "base_url": "https://open.bigmodel.cn/api/anthropic",
  "protocol": "anthropic",
  "auth_type": "bearer",
  "model": "glm-5.3-flash",
  "api_key_env": "REPROLAB_API_KEY",
  "max_tokens": 8192
}
```

Messages 请求发送到 `/v1/messages`，使用顶层 system、用户 messages 和必填 max_tokens，不携带 OpenAI 专有的 response_format。基地址可包含 `/v1`，也可填写完整 `/v1/messages`，程序不会重复追加。

`auth_type` 支持 `api-key`（x-api-key 请求头）和 `bearer`（Authorization 请求头）。Anthropic 协议默认 api-key；智谱文档中的 AUTH_TOKEN 方式使用 bearer。请求包含 `anthropic-version: 2023-06-01`。max_tokens 默认 8192，允许 1–32768，具体模型仍可能有更低上限；这是最大生成量，不代表每次都会消耗这么多。

v0.6.2 对 max_tokens 截断、工具调用、暂停和拒绝返回具体错误提示，不保存为可运行草稿。对于始终思考的模型，缩小 max_tokens 可能使正文尚未输出就被截断；需结合服务返回的原因调整输出额度和等待时间，不能把超时直接归因于 Key 或协议错误。

只接受正常 `end_turn` 完成的非空文本块，忽略单独的 thinking 块；截断、工具调用或拒绝响应都不能成为有效草稿。模型用量按服务返回的 input_tokens/output_tokens 保存。json_mode 仅影响 OpenAI 协议。

这些协议选项决定请求格式，不决定服务商的账号或套餐适用范围。智谱配置依据：[官方 Anthropic 接入示例](https://docs.bigmodel.cn/cn/coding-plan/tool/claude)。

使用远程服务需要 HTTPS，本机 localhost/127.0.0.1/::1 可用 HTTP。不跟随重定向，不自动重试，不把远程错误正文写进日志。调用会把 `prompt.txt` 中的问题和选定代码发送给你配置的服务，产生的费用由相应 API 账号承担，与本聊天是否有子智能体无关。ReproLab 不使用本机 Codex 登录，也不自带模型账号。

## 3. 生成草稿

    python -m reprolab draft generate /path/to/work/drafts/draft-ID --config /path/to/work/ai.local.json

可以用 `--base-url`、`--model` 覆盖地址和模型，`--protocol` 选择 `openai`、`responses` 或 `anthropic`，`--ai-timeout` 设置连接/读取超时（0.1–600 秒，默认 180）。一次命令请求一次，不自动改写测试来追求通过。没有配置、认证失败、超时、截断响应、拒绝回答或无效测试都会保留准备材料及错误状态，退出码为 2，不生成虚假案例。

草稿目录：

```text
draft-ID/
  issue.txt                 问题描述
  context.json              两个提交的选定代码与指纹
  prompt.txt                实际发送的提示词
  response.schema.json      模型应返回的格式
  draft.json                准备/生成状态与来源记录
  provider.json             服务地址、模型、用量（不含 Key）
  response.json             原始模型文本响应，失败时便于检查
  response-record.json      v0.7 响应与输入的关联记录，用于离线恢复
  verification.json         使用 draft verify 后保存的最近验收记录
  regression.py             待审阅的单测试草稿
  explanation.md            检查目标、预期行为、假设、预期故障
  cases/case.json           可直接交给 verify / cases / batch
```

生成前只做静态检查，不导入或执行测试。支持纯 JSON 或仅由一个 JSON 代码围栏包裹的对象，保留原始响应；夹杂额外说明的输出仍拒绝。支持模块顶层 try/except 中的导入回退，不放宽到仅在函数内导入。首版要求一个无装饰器的顶层同步 `test_` 函数；必须在测试主体中调用从已提供上下文的项目模块顶层导入的对象。拒绝语法错误、多测试、装饰器、显式 skip/xfail，以及只有标准库调用的假测试。采用保守导入规则，动态导入、测试内部才导入、仅通过辅助函数间接调用等形式可能需要手工改成直接调用。

静态检查不能证明函数确实执行到、断言正确或没有硬编码。请审阅生成的测试确实对应问题、没有复制被测实现或按版本制造结果。项目名称与标准库同名等特殊布局也需要额外审阅。当前只运行可信代码，进程不是安全沙箱。

生成测试采用**独立测试模式**：可导入项目根目录或 src 布局的代码，不加载项目 conftest、pytest 配置或项目 fixture；可以使用 pytest 内置 fixture。已有 `--project-test` 流程保持原语义：after 的同一测试覆盖到两个临时副本，原仓库和源码证据不变，addopts 和自动插件发现仍禁用。

## 4. 审阅后验收

    python -m reprolab verify /path/to/work/drafts/draft-ID/cases/case.json --output /path/to/reports
    python -m reprolab cases /path/to/work/drafts/draft-ID/cases
    python -m reprolab batch /path/to/work/drafts/draft-ID/cases --output /path/to/reports

最终仍只能选中一个测试。可以手工编辑 regression.py 和案例的 expected_failure，但必须在验收前声明正确故障条件；不能为了通过而删掉有效断言。报告保留起草模型信息和原始测试指纹，编辑后会说明实际执行文件与原草稿不同。报告及 evidence/case.json 保存生成来源；外部导入的模型名称只是导入者声明。证据回放仍使用独立快照，不需要模型配置或 Key。

生成不会覆盖已有成功草稿。模型调用或格式检查失败后可修正配置/外部响应，再对同一准备目录重试；保存中途失败会回滚本次生成文件，原始响应和错误信息保留。同一个草稿同时只允许一次生成。再次生成新的候选请重新 prepare。

## 状态与退出码

| 所在阶段 | 状态 | 含义 | 退出码 |
|---|---|---|---|
| 准备 | prepared | 尚未调用模型或运行验收 | 0 |
| 生成 | draft_generated | 草稿已生成，未验收 | 0 |
| 生成 | generation_failed | 配置/调用/响应/保存失败，无验收结论 | 2 |
| 验收 | not_reproduced | 旧版也通过，未复现问题 | 1 |
| 验收 | inconclusive | 环境、收集、故障不匹配等，无法验收 | 2 |
| 验收 | still_failing | 旧版复现，新版仍未通过 | 1 |
| 验收 | verified | 旧版匹配预声明故障，新版同一测试通过 | 0 |

`draft.json` 记录生成阶段，不随着验收更新；验收结果以每次新建的报告为准。v0.7 的 draft verify 另外保存 verification.json，draft status 可以显示最近结果和文件变动提示。退出码 0 在生成命令中只表示草稿生成成功。JSON 模式下的命令错误继续使用既有 `inconclusive` 错误信封；草稿目录中记录更具体的 generation_failed。

## 无 API 时导入真实模型响应

将 prompt.txt 提供给你选择的模型，按 response.schema.json 返回 JSON，保存为外部文件，再执行：

    python -m reprolab draft import /path/to/work/drafts/draft-ID --response /path/to/response.json --model "模型或来源名称"

这个命令不调用模型；它校验并保存你提供的响应，来源标为 external，不能当作 ReproLab 已自动接通该模型的证据。

## 当前本机入口

    reprolab draft prepare /path/to/project --before BEFORE --after AFTER --issue "问题描述"

本机 run.sh 自动给 prepare 使用 `work/reprolab/drafts`，给 generate 使用存在的 `work/reprolab/ai.local.json`，并继续把 verify/batch 报告放到 outputs/reprolab/reports。显式参数优先。

接口依据：[OpenAI Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。仅验证兼容协议不代表任何第三方服务可用；真实模型接通情况应查阅本次版本验收记录。

## 已完成的真实模型教学验证

v0.6.2 使用智谱 Anthropic 接口实际生成并验收了零值均值案例：旧版返回 3.0，新版返回 2.0，同一份未经手工修改的 AI 测试得到 verified。样例是专门构造的教学代码，不是外部项目采用证明。过程中的超时、格式兼容修复和证据位置见 [真实 AI 教学案例](ai-teaching-case.md)。

草稿恢复、列表、状态和报告说明保存见 [v0.7 草稿管理](draft-management.md)。

## v0.16桌面连接与多项计划

可以在设置页通过连接模型表单创建配置。protocol增加responses，使用Responses API的instructions、input和max_output_tokens。多项检查与辅助结果解释见 [AI项目验收助手](ai-project-acceptance.md)。
