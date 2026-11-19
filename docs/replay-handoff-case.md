# 真实 PR 验收与证据交接

[English walkthrough](replay-handoff-case.en.md)

[async-lru PR #799](https://github.com/aio-libs/async-lru/pull/799) 修复缓存方法的协程识别和弱引用回归。ReproLab 运行了 PR 增加的三个上游测试。基线三项失败，候选三项通过。测试范围限于以下三个用例。

| 测试 | 基线 | 候选 |
| --- | --- | --- |
| `test_alru_cache_method_is_coroutine_function` | 失败 | 通过 |
| `test_alru_cache_weakref` | 失败 | 通过 |
| `test_alru_cache_method_weakref` | 失败 | 通过 |

基线是 `c2ba8001e4edef3b196ac3f547021723f8e64914`。候选是 `639c403a266d99d223c649cb00fddaf40f371c85` 加一份本地 ReproLab 插件声明。两版运行候选中同一份上游测试及同一份插件声明。

## 不安装软件也能核对本次记录

这个[公开结果文件](cases/async-lru-799-outcomes.json)来自一次 v0.19.0a24 实跑。文件记录三行脱敏结果、原报告的判定和 SHA-256。它适合快速检查“这三项确实都记录为失败转通过”这句话。取得完整源码后，可在仓库根目录直接用 Python 标准库读取：

```sh
python3 -I src/reprolab/project_projection_format.py docs/cases/async-lru-799-outcomes.json
```

预期输出含 `"counts": {"improved": 3}`（显示时会换行）以及 `"source_verified": false`。独立读者可以重算这三行，但这个小文件没有源码、日志或完整证据包。以下步骤用于实际运行上游测试。每次运行生成自己的报告指纹。

## 发送方复跑

准备一个专用目录、已安装的 ReproLab CLI 和 Python 3.14。此次验证使用 macOS arm64、Python 3.14.7、pytest 9.1.1。下面的 Git 操作都在新目录内进行。

```sh
case_dir="$(mktemp -d)"
git clone https://github.com/aio-libs/async-lru.git "$case_dir/upstream"
git -C "$case_dir/upstream" checkout --detach 639c403a266d99d223c649cb00fddaf40f371c85
cat > "$case_dir/upstream/pyproject.toml" <<'TOML'
[tool.reprolab.pytest]
plugins = ["pytest_asyncio.plugin", "pytest_timeout"]
TOML

python3.14 -m venv "$case_dir/test-env"
"$case_dir/test-env/bin/python" -m pip install \
  -r "$case_dir/upstream/requirements-test.txt" pytest-cov==7.1.0 coverage==7.16.2

reprolab project review "$case_dir/upstream" \
  --before c2ba8001e4edef3b196ac3f547021723f8e64914 --working-tree \
  --python "$case_dir/test-env/bin/python" --path tests/test_basic.py \
  --test tests/test_basic.py::test_alru_cache_method_is_coroutine_function \
  --test tests/test_basic.py::test_alru_cache_weakref \
  --test tests/test_basic.py::test_alru_cache_method_weakref \
  --output "$case_dir/review" > "$case_dir/result.json"

report="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["report_dir"])' < "$case_dir/result.json")"
reprolab project summary "$report" --include-test-ids
reprolab project pack "$report" --output "$case_dir/handoff.zip"
```

## 接收方检查与重放

将可信的 ZIP 放在接收方本机，选择已安装测试依赖的 Python。可以直接从 ZIP 预检；明确启动重放后，结果会保存到新报告。

```sh
reprolab project replay ./handoff.zip --python /path/to/python --check-environment > preflight.json
reprolab project replay ./handoff.zip --python /path/to/python --confirm-run \
  --output ./replays --receipt-dir ./receipt > replay-result.json
```

接收方可保存原 ZIP 的 SHA-256 和 `preflight.json`，审阅后回传 `receipt/` 中的证据 ZIP、JSON 和 Markdown 回执。发送方用以下命令离线核对。该案例预期三项一致，逐项结果均为基线失败、候选通过。

```sh
reprolab project check-receipt ./handoff.zip ./receipt
```

读回执时，先看材料核对的 `status`，再看 `replay_verification.status`。前者为 `consistent` 表示回执与随附证据吻合。后者为 `verified` 才表示所选结果及要求核对的失败材料也匹配。

用公开版 v0.19.0a24 在同一台 macOS arm64 上复跑上述三个测试，原运行和重放都得到三项改善。其中两项的失败记录也匹配。协程识别测试的基线断言消息包含函数及对象在进程内的地址，第二次运行保存的地址不同。因此这一次 `check-receipt` 返回 `1`，材料状态为 `consistent`，重放状态为 `different`。这份结果支持两次都观察到相同的三项失败转通过；严格回执仍保留一项文本差异。

接收方可在工作目录中离线查看逐项记录：

```sh
reprolab project verify-replay ./handoff.zip ./receipt/replay-evidence.zip --include-test-ids --format markdown
```

本版的 `verify-replay --explain` 可以从同一对证据包离线解释 pytest 失败文本的差异。追加 `--format markdown` 查看汇总。JSON 的 `entries[].failure_text_difference` 指明差异位于基线还是候选。若同一条对象引用在消息中出现两次，诊断还会核对重放时两处引用的对应关系。普通十六进制金额、错误代码或字符串变化仍会被报告为文本差异。这个诊断只描述记录到的文本形态；严格重放退出码、历史回执和原始日志保持原有语义。

在 JSON 中查找 `reason: recorded_failure_changed` 可定位发生差异的测试。需要判断业务影响时，核对两份报告中该测试基线一侧的原始异常。失败消息也可能含真实的业务输入，只凭两个指纹无法判断差异是否来自地址。

开发版也可一次验收三个明确目标和接收方回执。任一目标未改善、出现新增失败或回执不能通过复核时，CI 会收到非零退出码。已有多轮证据时可加 `--require-stability PATH --min-stability-rounds 3` 离线复核。

```sh
reprolab project summary ./handoff.zip \
  --require-improvement tests/test_basic.py::test_alru_cache_method_is_coroutine_function \
  --require-improvement tests/test_basic.py::test_alru_cache_weakref \
  --require-improvement tests/test_basic.py::test_alru_cache_method_weakref \
  --require-receipt ./receipt
```

本次在同一台 Mac 的独立 Python 环境验证接收端。发送方的临时项目目录已移走。起初预检发现声明中的两个插件均无法确认，并列出了本机缺少的对应发行包。补齐插件与依赖后，重放再次得到三项改善。两版源码指纹、所选测试清单与来源报告一致，导入的原报告字节保持一致。用 `--verify-result` 从原 ZIP 重新执行时，核对得到三项一致。将同一个 ZIP 与新报告交给 `project verify-replay`，离线核对得到相同结果。独立物理机器和 Windows 尚待验证。

第二轮使用重新构建的开发 wheel，从公开提交生成新证据。发送端为 Python 3.14.7，接收端为 Python 3.13.15，均在同一台 macOS arm64 机器上运行。两端的 pytest 均为 9.1.1。接收端仅从证据 ZIP 重放，三项结果全部一致。用两份证据 ZIP 离线生成回执时，固定输入全部匹配，回执记录了工具链版本差异。
