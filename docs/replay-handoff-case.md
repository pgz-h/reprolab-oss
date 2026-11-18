# 真实 PR 验收与证据交接

[async-lru PR #799](https://github.com/aio-libs/async-lru/pull/799) 修复缓存方法的协程识别和弱引用回归。ReproLab 运行了 PR 增加的三个上游测试。基线三项失败，候选三项通过。测试范围限于以下三个用例。

| 测试 | 基线 | 候选 |
| --- | --- | --- |
| `test_alru_cache_method_is_coroutine_function` | 失败 | 通过 |
| `test_alru_cache_weakref` | 失败 | 通过 |
| `test_alru_cache_method_weakref` | 失败 | 通过 |

基线是 `c2ba8001e4edef3b196ac3f547021723f8e64914`。候选是 `639c403a266d99d223c649cb00fddaf40f371c85` 加一份本地 ReproLab 插件声明。两版运行候选中同一份上游测试及同一份插件声明。

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

预期回传材料状态为 `consistent`，重新计算的重放核对为 `verified`，且有三项一致。

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
