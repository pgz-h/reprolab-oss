# CLI、报告与 CI 接入

分段交付、固定时钟、系统网络隔离、PR 事件、服务和制品的入口见 [审核工作流](audit-workflows.md)。

CLI 与桌面共用执行和证据核心；不需要安装桌面依赖或配置模型。当前 Alpha 的字段仍可能扩展。消费者应检查 kind 和 schema_version，保留原 JSON，允许未知的附加字段；遇到未知状态或不支持的 schema，停止给出通过结论。

## 直接比较与分步检查

- `reprolab project review` 一次冻结两版、发现并运行所选测试，输出与 `project run` 相同的报告 JSON 和退出码。
- `reprolab project prepare` 固定 Git 基线与候选，返回具体会话目录 `directory`；不执行项目测试。
- `reprolab project run` 明确执行检查。没有可用的发现结果时先实际收集，再执行同一份所选测试。需要先浏览测试清单时，单独执行 `project collect`。

`prepare` 会检查所选解释器；`review`、`collect` 和 `run` 会执行可信项目代码。普通项目未提供选择时，仅在发现完整时使用已发现项，范围限于 1–2000 项；继承选择的缺失项不会被静默丢弃。自动化建议明确传入 `--test` 或 `--selection-file`。

默认发现遵循冻结项目的 pytest `testpaths`（未设置时使用 pytest 默认范围），采用实际配置中的 `addopts` 警告筛选并保留显式插件加载规则，其他 `addopts` 参数继续忽略。`project collect --path` 将一个相对测试文件或目录传给 pytest 并保存到会话。此后发现、刷新和复查沿用该范围，除非再次指定路径或用 `--default-scope` 恢复默认。明确执行的测试 ID 须来自实际发现清单。

发现有收集错误或超过 10000 项截断时，`project collect` 返回 2，JSON 保留 `partial`／`truncated`、可用 `tests` 和诊断。`status: collected` 只表示存在可用项，不代表清单完整。`project run` 不会隐式运行这份不完整范围；用户可修复后重新发现，或用 `--test`／`--selection-file` 明确选择可用项。报告保留发现限制，结论只覆盖实际选择。

发现记录新增 `worker_status`、`exit_code`、`timed_out`、`worker_record_complete` 和 `diagnosis`，不改变原 schema 或移除已有字段；旧记录缺少这些字段时只能给出保守诊断。

## 先审阅推荐，再明确执行

先确认项目可信并准备好它的 pytest 环境。把下列三个路径换成本机实际路径。输出路径放在仓库之外，路径中可以有空格。

```sh
repo="$HOME/work/My Project"
python="$HOME/envs/My Project/bin/python"
output="$HOME/work/ReproLab Review"
reprolab project suggest "$repo" --before HEAD --working-tree --python "$python" --output "$output"
```

`suggest` 冻结基线与候选，并在候选快照实际收集。pytest 会加载项目、测试文件及配置中的插件，测试主体等到后续明确执行时才运行。也可用 `--after COMMIT`、`--path tests/subdir` 或 `--source-path PATH` 限定这次对照。输出默认为中文人读预览，`--language en` 改为英文。语言选项只影响预览文字，测试 ID、依赖路径、清单内容和退出码相同。`--format json` 返回原始结构化记录。

- 完整发现且有可用推荐时，打开“待审阅清单”，按需要保留、删除或增加已发现的完整测试 ID，再复制预览最后的 `run` 命令。清单按原收集顺序保存。预览优先显示发生变化的测试定义及候选源码位置，随后显示其余推荐。文件级、间接导入和 fixture 关联仍计入推荐。未推荐项可从“可用测试 ID”文件中手动选入。
- 完整发现但无推荐时退出 `0`，状态为 `no_recommendations`。部分发现、截断、受阻或推荐超过单次 2000 项时退出 `2`。这些情况没有待审阅推荐清单或可直接运行的命令。可从已保存的可用 ID 中明确指定本次范围。若调整的是 Python 环境，可用 `project collect` 复查原会话；若改了源码或项目配置，请重新 `project suggest` 冻结新会话。新建会话直接运行时也须给出 `--test`、`--selection-file` 或验收计划。
- 要手动执行，把预览中“已冻结会话”的实际路径赋给 `session`，把示例 ID 换成该会话中明确选中的 ID。例如 `reprolab project run "$session" --test 'tests/test_cart.py::test_total' --output "$output/reports"`。重用同一会话和清单可多次保存新报告。每次使用原固定源码，Python 环境沿用会话的解释器路径。若更改环境导致实际发现需要核对，仍按所选完整 ID 检查。
- `suggestion.json` 保存两版指纹、当次发现记录和其 SHA-256、推荐原因以及清单位置。测试定义变化取自当次收集记录的 `changed_test_definitions`，位置指向候选快照。推荐受发现范围、静态关联与已记录 fixture 绑定限制。旧会话可继续按原流程运行和检查，旧报告缺少新字段时仍可查看原结果。

## 执行前从审阅清单保存验收计划

从 `project suggest` 取得会话路径，审阅并编辑它给出的清单。把会话、清单、测试 ID 和保存路径换成本轮实际值，计划的父目录须已存在。可明确保存到仓库配置目录，例如 `.ci/acceptance-plan.json`。此时复制的运行命令仍使用原冻结会话，并把报告写到仓库外。

```sh
reprolab project plan-from-selection "$session" --selection-file "$selection" \
  --require-improvement 'tests/test_cart.py::test_total' \
  --output "$output/reviewed plan.json"
```

预设门禁为 `strict`，也可用 `--gate no-new-failures`。省略 `--require-improvement` 时，计划仍保存完整审阅范围和门禁。可重复提供不同的必验目标，每个都须是审阅范围内实际发现的完整 ID。空清单、重复 ID、缺失 ID 和不完整或无法核验的发现受阻。命令只读取固定源码及已保存发现，不调用 pytest、模型或执行项目，也不会覆盖已有文件、写入 Git 元数据或改写冻结会话与报告证据目录。

计划使用现有第 3 版格式，绑定基线源码及文件权限的 SHA-256，保存有序的 `approved_selection` 和显式 `required_tests`。这是维护者选定的验收条件。执行后的报告另行证明各项是否通过，签名批准须另行完成。生成成功返回 `0` 并输出 `acceptance_plan_sha256` 和可复制的 `run_command`。

```sh
reprolab project run "$session" --acceptance-plan "$output/reviewed plan.json" \
  --verify-plan --output "$output/reports"
reprolab project summary "$received_zip" --acceptance-plan "$output/reviewed plan.json" \
  --expect-plan-sha256 "$trusted_plan_fingerprint"
```

第一条按计划清单执行并核对报告。第二条用收到的原证据 ZIP 和可信渠道取得的计划、指纹离线重算，报告目录也可代替 ZIP。缺少已审阅项或必验目标时，即使已选项全绿，计划仍会受阻。换了冻结基线的会话须重新审阅。已有格式的验收计划继续由原入口读取。

## 对比安装后的下游 wheel

用 `verify-wheels` 在可信的固定下游项目上实际安装、导入并测试两份 wheel。省略 `--confirm-run` 时仅给出执行预览。显式提供 `--gate`（含 `legacy`）或 `--require-improvement` 时必须确认执行，缺少确认返回 2。单独选择 `--format` 不改变预览的退出行为。默认使用已有 Python 环境中的 pytest 和 pip，分别安装两份 wheel 时使用 `--no-deps`。该环境缺少 pip 时，确认运行会在生成证据前报告所选解释器路径。首次比较与已有证据重算可以使用同一套逐项门禁。

```sh
reprolab project verify-wheels "$downstream" --ref HEAD \
  --before-wheel "$baseline_wheel" --after-wheel "$candidate_wheel" \
  --module my_package --test 'tests/test_example.py::test_behavior' \
  --confirm-run --output "$evidence" --gate regression --format markdown

reprolab project check-wheels "$evidence" --gate no-new-failures --format json
```

将示例中的测试目标换为实际的 pytest 选择器。也可用 `pack-wheels "$evidence" --output "$evidence_zip"` 打包，再离线对证据 ZIP 使用 `check-wheels "$evidence_zip"`。核对会重验下游快照、wheel 和绑定的运行文件，随后对比各版 JUnit。新运行还记录每项 pytest 阶段及完整 node ID。旧目录或旧 ZIP 可按 pytest JUnit 规则重建身份，不能唯一重建的项或未记录的阶段保持不确定。完整参数后缀参与对齐。重复、缺失、额外案例、跳过、收集或安装不完整等原因不从总数猜测。

- 直接调用 `verify-wheels` 仍返回原聚合及其门禁。直接调用 `check-wheels` 仍只按材料核对退出。两者默认输出原 JSON。
- 显式使用 `--gate regression` 时，有确认新增失败返回 `1`。仅有通过和改善返回 `0`。已有失败、未观察或不确定项使门禁返回 `2`。已有明确新增失败时仍保留 `1`，并同时展示范围问题。
- `--gate no-new-failures` 允许已核对的既有失败。需要相同测试 ID、已记录的失败位置与类型，以及精确匹配的失败记录。两版都红仍可出现在逐项结果中；位置、异常或文本变化与证据不足均受阻。
- 当两侧 wheel 文件名与 SHA-256 都相同，且固定下游测试出现红绿反转时，逐项报告保留两次结果并标记 `same_wheel_divergence`。逐项门禁与必验改善项会受阻。旧聚合状态按原规则读取。
- `--require-improvement TEST_ID` 可重复指定不同的必验目标；参数化用例须包含完整参数后缀。目标未观察时受阻，已观察但没有由失败变为通过时失败。提供此选项时默认逐项 `regression` 门禁。
- 逐项输出的顶层 `status` 保留旧聚合。`integrity` 表示材料核对结果，业务判定见 `comparison.gate`。使用显式门禁时，`check-wheels` 也按该业务门禁退出。仅改用 `--format markdown` 或 `--format json` 时，核对仍按材料一致性退出。
- 逐项 JSON 和 Markdown 保留相对测试地址并对参数后缀使用指纹。`--include-test-ids` 可显示原始参数 ID。逐项输出省略日志、异常文本、主机名和本机路径；交接原证据前应审阅 ZIP 内的原始内容。

### 隔离功能测试

发布前可把 `--test-wheelhouse "$trusted_test_wheelhouse"` 加入 `verify-wheels` 命令。ReproLab 将可信本地 wheelhouse 暂存一次。基线和候选各用一个最初无第三方分发的 venv，通过同一次 pip 解析安装各自的 wheel、默认声明依赖和 `pytest>=8,<10`，随后运行原有选测。所选 Python 须支持创建 venv 和启动 pip 工具环境。wheelhouse 须包含适用于该 Python 的 pytest、工具依赖及两端需要的运行依赖，遵循[干净安装的本地输入约束](#检查候选-wheel-的干净安装)。文件出现在 wheelhouse 中只表示可供解析，只有解析选中的包进入目标环境。pytest 及其依赖属于两端测试环境。

隔离模式检查导入的 `--module` 位于对应 venv，且其文件可由已安装分发的 RECORD 唯一归属于对应 wheel。对命名空间包，请选择有实体文件的子模块。依赖安装、工具准备或模块归属受阻时返回 `2`，可查阅相应一侧的 `install.log` 或 `pytest.log`。通过的工具初始化日志附在同一侧的 `install.log` 中。venv 分离 Python 包环境，执行仍依赖可信的解释器、制品和下游测试代码。

### 接收方验收 wheel 发布证据

维护者在证据之外维护发布计划。使用从可信制品取得的文件名和 SHA-256、审阅过的下游提交及选测范围填好计划。接收方另外准备含该提交、树和 blob 的可信本地 Git 仓库（也可用裸仓库）。

```sh
reprolab project check-wheels "$received_zip" \
  --review-plan "$maintainer_plan" \
  --verify-git-repository "$trusted_downstream_git" --format markdown
```

证据目录也可代替 ZIP。两个验收选项须同时使用。`--gate` 和 `--require-improvement` 只能通过计划提供。验收 Markdown 始终对参数 ID 使用指纹。

```json
{
  "kind": "reprolab_wheel_review_plan",
  "schema_version": 1,
  "wheel_artifacts": {
    "before": {
      "filename": "my_package-1.0-py3-none-any.whl",
      "sha256": "1111111111111111111111111111111111111111111111111111111111111111"
    },
    "after": {
      "filename": "my_package-2.0-py3-none-any.whl",
      "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
    }
  },
  "downstream_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "module": "my_package",
  "selected_tests": [
    "tests/test_example.py::test_smoke",
    "tests/test_example.py::test_behavior[expected-case]"
  ],
  "gate": "regression",
  "required_improvements": []
}
```

- 把示例中的提交、摘要、文件名、模块及测试替换为实际的期望值。提交须为 40 或 64 个小写十六进制字符，wheel 摘要须为 64 个小写十六进制字符。每端只用 wheel 的文件名。
- `selected_tests` 是非空、无重复的有序 pytest 选择器列表。验收按发送方 `verify-wheels --test` 的记录逐项原样比较，交换顺序也会受阻。函数选择器可覆盖其参数化用例，需要固定具体参数时应在计划中逐项列出完整 node ID。这个顺序约定针对传给 pytest 的选择器，实际收集和运行顺序仍由 pytest 决定。
- `gate` 可选 `regression` 或 `no-new-failures`。`required_improvements` 可省略或为空，指定时须列出无重复的完整 node ID，包括所需的参数后缀。计划使用严格的 UTF-8 JSON，拒绝未知字段、重复键和不支持的版本。
- 可选的 `required_test_environment` 固定两端功能测试实际使用的环境，包含 `python_version`（完整版本）、`implementation`、`platform` 和 `machine`，字段格式与下节的安装环境相同。例如，`{"python_version":"3.11.18","implementation":"CPython","platform":"darwin","machine":"arm64"}`。分别使用两端 pytest 子进程写入、且通过证据指纹核对的 phase 记录。缺失、无法读出或任一端值不匹配时返回 `2`。历史证据照常支持旧计划。功能测试环境和干净安装环境可各自指定，摘要分别显示。
- 可选的 `required_test_wheelhouse_sha256` 要求两端都启用隔离功能测试，并使用经维护者批准的同一份功能 wheelhouse。由维护者从独立可信的目录执行 `project fingerprint-wheelhouse DIRECTORY`，将输出的 `wheelhouse_sha256` 填入计划。证据保存暂存清单和两端已绑定的 phase 记录，wheelhouse 的 wheel 文件由维护者另行保管。接收方从清单重算指纹，再和两端 phase 记录分别核对。历史已有环境模式、任一端记录缺失、清单或指纹不符均返回 `2`。该字段可独立于 `required_test_environment` 和 `required_install_*` 使用。摘要单独报告功能依赖来源。

接收方先核对证据指纹和计划范围，再用本地 Git 对象独立导出计划提交，核对冻结文件的内容和权限。Git 导出采用与执行端相同的源码范围。验收在范围匹配后重算计划门禁。JSON 的 `review_plan_acceptance` 记录计划 SHA-256 和验收结论，范围受阻时给出 `mismatched_fields`。计划与证据匹配且门禁通过返回 `0`，范围匹配且有确认的新增失败或必验改善失败返回 `1`，范围不符、Git 对象缺失或证据不足返回 `2`。

离线验收可以判断已保存的结果是否覆盖维护者指定的 wheel 与下游输入，并满足所选门禁。运行记录的真实性取决于发送方或受信的执行来源。发送端在已有 Python 环境中用 `pip --no-deps` 安装 wheel。如发布还要求在空环境中完成依赖解析与安装，可把下节的检查纳入同一份计划。

### 按维护者计划生成 wheel 功能证据

发送方可用接收方提供的同一格式计划生成证据。

```sh
reprolab project verify-wheels "$downstream" --review-plan "$maintainer_plan" \
  --before-wheel "$baseline_wheel" --after-wheel "$candidate_wheel" \
  --python "$target_python" --test-wheelhouse "$local_test_wheelhouse" \
  --confirm-run --output "$evidence" --format markdown
```

实际提交、模块、选测顺序、门禁和必验改善项取自一次读取的计划。计划执行拒绝同时传入 `--ref`、`--module`、`--test`、`--gate` 或 `--require-improvement`。手工模式的 `--ref` 默认仍为 `HEAD`。若计划要求 `required_test_wheelhouse_sha256`，须提供 `--test-wheelhouse`。隔离模式使用能创建 venv 的 Python，pytest 由功能 wheelhouse 提供。计划固定 Python 时，预检从实际所选解释器采集环境标识。

执行前先暂存并核对两端 wheel 的文件名和 SHA-256，并核对暂存的功能 wheelhouse。两端制品都固定后才开始安装。省略 `--confirm-run` 只执行预检，成功只表示本次检查的输入符合计划。确认执行时会重新检查本次实际暂存的输入。执行完毕后，发送方还会从两端已核对指纹的 pytest 记录复核计划要求的实际功能环境和隔离范围。范围不符时保留证据、指出具体字段并返回 `2`。命令输出功能门禁结果和本次读取的计划 SHA-256。需要独立干净安装报告的发布计划，由接收方用 `check-wheels --install-check` 完成发布验收。

### 在 CI 中保留 wheel 验收摘要和退出码

Markdown 首屏显示决定命令退出码的规则及实际退出码，并单独显示业务门禁。普通 `check-wheels --format markdown` 按证据完整性退出，因此材料一致时可返回 `0`，即使业务门禁显示失败。要在流水线中应用普通业务门禁，传 `--gate`。接收方计划模式按发布验收退出，发送方计划模式按功能门禁退出。证据缺失、ZIP 无法读取或计划无效时，Markdown 提供简短受阻摘要，详细诊断仍写到标准错误。

下面的 GitHub Actions 步骤假定工作流已提供 `EVIDENCE_ZIP`、`REVIEW_PLAN`、`TRUSTED_DOWNSTREAM_GIT` 和 `INSTALL_CHECK`。只验收功能且计划未要求干净安装时，移除 `--install-check` 参数。

```yaml
- name: Verify wheel release evidence
  shell: bash
  run: |
    summary="$(mktemp)"
    trap 'rm -f "$summary"' EXIT
    status=0
    reprolab project check-wheels "$EVIDENCE_ZIP" \
      --review-plan "$REVIEW_PLAN" --verify-git-repository "$TRUSTED_DOWNSTREAM_GIT" \
      --install-check "$INSTALL_CHECK" --format markdown >"$summary" || status=$?
    if [ -s "$summary" ]; then
      cat "$summary" >>"$GITHUB_STEP_SUMMARY"
    else
      printf '### ReproLab wheel check\n\n- Exit code · %s\n- Diagnostic details are on standard error.\n' "$status" >>"$GITHUB_STEP_SUMMARY"
    fi
    exit "$status"
```

### 检查候选 wheel 的干净安装

```sh
reprolab project verify-wheel-install "$candidate_wheel" \
  --wheelhouse "$local_wheelhouse" --python "$target_python" \
  --confirm-run --output "$new_install_report"
```

省略 `--confirm-run` 会预览候选文件名和可供解析的 wheel 数量。确认后，命令使用所选 Python 创建两个临时 venv。工具环境提供 pip，目标环境从空的第三方包集合开始。pip 将候选 wheel 和所需依赖装入目标环境，再在同一目标环境执行 `pip check`。省略 `--python` 时使用当前解释器。创建环境和 pip 检查共用 `--timeout`，默认 120 秒。

本地 wheelhouse 可以为空，只读取其中的普通 wheel 文件。目录上限为 512 个条目、128 份 wheel、合计 1 GiB，单份 wheel 上限为 250 MiB。pip 使用本地暂存的 wheel，关闭索引、缓存和调用者的 pip 配置。候选和依赖的 `Requires-Dist` 若声明直接 URL 或路径引用会受阻。请提供可信制品和可信解释器。这个约定约束 pip 的制品来源，运行进程仍拥有操作系统授予的权限。

输出目录内的 `wheel-install-check.json` 保存候选文件名和 SHA-256、实际 Python 版本、实现、平台、架构、pip 版本和各步骤结论，并附本机诊断日志。安装与 `pip check` 均通过返回 `0`，pip 报告安装或依赖检查失败返回 `1`，工具环境不可用、超时或输入超出本地安装约定返回 `2`。此检查覆盖指定解释器下的默认安装和适用的声明依赖，功能测试仍由 `verify-wheels` 提供。

要让发布接收方强制验收一个目标环境，在上节计划顶层加入 `required_install_environment`。以下是 macOS arm64 上 CPython 3.11.18 的值，实际值可从目标环境生成的检查报告读取。

```json
{
  "required_install_environment": {
    "python_version": "3.11.18",
    "implementation": "CPython",
    "platform": "darwin",
    "machine": "arm64"
  }
}
```

```sh
reprolab project check-wheels "$received_zip" \
  --review-plan "$maintainer_plan" --verify-git-repository "$trusted_downstream_git" \
  --install-check "$trusted_install_report/wheel-install-check.json" --format markdown
```

接收方在功能证据之外提供自己执行或从受信执行方取得的报告。验收要求报告记录的候选文件名、SHA-256 及四项环境值逐一匹配，并且安装和 `pip check` 都通过。缺失、失败或不匹配会返回 `2`，报告与其他范围都匹配后才按功能门禁退出。摘要单独显示安装检查通过，JSON 的 `review_plan_acceptance.installation` 保存报告 SHA-256 和匹配的环境。验收只读取报告，报告真实性仍依赖执行来源。省略计划中的 `required_install_environment` 时，原有功能验收照常使用。

### 固定安装检查使用的 wheelhouse

```sh
reprolab project fingerprint-wheelhouse "$trusted_wheelhouse"
```

维护者从独立可信的本地依赖目录生成期望值，把 JSON 输出的 `wheelhouse_sha256` 填入同一发布计划顶层的 `required_install_wheelhouse_sha256`，并保留 `required_install_environment`。接收方从安装报告记录的实际暂存输入清单重算指纹。即使候选和 Python 环境相同、安装也通过，来自不同 wheelhouse 的报告仍会返回 `2`。

指纹覆盖目录顶层全部合规 wheel 的文件名和逐文件 SHA-256，复用安装检查的文件数量、大小和依赖声明约束。清单按文件名排序，条目使用排序后的 JSON 对象键和紧凑分隔符，取该 JSON 的 SHA-256。目录位置、输入顺序和普通非 wheel 文件不参与计算，空目录对应 `[]` 的 SHA-256。重复文件名（含仅大小写不同）、无效清单项、非法 wheel 文件或直接 URL 依赖会受阻。

这是暂存给 pip 的可用依赖制品清单。实际选中的依赖仍由 pip 按目标环境和依赖声明解析，报告的真实性仍取决于可信执行来源。

## 原生测试试点 / Native test pilot

固定 Git 两版后，`project native` 可构建并执行 Cargo 或 CMake/CTest。默认预览读取 Git 版本及遗漏改动计数，实际构建和运行须显式提供 `--confirm-run`。把源码和记录视为可信输入。每版在独立临时副本构建，证据保留两版源码、运行阶段和各自测试结果。

```sh
reprolab project native "$repo" --runner cargo --before "$base" --after "$candidate" \
  --shared-test-source src/tests.rs --test 'tests::my_case' \
  --confirm-run --output "$evidence"

reprolab project native "$repo" --runner cmake --before "$base" --after "$candidate" \
  --shared-test-source tests/checkout.c --test checkout \
  --confirm-run --output "$evidence"
```

Use the actual test identities and repo-relative definition files. The Cargo pilot requires one root package with a tracked Cargo.lock and supports standard library unit tests through libtest. Cargo dependency resolution uses locked offline mode. The CMake pilot uses local CMake and CTest 3.29 or newer with a Debug build. Supply `--cargo /path/to/cargo` if Cargo is outside PATH. Repo build scripts and tests still execute trusted code.

`--shared-test-source` 明确声明定义本轮测试的文件，可重复提供。工具核对这些文件的两版内容和权限，再用完全一致的测试名对齐。这是对已声明范围的核对。请把相关测试源码和定义配置一并列入；测试内联于已修改文件时，可先拆出独立的测试模块。CTest 还核对同名测试的实际命令、参数和属性。没有已核对的共享声明，或同名 CTest 的配置改变或缺证据时，两版各自结果依然可见，该项不作为共享测试。同名测试不足以证明共享定义。改名、单侧缺项、跳过、构建／发现受阻或空集均不能得到通过。单侧未观察不代表测试已删除，需结合该侧的发现与运行状态判断。显式选择外的范围没有本轮结论。

The gate compares exact IDs on both sides with identical declared definition files. CTest also requires matching recorded commands, arguments and properties. Changes outside those files can still affect the tests; this is a check of the declared scope. The report keeps each native result alongside the shared comparison. A missing observation can also mean that a side could not build or discover tests. Exit codes are 0 for a passing selected comparison, 1 for a confirmed regression, and 2 for a blocked comparison. A confirmed regression still counts when another case is ignored or inconclusive.

```sh
reprolab project pack-native "$evidence" --output "$evidence_zip"
reprolab project check-native "$evidence_zip"
```

CI 可用 `check-native --verify-result` 直接按复核后的普通门禁退出，输出中的 `verified_result.rules` 标明当前或历史规则。接收方可用 `check-native --review-plan FILE` 按独立计划验收，并用 `--verify-git-repository REPOSITORY` 要求本地 Git 来源核对。完整命令与计划字段见[原生项目测试](native-languages.md#读结果--reading-the-gate)。

当前 PR 的接收作业可用 `check-native ZIP --github-event FILE --pr-mode head|merge --policy-path PATH --verify-git-repository REPOSITORY`。它从接收方的事件与本地对象重建计划，要求证据记录相同的计划 SHA-256，并核对冻结 Git 快照。计划包含目标和 head 提交，目标分支单独前进也会阻止旧证据通过。纯离线复核可继续用原计划文件。参见[从 GitHub PR 事件生成验收计划](native-languages.md#从-github-pr-事件生成验收计划)。

`check-native` 也接受证据目录。核对散列、原始运行记录及重算结果，全程不重跑构建或测试。核对命令的 `0` 表示材料一致，业务门禁见 `comparison.gate.exit_code`。证据 ZIP 含冻结源码与原始日志，交接前请审阅。

## 在 CI 中比较两个提交

先用项目原有方式准备依赖，将仓库、解释器和选择清单换成实际路径。选择清单支持 JSON 或每行一个完整测试 ID 的文本。

```sh
reprolab project review /path/to/repository --before BASE --after CANDIDATE \
  --python /path/to/project-env/bin/python --selection-file /path/to/selection.txt \
  --output /tmp/reprolab-review --progress > /tmp/reprolab-result.json
```

会话与报告分别保存在输出根目录下的 `sessions` 和 `reports`，输出根目录应位于被测仓库外。报告 JSON 提供 `project_directory` 和 `report_dir`。如果已冻结源码但未形成报告，stderr 的错误 JSON 会附带 `project_directory`，可用 `project show` 查看已保存会话，或调整后用 `project collect`、`project run` 继续。

本地检查未提交改动时可用 `--before HEAD --working-tree`。`--path` 可缩小发现范围，`--test` 可重复指定精确测试 ID。需要在执行前查看清单时，用 `project prepare` 创建会话，从其 JSON 的 `directory` 取得后续命令所需的目录。多轮检查可使用[报告复查](report-recheck.md)沿用最初固定基线。

Go 单包比较可用 `project go … --all-tests --output DIRECTORY`，在同一次冻结的候选上发现并比较测试。报告的 `selected_tests` 是实际发现的顶层函数。只看清单时用 `--list-tests`，其 JSON 的 `kind` 为 `go_test_discovery`、`schema_version` 为 `1`，`tests` 是函数名数组。发现失败或清单为空时返回 `2`，诊断写入 stderr。见 [Go 命令与运行条件](go.md#命令行)。

Vitest 可用 `project vitest … --all-files --output DIRECTORY` 从同一次冻结的候选发现并比较文件。报告保存实际选择和发现日志。只看清单时用 `--list-files`，其 JSON 的 `kind` 为 `vitest_file_discovery`、`schema_version` 为 `1`，`files` 是 Git 仓库相对路径数组。见 [Vitest 命令行](vitest.md#命令行)。

## PR 摘要与 CI 门槛

`project summary REPORT` 先校验已有报告及执行证据，再在标准输出生成 Markdown。支持 pytest、Go 和 Vitest，可接受报告目录、`report.json` 或证据 ZIP。`--format json` 提供带 `schema_version` 的结构化摘要。

- 摘要提供两版来源、执行状态、选择范围、结论和报告 SHA-256。Vitest 新格式分别显示文件和用例统计。
- 新版报告的默认摘要列出新增失败的身份。pytest 参数化 ID 的参数部分显示为指纹，长名称附带可区分的指纹。`--include-test-ids` 可额外查看其他关注项。对外只需分享统计时使用 `--public-summary`。
- 省略 `--gate` 时，成功生成摘要返回 `0`，报告中的测试结论照常显示。证据不一致或报告无法读取时返回 `2`，诊断写入标准错误。
- `--gate strict` 沿用项目比较的判定。全部所选项通过或改善返回 `0`，出现新增失败返回 `1`，原有失败或其余需要处理的情况返回 `2`。
- `--gate no-new-failures` 在两版完成且所选结果可比较时允许原有失败。新增失败返回 `1`。跳过、受阻、取消和不完整执行仍需要处理，返回 `2`。只含文件级结果的旧 Vitest 报告无法用该规则豁免原有失败。Go 比较以选定的顶层函数为单位。
- 门槛结果会写入摘要，原报告结论保持原样。两版均失败表示该测试在基线和候选都失败，仍可审阅完整报告中的两次故障。若同一次比较包含新增失败和受阻项，门槛返回 `1`，摘要同时保留两类结果。

### 精简摘要交接 / Decision brief handoff

把命令中的文件名换成实际收到的 ZIP。分享时将 Markdown 与原 ZIP 一并交接。桌面可从结果页「复制精简摘要」，粘贴为 UTF-8 Markdown。桌面复制的是中文，使用严格门禁。

```sh
reprolab project summary 'received.zip' --brief --language zh > brief.zh.md
reprolab project check-summary 'received.zip' brief.zh.md --brief --language zh
```

Use the actual received ZIP path. Share the Markdown with the original ZIP. Both commands read saved evidence offline. The CLI defaults to English and a strict gate.

```sh
reprolab project summary 'received.zip' --brief > brief.md
reprolab project check-summary 'received.zip' brief.md --brief
```

生成命令按门禁退出，`0` 通过、`1` 失败、`2` 受阻或证据不可用。核对命令按材料一致性退出，`0` 表示全文匹配，`2` 表示不同或无法核对。核对输出的 `gate.exit_code` 是按接收方所给条件重算的门禁，材料一致时也可以是 `1` 或 `2`。选择与发送方相同的语言和门禁选项。若使用验收计划或其他材料，双方应从独立可信来源取得对应文件并明确传入。

Generation exits with the gate decision, while verification exits with the text check. A matching brief returns `0`, and `gate.exit_code` may separately report a failure or a block. Use the sender’s language and gate options. Supply the same evaluation inputs independently when a plan or other materials were used.

全文比对接受 CRLF 换行。短摘要核对覆盖展示的汇总和报告指纹。逐项结果、未展示的计划内容与发布者身份须分别核对。省略 `--brief` 使用现有完整 Markdown/JSON 摘要和历史核对；完整摘要可另附 `--facts-file`。要看原记录，按 `reprolab project unpack --help` 导入刚核对的 ZIP，然后查看其中的 `report.html` 或 `report.json`。

The check covers the displayed aggregates and report fingerprint, with CRLF accepted. Check unshown plan details, individual results and publisher identity separately. Omit `--brief` to verify a full or historical summary. Use `reprolab project unpack --help` to inspect the original ZIP.

Pytest 在两版均因测试主体的 `AssertionError` 失败时，会尽量比较记录的断言。位置指纹取自最内层 traceback 对应的项目相对文件、函数限定名和断言表达式的语法结构。行号用于查找表达式，指纹排除行号、绝对路径、断言消息及运行时值。指纹不同需要复核，返回 `2`，两版均失败的观察仍计入 `baseline_failure`。指纹相同支持继续沿用原有失败策略，证明范围限于所记录的断言表达式和位置标识。

同一函数中有多个相同条件时，新记录包含条件的出现次序、次数和函数语法指纹。两版函数结构相同且次序不同会触发复核。次数或结构不同、或同一行有多个断言而无法定位时，也返回 `2`。仅移动行或加注释保留可比较性。

旧 ZIP 可用新版直接重新生成摘要。只含上一版位置指纹时，等值指纹必须从保存的共享测试源码核对为唯一断言才能放行。对应多个语句、位于辅助模块或缺少可核对源码时需复核。更早、缺少断言位置字段的记录沿用消息比较兼容策略，消息指纹不同触发复核，消息一致或只能比较阶段与类型时显示所用依据。新版报告对显式抛出 `AssertionError` 或来自快照外的故障也可能无法取得位置，此时按阶段与类型处理。需要更完整的位置证据时，用新版重新执行两版。位置一致的断言仍可能因不同输入或根因失败。

`assertion_comparison` 在 JSON 摘要中提供各依据的计数。`--include-test-ids` 可查看逐项依据，`--public-summary` 仅包含这些计数。

在可信项目的 GitHub Actions 任务中，使用普通 `pull_request`、只读权限和 GitHub 提供的合并提交。示例安装合并工作区的测试依赖与固定版本 ReproLab，使用同一解释器运行两版。请按项目准备可同时运行两版的依赖。选择清单取自事件绑定的目标提交。ReproLab 核对合并结果的两个父提交分别是目标提交和 PR head，合并对象缺失或父提交不符时返回 `2`。步骤写入 [GitHub 任务摘要](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#adding-a-job-summary)。

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
    persist-credentials: false
- uses: actions/setup-python@v7
  with:
    python-version: '3.14'
- name: Install test dependencies and ReproLab
  env:
    REPROLAB_WHEEL_URL: ${{ vars.REPROLAB_WHEEL_URL }}
  run: python -m pip install -r requirements-test.txt "$REPROLAB_WHEEL_URL"
- name: Check selected tests on the merge result
  shell: bash
  env:
    BASE_SHA: ${{ github.event.pull_request.base.sha }}
  run: |
    run_dir="$(mktemp -d "$RUNNER_TEMP/reprolab.XXXXXXXX")"
    if ! git show "$BASE_SHA:.ci/reprolab-tests.txt" > "$run_dir/selection.txt"; then
      echo 'ReproLab blocked · target test selection unavailable.' >> "$GITHUB_STEP_SUMMARY"
      exit 2
    fi
    result="$run_dir/result.json"
    comparison=0
    reprolab project review "$GITHUB_WORKSPACE" --github-event "$GITHUB_EVENT_PATH" \
      --pr-mode merge --merge-commit "$GITHUB_SHA" --audit-discovery \
      --python "$(command -v python)" --selection-file "$run_dir/selection.txt" \
      --output "$run_dir/review" > "$result" || comparison=$?
    if (( comparison > 2 )); then exit "$comparison"; fi
    if ! report="$(python -c 'import json, sys; print(json.load(sys.stdin)["report_dir"])' < "$result")"; then
      echo 'ReproLab blocked · no checked merge comparison report. See the step log.' >> "$GITHUB_STEP_SUMMARY"
      exit 2
    fi
    reprolab project summary "$report" --gate no-new-failures >> "$GITHUB_STEP_SUMMARY"
```

`project demo --output DIRECTORY` 运行内置购物车教学项目，返回与 `project review` 相同格式的报告。示例预期出现 5 项两版通过，以及改善、新增失败、原有失败和受阻各 1 项。

### 验收指定修复和回执

在需要确认某个故障确实修好的 PR 中，列出必须由失败转为通过的测试。可同时要求回执，直接用发送方的 ZIP 复核。

```sh
reprolab project summary ./handoff.zip --gate no-new-failures \
  --require-improvement 'tests/test_cart.py::test_discount_cannot_make_total_negative' \
  --require-receipt ./receipt \
  --require-stability ./stability-checks/replay-stability-EXAMPLE --min-stability-rounds 3
```

- pytest 传入完整 node ID，包括参数化后缀。Go 传入顶层测试函数名。
- Vitest 使用 `--require-vitest-case FILE CASE`，其中 `FILE` 是 Git 仓库相对路径，`CASE` 是报告中的完整用例名。仅有文件级结果的旧报告无法确认单个用例改善。
- 两种目标参数都可重复传入不同用例。所有目标都必须改善。两版均通过、两版均失败和新增失败都无法满足目标。缺少指定用例、跳过或未能完整运行时，目标状态为 `blocked`。
- `--require-receipt` 重新校验回执 ZIP、JSON 与 Markdown，再核对固定输入和逐项结果。材料相符且结果完整复现时通过；如果复现结果发生变化，返回失败；材料不一致或证据不足时返回受阻。运行条件差异仍单独记录。
- `--require-stability` 接受已保存的稳定性检查目录或摘要文件。它重新校验同级 `reports/` 下各轮证据，要求全部轮次完整复现，且轮数达到 `--min-stability-rounds`（默认 2，可选 2–10）。如果达到轮数且观察到变化或持续偏离，返回失败；轮次不足、取消、证据缺失或未能完成核对时受阻。
- 使用任一验收要求时，默认启用 `strict`；可显式选择 `no-new-failures`。所有检查通过返回 `0`，有明确失败返回 `1`，只有受阻项时返回 `2`。证据读取或参数错误同样返回 `2`，并在 stderr 给出诊断。
- `--format json` 的 `comparison_gate` 保留原有门槛结论，`required_improvements`、`receipt` 和 `stability` 记录已指定的附加检查，`gate` 给出最终退出码。明确指定的目标名称会出现在摘要中。命令只读取保存的证据，无需加载项目代码。

### 复用验收计划

桌面「修复验收 → 保存计划…」可导出 JSON，CI 可以直接用同一份计划。以下计划要求指定测试改善、收到一致的回执，并完整复现至少三轮。

```json
{
  "kind": "project_acceptance_plan",
  "schema_version": 1,
  "runner": "pytest",
  "policy": "strict",
  "required_tests": ["tests/test_cart.py::test_discount_cannot_make_total_negative"],
  "require_receipt": true,
  "min_stability_rounds": 3
}
```

```sh
reprolab project summary ./handoff.zip --acceptance-plan ./acceptance-plan.json \
  --require-receipt ./receipt --require-stability ./stability-checks/replay-stability-EXAMPLE
```

pytest 使用完整 node ID，Go 使用顶层测试函数名，Vitest 使用 `{"file": "tests/cart.test.ts", "case": "cart > applies discount"}`。无需指定目标时可用空数组。将 `require_receipt` 设为 `false` 或将 `min_stability_rounds` 设为 `null`，可分别取消对这两份材料的要求。执行器须与报告匹配。使用计划时，策略、目标和最少轮数从文件读取，CLI 只需提供本次的证据路径。

若报告缺少目标，或本次缺少计划要求的材料，对应项按受阻处理。退出码仍使用原有的 0、1 和 2。Markdown 与 JSON 摘要都会给出计划 SHA-256。指纹基于计划内容，移动文件或调整 JSON 空白和对象字段顺序后可继续核对。

锁定基线的计划使用 `schema_version: 2`，并增加 `baseline` 对象。其字段 `source_sha256` 和 `source_mode_sha256` 分别来自来源报告这两个对象的 `before` 值。CLI 以校验后的报告比较基线，基线变化或指纹缺失时将 `baseline_check.status` 标为 `blocked`。如果其它检查没有明确失败，退出码为 `2`。候选版本可以变化。已有 schema 1 计划可继续使用，计划指纹的计算方式保持兼容。

### 用计划选择 pytest 测试

```sh
reprolab project review ./repo --before BASE --working-tree \
  --acceptance-plan ./acceptance-plan.json --output ./review
```

对于已有会话，可用 `project selection-check SESSION --acceptance-plan FILE` 核对已保存的发现记录，再用 `project run SESSION --acceptance-plan FILE --output REPORTS` 运行两版。用计划选择测试时，计划至少须包含一个 pytest 完整测试 ID，运行范围来自计划中的明确目标。`selection-check` 的计划与 `--test`、`--selection-file` 互斥。

运行时会先核对锁定基线，再按本次运行环境核对测试发现。基线不符、发现不完整或缺少目标时返回 `2`，stdout 的 `status` 和 `missing_tests` 提供诊断，`project_directory` 指向保存的会话。只读核对还可能返回 `needs_collection`。成功运行仍输出原有报告。用 `project summary REPORT --acceptance-plan FILE` 完成验收，包括计划中的回执及稳定性要求。

### 运行测试并直接验收计划

```sh
reprolab project go ./repo --before BASE --working-tree --package internal/cart \
  --acceptance-plan ./go-plan.json --verify-plan --output ./reports
reprolab project vitest ./repo --before BASE --working-tree --project-dir apps/web \
  --acceptance-plan ./vitest-plan.json --verify-plan --output ./reports
```

Go 在冻结的候选包发现顶层测试，缺少指定函数时返回 `2`。Vitest 运行计划指定用例所在的完整测试文件，同一文件只选择一次。缺少文件时在运行前返回 `2`，缺少用例时保留报告，并在验收中标为受阻。Go 的包目录和 Vitest 的 npm 项目目录仍由本次命令指定。计划锁定的基线会在运行前核对。

`--verify-plan` 也可用于 pytest 的 `review` 和 `run`。运行后会重新校验报告，并在 stdout 的报告 JSON 中追加 `acceptance`，退出码使用验收门槛。报告原来的 `status` 和 `report_dir` 仍可读取。若需核对已有材料，可同时提供 `--require-receipt` 或 `--require-stability`。计划要求材料而本次尚未提供时，保留报告并按受阻处理，后续可以用 `project summary` 补齐。验收操作出错时，stdout 保留报告位置和 `acceptance_error`，退出码为 `2`。

要在修复目标之外运行回归测试，使用 `--verify-plan` 同时提供计划和显式范围。三个执行器都支持 `--selection-file`。清单支持 JSON 数组或逐行文本，分别填写 pytest 完整 ID、Go 顶层函数名或 Vitest 仓库相对路径。也可以重复使用 `--test`（pytest 和 Go）或 `--file`（Vitest）。例如：

```sh
reprolab project review ./repo --before BASE --after CANDIDATE \
  --selection-file ./regression-tests.txt --acceptance-plan ./acceptance-plan.json \
  --verify-plan --output ./review
```

显式范围决定实际运行的测试，计划决定最终判定。请把所有必验目标包含在显式范围内。缺少计划目标时，报告保留，验收将该目标标为受阻。本轮也发现明确失败时仍返回 `1`。锁定基线不符时在运行两版前返回 `2`。只检查回归门槛和证据要求的空目标计划也可与显式范围一起使用。

### 将验收摘要写入 GitHub Actions

开发版的 pytest `review`／`run`、Go 和 Vitest 支持 `--summary-file FILE`。它从同一轮已校验的报告保存 Markdown。使用 `--verify-plan` 时，摘要还包含本次计划验收结果。报告包含明确失败或受阻项时，仍会生成摘要并保留验收退出码。若基线不符等原因导致没有可核对的报告，只有命令诊断，没有 Markdown 文件。

摘要使用 UTF-8，父目录须已存在，目标须是新文件。写入成功时 stdout JSON 包含 `summary_file`。如果无法写入，JSON 仍保留 `report_dir`、`acceptance` 和 `summary_error`。此时已确认失败仍返回 `1`，其余情况返回 `2`。计划中的必验测试名会出现在 Markdown 中。

以下 pytest workflow 使用普通 `pull_request`、GitHub 托管 runner 和只读权限。先在仓库中设置 `REPROLAB_WHEEL_URL` 指向支持这里各选项的固定版本 wheel，再按项目调整依赖安装步骤。从桌面验收结果导出 CI 配置后，将两份 JSON 放入目标分支的 `.ci/` 并核对其中的目标。门禁比较事件目标提交与已核对父提交的合并结果，并使用目标提交中的计划和选择清单。PR 对这些规则的提议可另行核对。

```yaml
name: Review selected regressions

on: pull_request

permissions:
  contents: read

jobs:
  repair-acceptance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/setup-python@v7
        with:
          python-version: '3.14'
      - name: Install test dependencies and ReproLab
        env:
          REPROLAB_WHEEL_URL: ${{ vars.REPROLAB_WHEEL_URL }}
        run: python -m pip install -r requirements-test.txt "$REPROLAB_WHEEL_URL"
      - name: Check the merge result against the target plan
        shell: bash
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          run_dir="$(mktemp -d "$RUNNER_TEMP/reprolab.XXXXXXXX")"
          if ! git show "$BASE_SHA:.ci/regression-tests.json" > "$run_dir/selection.json" ||
             ! git show "$BASE_SHA:.ci/acceptance-plan.json" > "$run_dir/plan.json"; then
            echo 'ReproLab blocked · target selection or plan unavailable.' >> "$GITHUB_STEP_SUMMARY"
            exit 2
          fi
          verdict=0
          reprolab project review "$GITHUB_WORKSPACE" --github-event "$GITHUB_EVENT_PATH" \
            --pr-mode merge --merge-commit "$GITHUB_SHA" --audit-discovery \
            --python "$(command -v python)" --selection-file "$run_dir/selection.json" \
            --acceptance-plan "$run_dir/plan.json" --verify-plan \
            --summary-file "$run_dir/summary.md" --output "$run_dir/review" \
            > "$run_dir/result.json" || verdict=$?
          if [[ -f "$run_dir/summary.md" ]]; then
            cat "$run_dir/summary.md" >> "$GITHUB_STEP_SUMMARY"
          else
            printf 'ReproLab blocked · no checked merge summary (exit %s). See the step log.\n' \
              "$verdict" >> "$GITHUB_STEP_SUMMARY"
            if [[ -s "$run_dir/result.json" ]] &&
              status="$(python -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["status"]))' \
                < "$run_dir/result.json")"; then
              printf 'ReproLab result status: %s\n' "$status"
            fi
          fi
          if [[ ! -f "$run_dir/summary.md" && "$verdict" == 0 ]]; then exit 2; fi
          exit "$verdict"
```

Go 或 Vitest 可替换为各自的命令和显式测试范围，保留同样的验收参数与退出码处理。示例只将 Markdown 发送到任务摘要。按项目的数据要求另行决定是否上传完整证据，报告可包含源码快照、日志和本机环境信息。[GitHub 任务摘要](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#adding-a-job-summary) · [GitHub PR 运行权限](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)

### 比较目的和计划变更

`review --github-event EVENT --pr-mode head` 从唯一共同祖先比较到 PR head，用于检查分支改动。`--pr-mode merge --merge-commit SHA` 从事件目标提交比较到已核对两位父提交的合并结果，用于检查合入。手工指定两个版本会标为指定版本对比。摘要开头显示比较目的、实际版本、范围及门禁结果。公开摘要保留比较类型和数量。已有 ZIP 可重新生成新版摘要。

```sh
reprolab project compare-plans ./evidence.zip --plan-path .ci/acceptance-plan.json \
  --selection-path .ci/regression-tests.json
```

`compare-plans` 从两版冻结快照读取同一路径的计划，用同一份已核对报告计算两个结果。它显示计划来源、指纹、变化的规则和各自的门禁。命令的退出码取目标计划，候选计划的结果供审查提议。可选的 `--selection-path` 核对两版 pytest 的独立测试清单，显示未覆盖的测试和建议复跑清单。若计划有额外的回执或稳定性要求，摘要显示缺少的材料，可另用 `summary` 带相应材料核对。报告仅保存共同祖先而未包含目标提交时，目标计划受阻，需使用合并模式获取对应证据。

### 离线核对收到的 PR 摘要

拿到 `project.zip`、`review-summary.md` 和生成摘要时使用的验收计划后，接收者可以直接核对内容。核对只读取保存的证据。

```sh
reprolab project check-summary ./project.zip ./review-summary.md \
  --acceptance-plan ./acceptance-plan.json
```

`check-summary` 支持与 `summary` 相同的验收和用例显示选项。请传入生成原摘要时的策略、显示选项、计划与外部证据。命令先核对项目证据，再重建当前摘要和已支持的历史摘要。完整 Markdown 必须匹配其中一种，允许 CRLF 换行。`different` 可能来自格式不受支持、选项有异或内容被编辑。

- 一致时返回 `0`，JSON 的 `status` 为 `consistent`。内容不同返回 `2`，`status` 为 `different`。证据校验失败或摘要无法读取时也返回 `2`，错误写入 stderr。
- `markdown_format` 记录匹配的是 `current`、`pre_phase`（增加阶段判断前）、`pre_scope`（增加运行范围前）或 `cb56b04`。配套 JSON 须匹配同一格式。当按旧规则重建的门禁与当前不同时，`historical_gate` 保留旧决定。`cb56b04` 格式也会返回其历史门禁。`gate` 始终记录当前规则的业务决定及其退出码。CI 直接执行验收时，使用 `project summary` 或 `--verify-plan`。
- `expected_markdown_sha256` 是当前规范正文的指纹。`matched_markdown_sha256` 是匹配到的规范正文指纹。摘要核对使用完整内容。要求签名批准时，请同时提供原计划、签名、可信公钥和预期身份。签名与业务门禁分别核对。

`project demo --review-handoff --output DIRECTORY` 可生成一套对应文件。JSON 的 `handoff_dir` 指向交接目录。内置案例的折扣修复生效，仍有一项金额显示回归，因此运行返回 `1`，摘要核对返回 `0`。

### pytest 报告的发现、推荐与执行范围

摘要的 `run_scope` 分别给出当轮发现、保存的推荐清单、实际选择和每版观察到的执行。发现与推荐来自 ZIP 挂接并可核验的候选侧发现记录。部分或截断发现的数量为保存的唯一测试 ID 提供的下界。缺失记录显示未知。明确保存的空推荐清单显示零项。推荐计数仅覆盖当轮保存的线索，未纳入项的结果在本轮未知。普通摘要加 `--include-test-ids` 可查看最多 20 个未纳入的推荐及原理由。公开摘要只显示计数和状态。

执行显示每版状态、已开始数量和已完成的 pytest 阶段记录数。跳过或准备失败的测试可能已有完整阶段记录，结论仍见逐项结果。取消或不完整日志只提供已观察到的下界。明确未执行的版本显示零项及未开始数量。推荐和范围信息用于解释证据，门禁仍评估实际选择与验收条件。

### pytest 主体失败与清理异常

`pytest_phases` 分别汇总测试主体（`call`）和准备、清理阶段。例如准备时跳过的测试可能有完整生命周期记录，同时没有 `call` 观察。候选主体通过但清理失败时，整项仍受阻。若基线整项通过，候选准备成功、主体失败且清理也失败，两侧记录完整且可比较时，把主体绿转红计为一项新增失败，门禁返回 `1`。同一测试的清理问题在阶段汇总和逐项记录中另有标记。公开摘要只显示固定阶段状态和计数。

报告的原始 `status`、`counts` 与阶段事件保留按生命周期归并的观察。新版运行可以附带 `phase_decisions`，CLI 同时给出独立的 `comparison_verdict` 和 `comparison_counts`。新版 `summary` 能从带有足够阶段与比较证据的旧 ZIP 重判，若比较计数变化则附上 `observed_counts`。xfail、未完成执行、缺失阶段、不可比较的源码、输入、参数或诊断对照仍按证据约束处理。严格修复案例与重放仍使用各自的生命周期证据条件。

## 输出和退出码

`project` 命令的正常结果写入 stdout。默认输出单个 JSON 对象，`summary` 默认输出 Markdown，`verify-replay` 可用 `--format markdown` 选择 Markdown。`--progress` 将实时进度写入 stderr，不混入最终结果。参数错误、异常和没有形成报告的中断也可能在 stderr 返回错误 JSON；启用进度时 stderr 是混合诊断流，不应当整体解析成 JSON。

| 执行结果 | 退出码 | 自动化处理 |
| --- | --- | --- |
| project review／run 的全局 regression_passed | 0 | 本次范围两版通过或改善，且执行记录完整 |
| 全局 regression_detected | 1 | 存在新增失败；查看逐项状态，同时保留其他受阻信息 |
| attention、cancelled、无可用结果、参数／环境错误 | 2 | 不作通过结论，保留材料并检查原因 |
| prepare、show 等非验收操作成功；collect 获得完整可用清单 | 0 | 只表示该操作成功，不表示修复通过 |
| collect 失败、无测试、部分发现或清单截断 | 2 | 检查诊断和范围；部分清单中的可用测试仍可明确选择 |
| project check 返回 consistent | 0 | 材料相互一致；报告本身仍可能记录失败 |

以上退出码针对 project 命令；严格案例 verify 有自己的结论状态，不能把两个入口的字符串混用。CI 应保留原退出码，不用 `|| true` 隐藏检查失败。即使返回 1 或 2，也可能有完整报告，应保存 result.json 指向的 report_dir。

### 同快照结果分歧与排除改动

两版的冻结源码和普通文件权限相同，但同一用例一版失败、一版通过时，报告保留两次真实结果，比较判为受阻并返回 `2`。若 Git 中有已跟踪的改动被快照规则排除，摘要显示排除数量。两版快照因此相同时返回 `2`。如果快照内也有改动，则继续按实际纳入范围判定，并提示有部分 Git 改动未覆盖。排除记录不保存这些文件的路径和内容。

CLI 的 `comparison_scope` 给出原因，`comparison_verdict` 给出比较判定。原报告的 `status`、`counts` 和 `entries` 仍记录执行观察。`project summary` 可从旧报告的源码与权限指纹重新核对同快照分歧。旧报告未记录的快照排除数量无法离线补算。只生成摘要的 `project summary` 在输出成功时返回 `0`，给出门禁结论时按 `--gate` 或验收要求返回。

验收请求附加回执或稳定性材料时，文件缺失、无法读取或身份不符会使该项受阻。原始报告校验通过且已有明确验收失败时，仍返回 `1`，同时在摘要中显示附加材料受阻。

### 核对 pytest 的规则、导入和顺序

`project collect` 的 `pytest_rules` 记录候选配置路径、采用的 addopts 警告筛选数量，以及是否忽略了其他参数。项目报告为两版分别保存这些字段。两版的 `module_origins` 和 `execution_order` 记录观察到的导入来源与实际启动顺序。旧报告可能缺少这些字段，新的执行按当前规则记录。

项目模块从基线快照切换到候选外部来源，或 `snapshot_modules` 要求未满足时，`comparison_scope` 会列出相关模块和两版来源。原本可通过的比较将返回 `2`，已观察到的新增失败仍返回 `1`。HTML 并排展示请求顺序和两版实际启动顺序，优先展示发生变化的位置。顺序提示只描述观察到的变化。

### 固定顺序复跑对照

两版实际启动顺序不同时，可从原 pytest 报告或证据 ZIP 发起一次对照。先预览确定的顺序，再明确运行：

```sh
reprolab project order-control ./source.zip
reprolab project order-control ./source.zip --confirm-run --output /tmp/reprolab-order-test
```

默认 `--order baseline`，采用基线完整的实际启动顺序。也可用 `--order requested`，让两版都按原来的请求顺序执行。两种模式都沿用原报告的源码快照、所选测试和共享测试文件。工作进程在收集钩子之后应用指定顺序，并逐项核对实际启动。如果基线顺序记录不完整，预览会提示改用原请求顺序或重新取得基线证据。

`order-control` 的 `0` 表示对照已完整执行，包括对照中仍有实际失败的情况。`2` 表示对照受阻或观察不完整。报告记录 `order_control`、来源指纹和两版实际序列。按计划重新验收时，对照中观察到的绿灯会单独标为受阻。对照报告可以按同一顺序重放，原生顺序运行和固定顺序运行属于不同的重放输入。

### 基线和候选的执行先后对照

正常 pytest 比较先跑基线 A，再跑候选 B。缓存、数据库或其他共享状态可能让第二个执行的版本受益。用 `version-order` 从同一份已校验的 pytest 报告另存 A→B 和 B→A 两组运行。两组沿用冻结源码、选择范围和原报告已有的固定用例顺序设置。`order-control` 控制单个版本内的用例顺序。

```sh
reprolab project version-order ./original.zip
reprolab project version-order ./original.zip --confirm-run \
  --python /path/to/project-env/bin/python --output /tmp/reprolab-version-order > order-result.json
```

默认只预览，不执行项目代码。明确确认后，每组两个测试进程共享一个 `REPROLAB_PAIR_STATE_DIR`，另一组得到独立的空目录。这些临时目录在各组结束后清理。项目可让测试 fixture 将缓存或本地状态写入该目录。该变量只提供给正式测试进程。数据库、远端服务和其他文件的重置需要由项目控制，报告会标记这些状态未知。运行时仍须提供原 pytest 配置声明的本机变量。

JSON 保存两组报告路径和逐项观察，`directory` 下的 `version-order.md` 按执行位置展示四列结果。第二个执行的版本总是通过，支持顺位效应这一解释。候选两次都通过且基线至少一次失败，说明候选在本次两种位置下都通过。缺少一组、有测试超时／取消或执行条件出现已知差异时，受影响的观察标为证据不足。

`version-order` 返回 `1` 表示原报告严格门禁或任一对照报告仍有新增失败，`2` 表示证据不完整或原报告严格门禁受阻，`0` 表示对照完成且没有上述情况。两组报告属于诊断证据。CI 修复验收继续读取原报告及原计划。对照报告的绿灯无法通过 `project summary --gate` 或 `--require-improvement`。

把 JSON 中 `reports.ab` 和 `reports.ba` 指向的报告分别用 `project pack REPORT --output FILE.zip` 导出。向接收方交付原始 ZIP、这两个 ZIP 和 Markdown。原始报告、两组报告可分别核对，接收方还可从三份证据离线重算四列结果和报告指纹：

```sh
reprolab project check-version-order ./original.zip --ab ./ab.zip --ba ./ba.zip \
  --markdown ./version-order.md
```

`check-version-order` 接受报告目录、`report.json` 或 ZIP，无需被测项目、原 Python 环境或缓存。如果只收到一组，可只传相应参数，结果会保留已知观察并标记证据不足。返回 `0` 表示对照完整且提供的 Markdown 一致。缺组、受阻、给错报告、两组来自不同实验或 Markdown 不一致均返回 `2`。复跑完整对照时，从原报告再次调用 `version-order`。

### 核对测试保护范围与故障换型

pytest 的 `project review` 和 `project run` 自动采用候选发现清单时，会另行从冻结基线收集其原生测试。为避免额外收集给两版执行预热，基线收集在两版运行后进行。显式选择范围时，可加 `--audit-discovery` 做同样核对。

报告独立保留执行结果和两版发现差异。完整基线独有项会区分原测试文件在候选快照中缺失、可能改名和候选未发现。可能改名的线索是新 ID 保留了原用例名称，或同一个文件只少一项并多一项。可能改名仍需要审查覆盖是否相同。任一版发现不完整时保留已知计数，暂停缺失项的归类。发现缺口使门禁受阻，已确认新增失败仍返回 `1`。

pytest 和 Vitest 可核对同一用例两版保存的异常类型，pytest 还核对失败阶段。宽松门禁遇到换型时要求复核。异常消息和参数的变化仍可在各版原始报告中查看。故障换型默认显示计数，用 `--include-test-ids` 查看对应的 ID 和差异，完整证据保留逐项记录。

### 重放基线夹具输入

pytest 所选测试的相关 `conftest.py` 在两版间发生变化时，报告保留文件指纹。该测试观察到红转绿时，门禁要求核对夹具变化。可以先预览，再让候选使用冻结基线中的文件复跑。

```sh
reprolab project fixture-control ./original.zip --file tests/conftest.py
reprolab project fixture-control ./original.zip --file tests/conftest.py \
  --confirm-run --output /tmp/reprolab-fixture-control
```

`--file` 可重复指定文件型夹具及其辅助文件，路径须相对于仓库根。原始报告和快照原样保留。候选测试副本使用这些基线文件，工作进程在载入测试前记录文件指纹。报告含来源指纹及两版原文件指纹，可正常打包、校验和重放。若候选复跑仍失败，原先的绿灯可能依赖输入变化。诊断完成返回 `0`，正式验收用原报告及验收计划。这个入口固定显式指定的基线文件，动态生成的对象和外部数据需要项目自己的重现机制。

### 保存并重放动态业务输入

pytest 测试可以使用 ReproLab worker 提供的 `reprolab_input` fixture。为每个要复现的值选择一个稳定名称，并传入按需取值的函数。

```python
def test_nonnegative_balance(reprolab_input):
    transaction = reprolab_input("transaction", generate_transaction)
    assert balance(transaction["amount"]) >= 0
```

同一个测试若也要直接用原生 pytest 运行，可以把下面的辅助函数放在项目测试中。原生 pytest 没有此 fixture 时仍走原来的取值函数。

```python
import pytest


def recorded_input(request, name, factory):
    try:
        capture = request.getfixturevalue("reprolab_input")
    except pytest.FixtureLookupError:
        return factory()
    return capture(name, factory)
```

测试通过 pytest 的 `request` fixture 调用 `recorded_input(request, "transaction", generate_transaction)`。

常规运行会调用取值函数并在两版原始事件中记录值。逐项证据保留样本指纹，报告标明哪些业务输入发生变化。某项观察到红转绿而已记录的值或名称不同，正式门禁会受阻。测试可记录字符串、布尔值、有限数值、`null`、数组和字符串键对象。每个测试每个名称只使用一次，单项最多 16 KiB、一版合计最多 1 MiB。测试得到 JSON 值的独立副本，后续修改不影响已保存输入。

用 `input-control` 从基线已完整失败且有输入记录的原报告复跑。两版仍用原来的冻结输入及测试范围，目标测试从证据取值，跳过对应的生成函数。默认固定全部符合条件的基线失败测试，可重复指定 `--test` 仅固定部分目标。

```sh
reprolab project input-control ./original.zip
reprolab project input-control ./original.zip --confirm-run --output /tmp/reprolab-input-control
reprolab project pack /path/to/control-report --output ./control.zip
reprolab project check-input-control ./original.zip ./control.zip
```

缺少、重复、未使用的样本以及无效 JSON 会保留为受阻。若重跑不再出现同类型基线失败，或实际导入来源需要核对，诊断不会判定候选通过了原始检查。`candidate_passed` 或 `candidate_failed` 记录候选在该样本的结果。`0` 表示诊断完整，`2` 表示证据不足，已确认的新增失败保留 `1`。正式验收仍读取原报告及原计划。

样本值保存在原始事件和对照证据包中。只记录可交付的测试输入。输入漂移默认显示计数，`--include-test-ids` 可查看涉及的测试与输入名称。fixture 固定由测试明确请求的值。依赖数据库或外部服务时，应把决定业务行为的值传给被测入口，并由项目控制相关状态。

### 持续保护已接受的修复

当一份报告按原计划验收通过后，可以用以下命令生成长期保护计划。

```sh
reprolab project protect-plan ./accepted.zip ./repair-plan.json --output ./protection-plan.json
reprolab project review /path/to/repository --before BASE --after CANDIDATE \
  --acceptance-plan ./protection-plan.json --verify-plan --output /tmp/reprolab-protection
```

新版计划会把原红转绿目标移入 `required_passes`，并把获准报告的实际测试选择保存为 `approved_selection`。可用于 pytest、Go 和 Vitest。持续目标要求候选通过，基线合入修复后也可以复用。生成新计划时会重验原计划，原计划所需回执或稳定性材料可一并提供。新计划使用后续比较的基线，并继续要求原计划的政策、回执和最少稳定轮数。

执行时省略显式清单会采用已批准范围和必验项。用 `--verify-plan` 评估一份显式清单时，摘要会列出缺失、新增及共同测试顺序变化。漏掉任一已批准项会受阻，确认的回归仍优先报失败。旧计划格式和指纹继续可用。

### 将失败样本纳入持续保护计划

如果原始比较因为业务输入漂移受阻，先用 `input-control` 确认候选能处理基线失败的样本。再把原报告、对照报告与原修复计划一起交给 `protect-plan`。

```sh
reprolab project protect-plan ./original.zip ./repair-plan.json \
  --input-control ./control.zip --output ./protected.json
reprolab project review /path/to/repository --before BASE --after CANDIDATE \
  --acceptance-plan ./protected.json --verify-plan --output /tmp/reprolab-protected
```

对照必须来自同一原报告。样本目标须属于原计划的必验修复项。生成计划前会核对两版样本消费、候选结果、原比较政策及计划所需材料。其他回归、保护范围缺失及已记录的运行条件差异仍会阻止生成。对照仅能消除已核对目标的输入漂移疑点，原报告及门禁保留。

新计划使用格式 4，包含原报告与对照报告的指纹、固定业务样本、持续通过目标和已批准范围。以后每次按计划运行，两版都使用保存的样本。报告记录运行时的完整计划指纹和样本消费。实际失败返回 `1`。遗漏样本目标、测试不再请求样本、报告由不同计划或普通运行生成时，验收受阻。计划 JSON 含样本原值。

将新报告的 ZIP、Markdown 和计划交给接收方，用 `project check-summary ... --acceptance-plan ./protected.json` 离线复核。若要独立核对计划的样本来源，可从原始 ZIP、对照 ZIP 和原修复计划重新运行 `protect-plan`，比较生成的计划指纹。已有计划格式 1–3 与其历史摘要继续可用。

### 对照旧新实现与旧新测试

普通 pytest 比较让基线和候选使用同一份候选测试文件。`test-source-control` 从原报告用基线测试文件复跑两版，`check-test-source-control` 则从两份证据核对四格结果。测试文件缺失时预览会说明，测试 ID 无法在实际执行中匹配时保留受阻。实现、配置及其他辅助文件沿用原比较。固定业务样本等已声明设置继续使用。对照用于解释原结果。

```sh
reprolab project test-source-control ./original.zip --confirm-run --output ./control
reprolab project check-test-source-control ./original.zip ./control.zip
```

### 固定持续保护的测试契约

确认固定样本计划在已批准的测试和辅助检查上通过后，可以用该报告生成版本 5 计划。

```sh
reprolab project pin-test-contract ./passing.zip ./protected.json \
  --file tests/support.py --output ./contract-plan.json
```

后续 `--verify-plan` 同时核对样本和候选执行的测试契约。契约包含持续目标所在的测试文件、相关 `conftest.py`、常见测试目录内的 Python 辅助文件，以及用 `--file` 指定的其他依赖。Python 文件使用不含位置和注释的结构指纹，字节指纹用于其他文件或无法解析的 Python 文件。结构相同的格式调整可以通过。改变语法树的重构以及增删辅助文件需要审阅，报告列出变更路径。动态导入、测试目录外的其他辅助逻辑和外部依赖应明确纳入检查范围。需要接受新契约时，由维护者审阅新的测试与反例，再从其验收报告生成新计划。

### 复核候选、计划与分享摘要

`summary` 和 `check-summary` 接受 `--expect-before`、`--expect-after` 和 `--expect-plan-sha256`。版本可用完整 Git 提交，或用 `sha256:...` 绑定冻结源码。CI 应将期望版本和批准计划指纹从审批或工作流传入。旧绿报告若对不上候选会受阻，已确认失败仍保留。 如需时效门槛，可加 `--max-report-age-hours 24 --report-as-of 2026-11-18T12:00:00+08:00`，同一评估时间可用于事后独立核对。

用 `summary --format json` 导出结构化摘要后，`check-summary --facts-file` 可对照 Markdown 与 JSON。两项都必须完整匹配同一套重建结果才返回 `0`。若两项各自匹配不同格式，会同时显示 `markdown_format` 和 `facts_format`，整套材料返回 `2 summary_format_mismatch`。

`summary --public-summary` 只提供比较类型、执行器、计数、门禁和摘要绑定字段。原报告持有者可用 `check-summary --public-summary` 复核。公开 Markdown 的门禁文字可能在不同策略下相同。需要核对具体策略时附上配套的公开 JSON，核对其中的 `gate.policy`。回放、逐例定位和离线验证需要原证据和相应计划。

### 有期限的已知故障豁免

pytest 的 `summary --gate no-new-failures --failure-waivers waivers.json --waiver-date YYYY-MM-DD` 可进一步要求每个已知失败都有有效豁免。用 `summary --format json --include-test-ids` 取得两版失败指纹。豁免记录包含完整测试 ID、`failure_sha256` 和 `expires_on`。到期日当天起失效，未批准、身份变化或缺少故障证据都会受阻。已确认的新失败仍返回 `1`。复核旧摘要时使用同一日期和文件。

```json
{
  "kind": "project_failure_waivers",
  "schema_version": 1,
  "entries": []
}
```

### 核对分片和历史

每个 pytest 片段仍有独立报告。把完整期望清单与各片段一起交给 `project check-shards --selection-file all.txt A.zip B.zip`。核对会列出漏跑、重叠和多余项，并检查共同的冻结输入与已记录条件。分片覆盖完整也仍需要考虑跨片段的顺序与共享状态。

会话索引丢失或报告移动后，可使用 `project runs SESSION --reports REPORTS` 从独立报告重新列出全部历史。显式加 `--rebuild` 可恢复会话内最近 200 份的缓存。报告仍保留原位置和原证据指纹。

`project locate-regression REPO --before GOOD --after BAD --test ID --output DIRECTORY` 按第一父提交历史逐个运行可信 Git 版本，保存每个版本的报告。无需假定提交顺序上的结果单调。端点证实、所有提交完成且输入可比时，输出首个通过转失败的边界。受阻或测试输入变化时保留已观察边界并标记不完整。默认最多 40 个提交，可用 `--max-commits` 设置。

### 同一源码的环境矩阵

分别在各个准备好的 Python 环境运行相同版本和测试范围。然后用 `project check-environment-matrix ENV1.zip ENV2.zip` 复核固定实现、测试范围和已声明控制，并查看各环境的基线／候选结果及发行包变化。虚拟环境由调用方准备，包清单是本轮所记录的环境证据。

### 最小化已观察失败的前置顺序

`project minimize-order SOURCE --test ID --output DIRECTORY --confirm-run` 从报告的候选实际启动顺序开始，复现目标后逐项删除前置测试。已记录的故障指纹和输入用于核对每次尝试。成功结果表示保留序列中每一项都已经尝试单独删除，删除后都无法复现同一记录故障。遇到取消、预算耗尽或不完整时保留试验报告。省略 `--confirm-run` 可只看范围。

### 签署证据来源或人工决定

`project attest` 使用本机 OpenSSL 和明确提供的 PEM 私钥将角色、决定、说明、可选工作流标签及计划指纹与报告绑定。签名记录是独立文件。记录人工决定后，原比较和 CI 门禁仍可独立复核。

```sh
reprolab project attest ./project.zip --role reviewer --actor maintainer \
  --decision approved --reason "Reviewed the fixed scope" --plan ./plan.json \
  --key ./private.pem --output ./decision.json
reprolab project check-attestation ./project.zip ./decision.json \
  --expect-role reviewer --expect-actor maintainer --plan ./plan.json --trusted-key ./public.pem
```

生产者可用 `--role producer --decision recorded --workflow LABEL` 声明工作流。接收方从独立信任渠道取得公钥，可用 `--expect-workflow` 核对期望标签。记录中的 UTC 时间由签署者提供。组织权限分配、密钥分发和工作流身份的外部认证由使用方负责。

### 比较实际安装的 Python wheel

可把两份已构建的 wheel 放入可信的验证 Git 仓库，分别提交，以普通 pytest 比较冻结其字节。同一份下游测试在各版快照中读取本版 wheel，并安装到本次测试自己的临时目录。用独立的 Python 进程从安装目录导入，再测试下游实际调用的 API、资源或命令入口。

例如，给两份 wheel 使用相同的相对目录 `dist_artifacts/`。下游测试以当前版本快照中的唯一 `*.whl` 作为输入，并在 `tmp_path` 中执行 `python -I -m pip install --no-index --no-deps --target PATH WHEEL`。该安装方式适用于无运行依赖的 wheel。带依赖或平台限制的制品应提供固定依赖和相应平台的解释器。用 `project review --test` 运行下游测试，随后照常打包并离线核对报告。报告的冻结源码指纹在这个流程中同时锁定 wheel 字节；证据包仍受常规大小限制。

### 冻结时检查证据 ZIP 路径

`project prepare` 返回 `archive_preflight`。从该会话生成的报告也会保存此项。Go 和 Vitest 在准备测试输入时核对。`blocked` 表示有路径会阻止单份证据 ZIP 导出，例如文件名含冒号。`review` 表示存在 Windows 文件名限制、大小写或 Unicode 规范化冲突，需结合接收方文件系统核对。`issues` 列出基线或候选及对应相对路径，最多显示 24 项。预检聚焦冻结源码路径，本地测试仍可运行，ZIP 容量和历史链在导出时另外核对。

## 程序读取报告

项目报告的 kind 为 project_regression，schema_version 当前为 1。常用字段：

| 字段 | 含义 |
| --- | --- |
| status、counts、entries | 总体结果、分类数量和逐项结果 |
| selected_tests、shared_tests | 本次所选精确 ID 和实际共享测试文件指纹 |
| collection.collection_path | 实际发现范围的相对路径，空字符串表示 pytest 默认范围，旧报告可缺省 |
| discovery_audit | pytest 两版原生发现完整性、计数和基线独有项 |
| fixture_changes、fixture_control | pytest 相关 conftest.py 的变化，以及明确指定的基线夹具对照文件 |
| input_control、entries[].before/after.input_digests | 固定动态样本的来源与目标，以及各版实际请求的样本指纹 |
| go_report_version、entries[].case_entries | Go 逐子测试报告版本及完整名称对应的观察 |
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

两版共享候选的所选测试文件及声明的 pytest 插件／导入方式，各自保留源码、项目配置和 fixture。测试文件相同并不代表所有执行条件相同。插件自动加载关闭。pytest 对比采用各版实际配置中 addopts 的警告筛选。可明确声明普通值和必须从本机提供的变量，见[运行变量](runtime-variables.md)。runtime_environment 记录普通值和本机名称，不保存本机值；项目重放预览的 required_local_variables 列出需重新提供的名称。

`project replay SOURCE` 接受报告目录、`report.json` 或证据 ZIP，可直接预览、预检或确认重放。ZIP 预览的 JSON 用 `source_archive` 记录原包路径，报告目录预览用 `report_dir`。确认重放时，`report_dir` 指向新保存的报告。

pytest 报告可用 `project replay SOURCE --python /path/to/local/python --check-environment` 核对接收方环境。它在证据校验后启动选定的可信解释器，核对 Python 和 pytest、读取发行包与插件入口元数据，测试收集和插件加载留到正式重放。输出的 `environment_check` 记录待处理依赖、插件及与原报告的差异。已知环境阻碍返回 `2`，需要核对的差异返回 `0` 和 `status: review`。插件元数据无法确认时应核对本机安装。普通预览保持只读，明确执行使用另一条带 `--confirm-run` 的命令。可按 [async-lru 真实 PR 交接案例](replay-handoff-case.md) 复跑。

## 重放结果核对

`project verify-replay SOURCE RESULT` 核对两份已保存的项目报告，接受目录、`report.json` 或证据 ZIP。它先校验证据，再比较两版源码与普通文件权限、共享测试文件、测试清单和顺序，以及 pytest 发现范围或 Go 包。两份报告须来自独立运行。

每项核对来源与重放的基线／候选通过或失败状态。pytest 按测试 ID，Go 按顶层测试函数，Vitest 按文件、项目和用例。跳过、预期失败、重试后通过、缺少逐项记录或进程中断都保留为待核对。Python 包、工具链、系统架构、时限等已记录差异会单列。绝对解释器路径变化可在环境信息中查看。结论描述所选结果是否重现，故障原因和整个项目的状态仍以实际测试报告为准。

已审阅并信任证据时，可以从 ZIP 直接重放并核对。重放本身的测试判定保留在输出的 `status` 字段，`replay_verification` 提供独立核对结果。保存标准输出即可留档。

```sh
reprolab project replay ./handoff.zip --confirm-run --verify-result \
  --python /path/to/local/python --output ./replays > replay-result.json

report="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["report_dir"])' < replay-result.json)"
reprolab project verify-replay ./handoff.zip "$report" > replay-check.json
```

| 核对状态 | 退出码 | 含义 |
| --- | --- | --- |
| `verified` | 0 | 固定输入一致，且所选逐项结果完整一致。两次都观察到同样的失败状态也可通过这项核对。 |
| `different` | 1 | 固定输入一致，发现可以核对的逐项结果变化。 |
| `incomplete`、`inputs_changed`、`same_run`、`unavailable` 或证据错误 | 2 | 输入有变化、两份记录属于同一运行，或证据与结果不足以完成核对。 |

此退出码用于 `verify-replay` 和带 `--verify-result` 的确认重放。普通重放仍按测试判定返回退出码。核对结果是由两份已保存证据计算的摘要，执行证据留在原报告和重放报告中。输出可能包含测试名和环境路径，分享时按项目的数据范围处理。

### 接收方回传证据

`--receipt-dir` 会开启重放核对，并在同一次命令中生成回传材料。需要 `--confirm-run`，回执目录必须是新目录。

```sh
reprolab project replay ./handoff.zip --confirm-run --receipt-dir ./receipt \
  --python /path/to/local/python --output ./replays
```

输出 JSON 中的 `receipt_dir` 指向三个文件。

- `replay-evidence.zip` 保存新运行的完整证据。
- `replay-check.json` 保存逐项核对与环境差异，内容与命令输出的 `replay_verification` 相同。
- `replay-receipt.md` 是可附到 PR 或 CI 的摘要，默认仅显示统计、报告指纹和核对项目。

有可核对证据时，结果变化或不完整也会保存回执，退出码沿用上表。回执写入失败时返回 `2`，JSON 保留 `report_dir`、`replay_verification` 和 `receipt_error`，可从已保存的报告继续处理。ZIP 和核对 JSON 可包含源码、日志、测试名和本机环境信息，回传前按项目的数据范围审阅。

已有两份保存的证据时，可以用 `pack-receipt` 离线补做回执。两份证据均可使用报告目录、`report.json` 或 ZIP。

```sh
reprolab project pack-receipt ./handoff.zip ./saved-replay.zip --output ./receipt
```

输出中的 `status: saved` 表示回执已经保存，测试核对结论在 `replay_verification` 中。若两份报告来自同一次运行或固定输入不同，返回 `status: not_saved` 和退出码 `2`。其余核对结果使用上表退出码。

### 发送方验收回执

用原始证据和回传目录，可离线复核整份回执。该操作只校验证据与重新计算结果，无需安装被测项目的依赖。

```sh
reprolab project check-receipt ./handoff.zip ./receipt > receipt-check.json
```

输出的 `kind` 为 `project_replay_receipt_check`，`schema_version` 为 `1`。核对重新读取两份报告，并将计算结果与接收方保存的 JSON 和 Markdown 核对。JSON 的空白和字段顺序，以及 Markdown 的 CRLF 换行，可在传输时变化。

- `status: consistent` 表示回传材料与证据相符。退出码按上表取决于 `replay_verification.status`，例如有真实结果变化时返回 `1`。
- `status: inconsistent` 表示证据无法校验，或某个回执文件缺失、无效或内容不一致，返回 `2`。`issues` 标明问题位于证据、JSON 或 Markdown。证据有效时仍会给出重新计算的 `replay_verification`。

回执核对描述两份已保存报告及摘要的一致性。报告的哈希用于内容核对，接收方身份和独立机器信息仍由交接双方确认。

### 单独生成 Markdown

需要把接收方的核对结果附到 PR 或 CI 时，可以从两份证据生成英文 Markdown 回执。

```sh
reprolab project verify-replay ./handoff.zip "$report" --format markdown > replay-receipt.md
```

回执包含两份报告的 SHA-256 和原始测试判定、所选结果计数、固定输入检查和已记录的运行条件差异。默认省略日志、源码、本机路径、包清单和测试名。`--include-test-ids` 可附上最多 20 项，优先显示有变化或未能核对的结果。附测试名时先审阅是否含项目数据。Markdown 与默认 JSON 使用同一套退出码。证据校验失败时只在 stderr 输出错误，无法生成回执。接收方可用相同证据重新生成回执，核对报告指纹和逐项结论。

## 多轮重放稳定性检查

用同一份可信项目证据连续运行多轮。每轮从原始报告的固定版本和选择范围开始，并独立保存两版报告和日志。支持 pytest、Go 和 Vitest，可沿用相应重放的工具链与超时选项。

```sh
reprolab project replay ./handoff.zip
reprolab project stability ./handoff.zip --confirm-run --rounds 3 \
  --output ./stability-checks --progress
```

`--rounds` 范围为 2–10，默认 3。标准输出和 `replay-stability.json` 使用 `project_replay_stability` schema 1，包含原报告指纹、各轮报告路径与核对结果、逐项观测和计数。每完成一轮更新文件。停止时保留已保存的报告，摘要会记录已取得的轮次。

逐项状态为 `matched`（每轮复现）、`consistently_different`（每轮都以同一种方式偏离原结果）、`variable`（至少两轮有不同的基线／候选结果）或 `incomplete`（记录不足）。只有固定输入确认一致、且该轮两侧有可核对的通过或失败结果，才会计入观测。运行条件差异在各轮分别记录。

退出码 `0` 表示所有轮次完整复现，`1` 表示轮次完整且观察到变化或持续偏离，`2` 表示取消、材料不全或其他阻碍。未完整执行时，摘要仍会列出已观察到的变化。结论描述本次所选测试和实际轮次。

### 查看历史并离线复核

```sh
reprolab project stability-history ./handoff.zip --output-root ./stability-checks
reprolab project check-stability ./handoff.zip ./stability-checks/replay-stability-EXAMPLE
```

`stability-history` 按原报告的会话、运行 ID 和报告指纹列出最多 50 条匹配的保存记录。输出的 `recorded_status` 是保存时的状态。打开一条记录时使用 `check-stability`，接受检查目录或其中的 `replay-stability.json`。移动检查时一起保留同级的 `reports/`。复核会重新校验轮次身份及证据，并用报告重新计算逐项结论。缺失、重复或证据不一致的轮次列在 `history_issues`，原轮次编号保留，退出码沿用上文。两条命令只读取保存的材料。

## CI 的使用范围

在已有可信项目的 CI 中，先按项目原有方式准备解释器和依赖，再调用上述流程；上传 result.json 和报告目录时遵循项目的数据公开范围。ReproLab 本身不负责部署数据库、启动服务或提供安全沙箱。

仓库自带工作流用于验证 ReproLab 自身：两组 Python／pytest 核心检查、Python 包构建、干净环境安装后的 CLI 检查。它不等同于每个接入项目的测试流程，也没有自动发布 Release。

## 复查输出中的自动进展

`project recheck` 产生报告后，输出附加 `review_progress`。桌面与 CLI 使用同一证据校验和比较函数：`status` 为 `compared` 时，`comparison` 保留完整 schema 3 比较；`summary`、`scope_summary` 提供简短进展与范围。来源丢失或不一致返回 `unavailable`，停止核对返回 `cancelled`，未绑定来源返回 `not_applicable`；这些情况的 `comparison` 均为 `null`，不能推断已解决数量。

这是读取保存证据得出的派生信息，不写回 `report.json`。命令退出码仍取决于本次真实执行报告；跨轮核对不可用不会改写执行结果。消费者应分别读取执行状态和 `review_progress.status`，不能只凭一个退出码推断跨轮修复已确认。尚未生成报告、需先核对测试范围时不返回此字段。手动 `project compare` 入口保持兼容。


## 已有环境的静态检查与继续使用

`environment inspect` 保留原 `status` 和退出码：`review`／`attention` 返回 2，不能当作检查通过。新增 `reuse` 对象说明桌面采用已有解释器的条件：`status` 为 `ready`、`review`（需核对未确认声明）或 `blocked`（存在已知环境／配置问题），`reasons` 保留待处理原因。它不是测试结论，也不是自动准备环境的许可。

CLI 原有显式 `--python` 用法不变；确认已有环境后可用同一个会话路径重新 `project collect`，无需重新冻结源码。采用解释器不自动启用 `addopts` 或环境内其他插件；收集失败、部分发现、截断与测试执行仍按各自原契约判断。
