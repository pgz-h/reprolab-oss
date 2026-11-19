# Choose tests from a real PR / 从真实 PR 准备测试范围

The v0.19.0a25 CLI can freeze a Git comparison, discover pytest IDs and prepare an editable test list in one step. Collection loads trusted project and plugin code. Test bodies run when you execute the separate run command. The two examples below use existing upstream tests. They document local experiments and do not imply upstream adoption.

本版可从固定的 Git 改动生成推荐选测清单。先核对推荐理由和清单，再决定实际运行哪些项。候选快照中的路径和行号可用于定位新增或修改的测试定义。行号供定位使用，纯空行和注释移动不会被计为定义改写。参数化后的每个测试 ID 单独计数。静态分析无法定位的动态用例仍可手动选入。

## async-lru #799: three new regression tests among sixteen

Use the [setup and pinned revisions](replay-handoff-case.en.md) or the [中文环境说明](replay-handoff-case.md). With the upstream checkout and its test environment ready, substitute their paths below. Use a directory outside the checkout for output.

```sh
reprolab project suggest /path/to/async-lru --before c2ba8001e4edef3b196ac3f547021723f8e64914 \
  --working-tree --python /path/to/test-env/bin/python --path tests/test_basic.py \
  --output /path/to/review-output
```

The candidate is based on merge commit `639c403a266d99d223c649cb00fddaf40f371c85` with the plugin declaration in the walkthrough. Among 16 discovered test IDs, three directly map to added definitions in the candidate file. The preview surfaces those three first. The generated run list keeps all 16 in discovery order, including existing tests in the edited file.

| Scope chosen after reviewing the list | Recorded added-definition IDs selected | Observed results |
| --- | --- | --- |
| All recommended IDs | 3 of 3 | 3 improved; 13 passed on both revisions |
| Only the existing 13 IDs | 0 of 3 | 13 passed on both revisions; the three omitted tests had no outcome in this run |

两份报告的严格门禁都通过。只有第一份实际观察到了新增测试由红转绿。第二份的短摘要会显示新增定义关联的 3 个 ID 全部未纳入，即使只看公开汇总也能识别这个范围差别。

If acceptance requires these three named improvements, a reviewer can check that stronger claim against the same ZIP:

```sh
reprolab project summary handoff.zip --gate strict \
  --require-improvement tests/test_basic.py::test_alru_cache_method_is_coroutine_function \
  --require-improvement tests/test_basic.py::test_alru_cache_weakref \
  --require-improvement tests/test_basic.py::test_alru_cache_method_weakref
```

The 16-test ZIP returns `0`. The 13-test ZIP returns `2`: the selected perimeter passed, but the required improvements are missing. This works offline and does not rerun either project version.

### Save a plan before running the tests

From version `0.19.0a25`, you can turn the selection you reviewed into an acceptance plan. Substitute the session and selection paths printed by `suggest` and choose a new output filename. For this PR, the maintainer requires three specific improvements and keeps the 13 other tests in the approved scope.

```sh
reprolab project plan-from-selection "$session" --selection-file "$reviewed_list" \
  --require-improvement tests/test_basic.py::test_alru_cache_method_is_coroutine_function \
  --require-improvement tests/test_basic.py::test_alru_cache_weakref \
  --require-improvement tests/test_basic.py::test_alru_cache_method_weakref \
  --output review-plan.json
reprolab project run "$session" --acceptance-plan review-plan.json --verify-plan \
  --output /path/outside/repo/reports
```

The plan records the baseline, selected IDs, explicit improvement targets and policy. It can also be saved as a new file in the project for code review. Creating it reads the frozen discovery record. Running it is a separate action. A recipient who trusts that plan can apply it offline with `reprolab project summary handoff.zip --acceptance-plan review-plan.json`. If the sender and receiver share an approved plan fingerprint independently, `--expect-plan-sha256` verifies the specified plan as well.

实际核对中，同一份计划作用于 16 项的证据包时返回 `0`，作用于仅跑外围 13 项的包时返回 `2` 并列出缺失的三个目标。生成的计划与执行证据分开，可由审查者先确认范围，再交给 CI 或收件人使用。

## python-dotenv #456: a fix that affects imports from ZIP files

[Upstream PR #456](https://github.com/theskumar/python-dotenv/pull/456) changes `src/dotenv/main.py` and adds `tests/test_zip_imports.py`. The source checkout also contains documentation symlinks. This comparison explicitly freezes `src`, `tests` and `setup.cfg`, covering both changed files and the relevant test configuration. The recorded conclusion applies to that scope.

The following setup was exercised on macOS arm64 with CPython 3.14, pytest 9, click and sh 2. Use a POSIX environment for these upstream tests. The CLI can be installed separately from the test environment.

```sh
case_dir="$(mktemp -d)"
git init -q "$case_dir/upstream"
git -C "$case_dir/upstream" fetch --depth=2 https://github.com/theskumar/python-dotenv.git \
  137bc3dc0b8cf3d417a1e800c4065c526e3fb96a
git -C "$case_dir/upstream" checkout --detach FETCH_HEAD
python3.14 -m venv "$case_dir/env"
"$case_dir/env/bin/python" -m pip install 'pytest>=9,<10' click 'sh>=2,<3'
reprolab project suggest "$case_dir/upstream" \
  --before dd1af684f2586d2c2fdd722f9c45d3212e1e4e59 \
  --after 137bc3dc0b8cf3d417a1e800c4065c526e3fb96a \
  --python "$case_dir/env/bin/python" --path tests/test_zip_imports.py \
  --source-path src --source-path tests --source-path setup.cfg \
  --output "$case_dir/review"
```

Inspect the suggested selection file, then use the printed run command for the frozen session. Both tests were discovered, linked to added definitions, selected and observed to improve. One exercises an import in the pytest process, the other invokes a separate Python interpreter. The baseline log includes the failing path lookup. The candidate passes the upstream assertion in each case.

两项上游测试分别覆盖父进程和子进程。要让第二位维护者核对本轮结果，使用 `project pack` 导出报告，再对生成的 ZIP 执行 `project summary --brief`。收件人可以从包内已保存的发现记录看到选了哪些变更范围。发布版历史案例采用的源码包和旧流程另见[已有适配记录](real-project-adaptation.md)。

## humanize #250: when the source checkout needs a build

A raw Git checkout can be the wrong input for an installation test. At the [humanize #250](https://github.com/python-humanize/humanize/pull/250) merge commit `2968d44280a68f775db73ddeb2f2fc6c05edac84`, importing `humanize` from frozen Git sources stopped discovery before any tests ran. The project's Hatch VCS configuration declares `src/humanize/_version.py` as a build output; that file is absent in both Git snapshots. In v0.19.0a25, a matching missing-module diagnostic identifies the declaration and the missing path. A partial or blocked discovery still cannot produce a ready-to-run suggested selection.

For an installation-level control, we built a wheel from the merge commit and another from its parent `8059ebe1732c89177709476165f6e87cc76fe1b7`. We used the candidate's unchanged upstream `tests/test_filesize.py` in a separate consumer checkout, exercising `test_naturalsize` against both installs. The baseline passed 68 of 70 parameterized cases, and the candidate passed all 70. The two baseline failures were the new cases for a float and a string representing one byte. The local wheels came from the pinned commits in shallow builds; their generated package versions differ from the official published releases.

With the default aggregate policy, `project verify-wheels` returns `2` because the baseline failed. Starting with v0.19.0a25, a recipient can also evaluate the same evidence case by case. After the sender has saved the wheel comparison directory, use:

```sh
reprolab project pack-wheels wheel-comparison --output wheel-evidence.zip
reprolab project check-wheels wheel-evidence.zip --gate regression --format markdown
```

Substitute the actual evidence directory for `wheel-comparison`. The archive contains both wheels, the frozen downstream checkout and the recorded installation/test evidence. Review these materials before sharing. Packing includes only the recorded files and snapshot; keep personal notes outside the snapshot. Reading the ZIP does not install or execute either wheel. See [wheel verification](audit-workflows.md) for the installation procedure.

For this case the opt-in gate returns `0`: 68 cases passed against both wheels and two improved. A check without an explicit gate can verify evidence integrity even when tests failed. To use the result for a CI decision, specify the gate explicitly. Parameter IDs in the comparison can contain private inputs; the default detailed view fingerprints those parameters.

这个例子验证了两条独立路径。直接检查 Git 源码时，缺少由项目构建阶段生成的版本模块，收集受阻，没有产生任何测试结论。分别构建并安装两版 wheel 后，可以读到真实测试记录。每份报告只支持其实际运行形态下的结论。
