# 审核、迁移与可复核交接

这些入口适用于可信本地输入。只读核对使用保存的证据。启动项目或服务的入口需要显式运行。

## 分段证据、范围与取证

- `project pack-series REPORT --output SERIES` 沿报告来源链收集普通分段。按需重复 `--source-directory` 补充已移动的来源。接收方用 `project check-series SERIES` 核对，用 `project unpack-series SERIES RESTORED` 还原。也可在桌面导入时选择 `series.json`。
- 每份报告分别压缩，每段默认 64 MiB。可调 `--part-mib`。分段允许单份报告最多 2 GiB 解压材料，且最多 2000 轮。普通单 ZIP 仍保留其读取限制。所有段和复查引用均验证后才发布还原目录。
- 大仓库可在 `prepare` 或 `review` 重复指定 `--source-path src/my_app --source-path tests --source-path pyproject.toml`。它们是显式冻结范围。所需配置和辅助文件须纳入范围。被排除的已跟踪改动计入范围提示。形同却只排除改动的比较仍不能通过。
- Git 导入检查本机文件系统是否会折叠不同的文件或目录。遇到碰撞会拒绝发布固定树。普通权限和源码分别有指纹。

## 固定执行条件与反复观察

```sh
reprolab project review REPOSITORY --before BASE --after CANDIDATE --output RUNS \
  --freeze-clock 2026-01-02T12:00:00+00:00 --timezone UTC --locale C
reprolab project replay REPORT --confirm-run --network-isolated --output REPLAYS
reprolab project first-attempts REPORT1 REPORT2
reprolab project check-performance R0 R1 R2 R3 R4 R5 --warmup 1 --max-ratio 1.25 --min-delta-ms 2
```

pytest 固定时钟在发现和执行工作进程中应用，覆盖常用 Python 墙钟 API。启动后的单调钟保持真实，子进程、原生库及外部服务仍有独立的时间来源。重放沿用已记录设置。区域须由所选 Python 实际支持。

`--network-isolated` 在 macOS 通过系统隔离入口启动重放，保存系统拒绝 TCP bind/connect 的探针记录。使用记录了隔离的报告再度执行时，也需明确选用隔离。Linux 使用 unshare 并验证不同的内核网络命名空间、仅回环接口和空的外部 IPv4 路由。Linux 托管执行器已实际验证工作进程无法访问命名空间外的监听器。系统禁止建立所需命名空间、探针失败或无受支持机制时受阻。npm 的离线缓存选项单独表示安装行为。

Windows 可使用本机 Docker Windows 容器。先在可信镜像中安装与宿主相同版本的 ReproLab、Python 和项目测试依赖，以 `--network none` 启动独立容器，然后将可信证据放入容器。例如在 Windows 宿主终端执行：

```powershell
reprolab project replay C:\evidence\project.zip --confirm-run --network-isolated --windows-container reprolab-offline --verify-result --output C:\results
```

报告、工具、缓存、输出和回执路径均在容器内解析，可以用 Docker 复制收件与结果。宿主经本机命名管道核对运行状态、Windows 平台、容器 ID 和网络模式，再在容器内检查活动非回环接口。镜像须提供 PATH 上的 Python、系统 Windows PowerShell 和 ReproLab；交接时先核对证据能否在 Windows 上保留原有文件权限。使用专供本次验收的容器，运行期间让网络配置保持固定；此流程面向可信代码。


新 pytest 报告可记录工作进程 CPU 和峰值 RSS，显示已观察的系统调用错误和终止信号。进程收到信号时原因保持未知。不同机器上的内核限额与子进程资源须另行鉴别。

`first-attempts` 显示已观察的首次尝试，明确分母和未知项。Go 以选择的顶层测试为单位。Vitest 有 repeat 时不会把最后的结果当成首次结果。新格式重放会核对已记录的错误阶段、类型和消息指纹，多轮比较也会保留这些差异。指纹相同的观察仍需要维护者判断是否为同一业务原因。

配对性能核对要求至少五份可比的 pytest call 测量，先排除声明的预热轮。使用配对差值、倍率与下四分位判定。记录条件改变或结果噪声较大时保留受阻。系统负载及顺序效应仍要结合实际工况。

## PR、批准与工作流身份

```sh
reprolab project review REPO --github-event event.json --pr-mode merge \
  --merge-commit MERGE_SHA --output RUNS
reprolab project summary REPORT --acceptance-plan plan.json \
  --require-approval approval.json --approval-key reviewer.pem --expect-reviewer NAME
```

`head` 模式对照唯一 merge base 与 PR head，`merge` 模式对照目标版本及其实际合并结果。合并结果的两个父提交必须与事件吻合。Git 对象由调用方提前取得。报告保存绑定使用的事件指纹。

签名批准绑定同一验收计划、报告和预期审查者。签名缺失或不匹配会受阻，已确认测试失败保留。桌面修复验收可选择同样的签名、公钥和身份。

`project check-github-attestation EVIDENCE.zip --bundle BUNDLE.jsonl --trusted-root ROOT.jsonl --repo OWNER/REPO --workflow OWNER/REPO/.github/workflows/build.yml --source-digest SHA` 委托 GitHub CLI 核对实际 ZIP、托管签署工作流和来源。期望值与可信根须独立取得。参见 [GitHub CLI 核对说明](https://cli.github.com/manual/gh_attestation_verify)。

## 服务版本、数据库和下游制品

`project service-matrix RECIPE --confirm-run --output EVIDENCE` 读取版本 1 JSON。消费者定义 `repository`、`before`、`after`、`tests`，每个服务定义 `name`、`repository`、`refs`、`entry`、`port_env`。仓库可相对于方案文件。服务从固定 Git 版本启动，通过环境获得 `REPROLAB_SERVICE_PORT` 和 `REPROLAB_READY_FILE`，准备好后写标记并监听回环地址。入口须允许在同端口重启。两版消费者之前分别重启服务，从新的源码及状态副本开始。消费者须声明 `port_env` 为本机变量。用 `check-service-matrix EVIDENCE` 离线核对各服务与报告绑定。

`project sqlite-matrix RECIPE --confirm-run --output EVIDENCE` 的 JSON 包含 `schema_version: 1`、`paths` 数组和 `probes` 对象。每条路径含 `name`、`seed`、`before`、`after`。初始数据库及两版 SQL 文件使用方案目录内的路径。两版在独立数据库上执行各自步骤，再用只读探针比较。探针若需要固定行顺序，请用 `ORDER BY`。用 `check-sqlite-matrix EVIDENCE` 核对固定材料，也可从生成的 `migration-matrix.json` 明确重放。运行前应备好已关闭、已完成检查点的 SQLite 副本。实际远端数据库需独立验证。

`project verify-wheels DOWNSTREAM --ref REF --before-wheel A.whl --after-wheel B.whl --module NAME --test ID --confirm-run --output EVIDENCE` 在同一个实际下游 Git 快照上分别离线安装两份 wheel，确认从相应临时安装目录加载指定模块，再运行同一组测试。所选 Python 要有 pip、pytest 及必要的其他依赖。用 `check-wheels EVIDENCE` 核对两份实际安装输入和 JUnit 结果。

## 候选、行为与反例

- `project compare-candidates REPO --before BASE --candidate LABEL_A SHA_A --candidate LABEL_B SHA_B --test ID --confirm-run --output EVIDENCE` 分别跑候选，也可用 `--selection-file` 或 `--plan` 指定范围。已有独立报告或 ZIP 时用 `candidate-history --candidate LABEL_A REPORT_A --candidate LABEL_B REPORT_B`。两者使用相同的候选可比性判断，保留各候选的局部门禁、父引用、范围与条件。`same_selection` 核对成员，`same_requested_order` 核对请求列表顺序。pytest 的 `observed_orders` 按基线和候选分别列出已记录的启动序列及出处。进度日志与可核验的旧工作进程事件都可作为顺序证据。完整序列须跨候选、跨两版一致才支持可比通过。顺序不同或证据不足时保留局部结果并返回 `2`，同基线上已有独立失败时返回 `1` 并保留不可比原因。
- `project minimize-input REPORT --test ID --confirm-run --output EVIDENCE` 试探更小的 JSON 值，并保留基线同一记录失败。用 `check-input-reduction EVIDENCE --source REPORT` 复核。命名样本键保持，内容可缩减。结果是操作范围内的局部缩减。
- `project capture-behavior REPO --before A --after B --probe probe.py --policy shape --confirm-run --output EVIDENCE` 在两版运行同一份输出 JSON 的探针。shape 保留既有字段与类型，exact 也比较值。可改用 `--module NAME` 提取公开 Python API 与参数签名。`check-behavior` 从观察重算差异。通过的结构检查只覆盖已声明的接口。
- `project mutation-check REPORT --source-path app.py --confirm-run --output EVIDENCE` 保持原测试，分别翻转应用源码的布尔字面量。见 `check-mutations`。记录一种可复核的测试有效性证据，位置及探针报告可查。

候选历史的 `execution_order_status` 区分 `matched`、`different`、`unknown`、`not_applicable`、`not_comparable` 和 `insufficient_candidates`。其中 Go 和 Vitest 的文件或测试列表在此处没有可比的实际启动顺序。pytest 的不完整序列会保留已观察的启动列表。当可核对同一基线与成员，但实际顺序不同或未知时，可把结果的 `order_rerun.test_ids` 保存为 JSON 数组，再经 `compare-candidates --selection-file` 重跑。优先采用已观察的完整序列作为请求，缺少完整序列时使用标为 `requested_order` 的请求顺序。pytest 插件可能调整启动顺序，重跑后继续以保存的启动记录复核。即使顺序一致，其他运行条件也应查阅对应记录。顺序改变时出现结果变化只支持这组运行的观察，业务与环境根因仍需分别核对。

pytest 候选比较也核对固定时钟、记录的业务样本和参数。每个候选列出保存的固定时钟及核验状态，并单独核对本报告内部的基线和候选输入。`scenario_comparison` 给出候选之间的场景结论。不同设置或样本、以及对齐所需的缺失记录，会在 `issues` 中指明报告、版本侧和相关测试。两边都没有配置固定时钟或采集业务样本时，这些字段显示 `not_recorded`，门禁按其他证据处理。参数化测试的同名 node ID 需要已记录的参数指纹支持。比较 JSON 仅展示样本的比较结论，保存的原始运行证据可能包含样本。

复跑固定日期场景时，用 `compare-candidates --freeze-clock '2026-11-30T12:00:00+00:00' --timezone UTC --locale C` 为整组候选设置同一个 Python 时钟。复跑样本场景时，在测试的输入提供端固定同一份业务输入，由 `reprolab_input` 实际采集，再用 `scenario_comparison` 核对。涉及的测试列于 `tests_requiring_aligned_inputs`，复跑时保留核对所需的共同范围与顺序。未采集的外部值仍未知。

## 长期案例和受限材料

`link-case-contract CASE REPORT PLAN --output FILE` 把具有 `schema_version: 1`、`kind: issue`、`id`、`expected` 和 `tests` 的本地记录关联到已通过的持续验收计划。将 `kind` 改为 `incident` 并提供 `--failure-source` 时还会核对事故失败样本与该计划。复核用 `check-case-contract CASE REPORT PLAN --contract FILE`，事故仍需同一失败来源。外部事故、Issue 来源以及预期行为由维护者提供。

`project project-outcomes REPORT --output projected.json` 以行号替换测试名，只交付允许的结果字段。`check-outcomes projected.json --source REPORT` 可从原证据验证来源关系。没有源证据时只能核对投影结构和结果分类。投影无法作为执行重放输入。普通 Python 可运行 `python -I src/reprolab/project_projection_format.py projected.json`，或用它检查单独 ZIP 内主报告的文件指纹和结果。跨历史、原始门槛和真实执行依然由完整证据与正式入口负责。
