# Rust, C and C++ / 原生项目测试

ReproLab offers two native CLI runners for fixed-version comparisons. They use the project's own toolchain on two fixed Git commits. The offline receiver only needs ReproLab and the evidence. The desktop's existing pytest, Go and Vitest paths remain available.

| Runner | Pilot scope | What identifies a test |
| --- | --- | --- |
| Cargo | One root package with a checked-in Cargo.lock. Build library unit tests with stable libtest. Dependencies must already be available for locked offline resolution. | The full libtest name, including modules. Each selected test runs separately. Integration tests, doctests, workspaces and custom harnesses are outside this pilot. |
| CMake/CTest | A root CMakeLists.txt, available compiler and project dependencies, CMake and CTest 3.29+. The runner configures a fresh Debug build with BUILD_TESTING enabled. | The full registered CTest name. The runner looks up exact names, records the expanded command, arguments and properties, and runs CTest with one test slot. |

使用前确认仓库可信。预览会读取两个 Git 版本和遗漏改动计数。追加 `--confirm-run` 后，会运行项目自身的构建配置和测试。CTest 可以调用项目配置中的外部工具。Cargo 的离线参数作用于 Cargo 的依赖解析。

## Rust library example

In a Rust repository with tests in `src/tests.rs`, replace the test ID and paths with those from your project. List the files that define the checks you want to compare. Use a new output directory outside the repository.

```sh
reprolab project native . --runner cargo --before HEAD~1 --after HEAD \
  --test 'tests::floor' --shared-test-source src/tests.rs --shared-test-source Cargo.toml \
  --confirm-run --output ../cargo-review
```

The two revisions keep their own sources and build products. ReproLab checks the bytes and file permissions of the test definition files you declare. If the only test definition lives in an edited implementation file, that file has changed and cannot establish a shared check. Native observations remain useful, but they do not qualify for a passing gate under a shared test claim.

## C example from zlib

We tested upstream commit `066e0b68a6c26d6b737bfad2ba395eb1317ca645` against parent `ec9d91df6f5540bb12b5ec8834d98094c6863071`. The commit changes `inflate.c`. These two existing upstream examples exercise the shared and static zlib builds. Both examples passed on both revisions on macOS arm64 with CMake/CTest 3.29.6. This run covers two registered C tests; it does not establish that the original bug was reproduced.

```sh
review="$(mktemp -d)"
git init -q "$review/zlib"
git -C "$review/zlib" fetch --depth=2 https://github.com/madler/zlib.git \
  066e0b68a6c26d6b737bfad2ba395eb1317ca645
git -C "$review/zlib" checkout --detach FETCH_HEAD
reprolab project native "$review/zlib" --runner cmake \
  --before ec9d91df6f5540bb12b5ec8834d98094c6863071 \
  --after 066e0b68a6c26d6b737bfad2ba395eb1317ca645 \
  --test zlib_example --test zlib_examplestatic \
  --shared-test-source CMakeLists.txt --shared-test-source test/CMakeLists.txt \
  --shared-test-source test/example.c --confirm-run --output "$review/evidence"
reprolab project pack-native "$review/evidence" --output "$review/zlib.zip"
reprolab project check-native "$review/zlib.zip"
```

For a C++ executable or a project mixing C and C++, pass its registered CTest names the same way. Exact selection preserves punctuation such as brackets. If CTest would implicitly add other fixture setup or cleanup tests, the runner blocks that selected execution. Review the additional names before including them.

## 读结果 / Reading the gate

- `comparison.gate.exit_code = 0` 表示已核对的选定范围通过或改善。声明文件相同只覆盖声明的范围，其他辅助文件仍可能改变测试含义。
- `1` 表示观察到可比用例从通过转为失败；即使另一项被忽略，报告也保留已确认的新增失败。
- `2` 表示当前证据不足以放行，例如编译失败、未声明共同测试定义、用例缺失或跳过。CTest 的运行命令、参数或属性变化时，同名测试的两次结果仍显示，但不能据此确认修复。
- `snapshot_scope.excluded_tracked_changes` 是发送方在指定 Git 提交间观察到的遗漏路径数。仅有遗漏改动且两边冻结快照相同时，`excluded_changes_only` 为 true，门禁返回 2。混合改动仍展示遗漏数，可比新增失败优先返回 1。显式比较同一个提交时，遗漏数为 0。相同快照出现红绿分歧时，报告保留两次结果，并以 `same_snapshot_divergence` 受阻。
- `check-native` 离线核对快照内容、权限、运行记录及重算的门禁。默认返回 0 表示材料一致，比较结论见 `comparison.gate`。ZIP 的快照散列只约束包内文件。Git 来源、遗漏改动计数和测试输入覆盖声明来自发送方。历史 schema 1 按原有规则核对，遗漏和测试输入覆盖情况显示为未知。

对指定修复进行严格验收，可以在原比较命令上增加 `--require-fix TEST`，并用 `--shared-test-input PATH` 列出测试读入的辅助文件、预期值和测试数据。用 `--attest-test-inputs` 声明这些文件与 `--shared-test-source` 一起覆盖所选测试使用的仓库内测试输入。两种路径选项均可重复。动态依赖由维护者审阅并声明。

```sh
reprolab project native "$repo" --runner cmake --before "$base" --after "$candidate" \
  --test checkout --shared-test-source CMakeLists.txt --shared-test-source tests/checkout.c \
  --shared-test-input tests/expected.txt --attest-test-inputs \
  --require-fix checkout --confirm-run --output "$evidence"

reprolab project check-native "$evidence_zip" --require-fix checkout
```

`fix_acceptance` 在声明文件和配置可比、整个选定范围的普通门禁通过、无遗漏的 Git 改动，且每个必验目标均从失败转为通过时返回 0。可比新增失败返回 1。未声明覆盖、输入变更或缺失、历史证据、未观察到指定改善等情况返回 2。证据包含源码与原始构建日志，分享前应检查范围。

### 使用接收方的验收计划

把计划保存在目标分支或其他可信 CI 输入中，交由负责合并的维护者掌控。计划文件应置于证据目录和 ZIP 之外。将下面示例的完整提交号、测试名和文件路径换成已审阅的值。

```json
{
  "schema_version": 1,
  "runner": "cmake",
  "commits": {
    "before": "1111111111111111111111111111111111111111",
    "after": "2222222222222222222222222222222222222222"
  },
  "selected_tests": ["checkout"],
  "required_fixes": ["checkout"],
  "shared_test_sources": ["CMakeLists.txt", "tests/checkout.c"],
  "shared_test_inputs": ["tests/expected.txt"]
}
```

```sh
reprolab project check-native "$evidence_zip" --review-plan "$trusted_plan"
```

`selected_tests` 和 `required_fixes` 均须显式列出，必验目标须包含在选测中。两份文件清单和选测范围均按集合精确匹配证据，文件须在两个快照中存在且内容与权限相同。确实没有支持输入时可显式填写 `"shared_test_inputs": []`。生成证据时应传入对应的 `--test`、`--shared-test-source`、`--shared-test-input` 和 `--attest-test-inputs`。

`review_plan_acceptance.plan_sha256` 绑定本次读取的计划文件字节。版本、执行器、选测或输入范围不符，以及无法按计划核对的历史格式，均返回 2。在计划范围内，退出码沿用指定修复验收的 0、1、2。提交字段核对的是证据中声明的 Git 提交。独立计划给出了维护者审核的范围，动态依赖及 Git 来源仍需另有证据。

CI 也可以运行 `check-native "$evidence_zip" --verify-result`，直接采用普通比较门禁的退出码。结果中的 `verified_result.rules` 为 `current_schema_2` 或 `historical_schema_1`。历史规则可能返回 0，其含义限于历史比较。此选项、`--review-plan` 和 `--require-fix` 各自选择一种退出语义。

### 在 PR 作业中写入接收方摘要

在只读权限的接收作业中运行下面的步骤。示例假定前一步已把证据 ZIP 下载到 `RUNNER_TEMP/native-evidence.zip`，且接收作业已有固定的可信 ReproLab 安装。接收作业提供当前要评审的可信 PR 事件和独立的本地 Git 仓库，并从事件锁定的目标提交读取测试策略。发送端按下文生成计划并执行。

```yaml
- name: Review native evidence for this pull request
  shell: bash
  run: |
    report="$RUNNER_TEMP/native-review.md"
    verdict=0
    reprolab project check-native "$RUNNER_TEMP/native-evidence.zip" \
      --github-event "$GITHUB_EVENT_PATH" --pr-mode head \
      --policy-path .github/native-policy.json \
      --verify-git-repository "$GITHUB_WORKSPACE" \
      --format markdown > "$report" || verdict=$?
    cat "$report" >> "$GITHUB_STEP_SUMMARY"
    exit "$verdict"
```

示例中的 `GITHUB_WORKSPACE` 须包含事件引用的 Git 对象和完整历史。`GITHUB_EVENT_PATH` 对应触发这次作业的快照。如果 CI 已获知目标或候选提交前进，传入对应的新可信事件。GitHub 重跑原作业会沿用原触发事件的 SHA 和 ref。[GitHub 重跑文档](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)

摘要分别显示材料一致性、普通比较、所选验收结果和接收方使用的 PR 提交，并用测试名的短 SHA-256 指纹标识用例。缺少 Git 提交等提前受阻的情况也会在摘要显示退出 2 和材料尚未核对，详细诊断保留在作业日志。只读核对材料可单独运行 `check-native ZIP`。仓库的 `build.yml` 用两个独立阶段对本仓库的安装版做自测。接收方从 checkout 的固定夹具配方重建普通文件计划，证据由发送方产出。部署到其他项目时由接收方独立决定可信验证器、工作流及策略。

### 用接收方的本地 Git 仓库核对来源

接收方已有可信的本地 Git 对象时，可以在独立计划验收中增加来源核对。

```sh
reprolab project check-native "$evidence_zip" --review-plan "$trusted_plan" \
  --verify-git-repository "$trusted_git_repository" --format markdown
```

ReproLab 按计划中的完整提交号从该仓库读取两棵树，重新计算快照内容、权限和被排除的跟踪改动数量。仓库可以是普通 checkout 或裸仓库。核对使用本地对象，部分克隆的缺失对象不会触发下载。命令不检出版本、不执行项目代码，也不联网。若本地对象无法提供所需数据、快照不符或遗漏数不一致，计划验收返回 2。历史证据仍受阻，报告会显示可独立核对的快照与本地遗漏数，并标明证据缺少原始遗漏计数。

信任来自接收方供应的 Git 对象。这一步确认冻结输入及遗漏记录与这些对象相符，执行者身份和测试实际由谁运行仍需单独证明。接收方只持有 ZIP 和计划时，可省略此选项进行按文件计划的验收。上一节的 PR 事件验收必须提供包含所需对象的本地 Git 仓库。仓库自测由接收端独立重建固定夹具的 Git 对象，并分别检查 ZIP 验收和本地 Git 来源核对。

### 从 GitHub PR 事件生成验收计划

在目标分支保存 `.github/native-policy.json`。它使用上文计划的范围字段，包含 `schema_version`，省略由事件填入的 `commits` 和 `pr_context`。由可信作业提供 PR 事件文件，并预先准备包含目标、候选及所需历史的本地 Git 对象。

```sh
trusted_plan="$RUNNER_TEMP/native-plan.json"
reprolab project plan-native-pr "$trusted_git_repository" \
  --github-event "$GITHUB_EVENT_PATH" --pr-mode head \
  --policy-path .github/native-policy.json --output "$trusted_plan" \
  > "$RUNNER_TEMP/native-plan-receipt.json"
```

发送端使用该计划运行仓库。只有受信任的项目才能执行。下例把证据写到新目录，并保留计划门禁的退出码；确认执行后，失败和受阻的证据也可打包。

```sh
verdict=0
reprolab project native "$sender_repo" --review-plan "$trusted_plan" \
  --attest-test-inputs --confirm-run --output "$evidence" || verdict=$?
test ! -d "$evidence" || reprolab project pack-native "$evidence" --output "$evidence_zip"
exit "$verdict"
```

当前 PR 的接收端直接从可信事件和本地 Git 重建计划并核对证据。发送端记录的计划 SHA-256 必须匹配。计划绑定 PR 模式、目标 SHA 和 head SHA。即使 `head` 模式下目标分支前进前后的共同基线、head 和选测完全相同，旧证据仍返回 2。事件重排或无关字段变化仍可得到相同的计划。

```sh
reprolab project check-native "$evidence_zip" \
  --github-event "$GITHUB_EVENT_PATH" --pr-mode head --policy-path .github/native-policy.json \
  --verify-git-repository "$trusted_git_repository" --format markdown
```

复查历史交接时可用 `--review-plan "$trusted_plan"` 复核留存的计划和 ZIP。旧版生成的计划仍可按原约定用于这种离线复核。当前 PR 的事件验收要求用包含 PR 上下文的计划重新执行。命令只核对所供事件固定的提交，离线运行期间不会查询远端 PR 状态。

显式计划执行必须确认运行。执行器、提交、选测、共享文件及必验项均来自计划；覆盖声明仍需显式传入 `--attest-test-inputs`。命令在执行前一次性读取计划，交接时请保留原计划文件。手填范围的 `native` 继续支持普通比较和预览。

`head` 比较唯一共同基线到 PR head。合入验收用 `--pr-mode merge`，读取事件中的合并提交，或用 `--merge-commit` 显式提供可信 CI 固定的合并结果。合并提交的两个父提交必须依次是事件中的目标和 head。父提交核对确认提交拓扑，合并树本身仍取自提供的 Git 对象。两种模式均从事件锁定的目标提交读取策略。生成回执记录事件、策略和计划文件的 SHA-256，可与原文件及目标 Git blob 独立核对。生成命令仅读取本地对象，原子写入新计划文件，退出 0 表示已生成计划。

### 指定要复现的基线故障

对明确知道旧版故障信号的原生修复，把计划或目标分支策略的 `schema_version` 改为 2，并为每项 `required_fixes` 提供一个字面量标记。例如，必验测试 `checkout` 可配置为：

```json
"baseline_failure_markers": {
  "checkout": "receipt price differs"
}
```

选择稳定、能识别目标故障的短文本。离线验收只查验基线中已独立分类为失败的同名用例。CTest 使用 JUnit 的 `system-out` 和 `system-err`。Cargo 使用稳定 libtest 的单项 `--exact` 日志，且只查该失败用例的独立捕获输出区块。无法唯一识别区块边界时受阻。标记按区分大小写的字面子串匹配。输出缺失或标记不匹配时，指定修复验收返回 2，并保留红转绿观察；已确认新增失败依旧返回 1。其他用例、构建输出与全局日志均不参与匹配。公开摘要只显示匹配数量与判定，原始标记及测试输出留在各自的计划和证据中。

计划 schema 1 继续按原规则验收，历史证据 schema 1 继续支持材料核对。已有 Cargo 证据 schema 2 可在保存的单项日志具备明确失败区块时用于标记验收。标记是接收方审定的观测信号，判断力取决于标记与业务故障的对应关系。可信运行器身份和日志真实性需要单独验证。
