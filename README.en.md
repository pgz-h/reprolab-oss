# ReproLab

**0.19.0a23 Alpha** · [Download the Python packages and macOS Apple silicon app](https://github.com/pgz-h/reprolab-oss/releases/tag/v0.19.0a23). The Windows x64 portable package is pending.

**Check a fix or project change with real tests, and keep replayable evidence.**

ReproLab freezes the source before and after a change, runs the same selected tests on both versions, and saves verifiable reports, logs, and snapshots for replay. Project comparison shows regressions, existing failures, improvements, and blocked results. Strict fix verification checks for the declared failure on the old version and a pass on the new version. AI can help prepare or interpret checks. Verdicts come from execution and apply to the selected tests.

[中文](README.md) · [Documentation index](docs/README.md) · [Installation](INSTALL.md) · [Project Workbench](docs/project-workbench.md) · [Changelog](CHANGELOG.md)

## Try a real historical fix

From a source checkout, run the bundled humanize case. The same test observes `naturalsize("1")` returning `1.0 Byte` before the fix and `1 Byte` after it. No model account or project download is needed.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/reprolab history --output /tmp/reprolab-history
```

This uses original upstream single-module snapshots and a ReproLab-authored behavior test. It is not full-package integration or evidence of upstream adoption. See the [case and provenance](docs/history-case.md).

ReproLab is useful when repeated fixes, patch review or handoffs require more than a one-off pytest run. Existing tox, Nox and CI workflows can continue to prepare environments and run routine tests.

## Try a pull request review from the CLI

After installing the CLI, run the bundled shopping-cart example. The first command tests both versions, verifies the discount fix, and exits with `1` because of a deliberate price-format regression. It saves an evidence ZIP, an acceptance plan, and a PR summary. The final command checks those files from the recipient’s side.

```sh
demo_dir="$(mktemp -d)"
reprolab project demo --review-handoff --output "$demo_dir" > "$demo_dir/result.json" || test "$?" -eq 1
handoff="$(python -c 'import json, sys; print(json.load(sys.stdin)["handoff_dir"])' < "$demo_dir/result.json")"
reprolab project check-summary "$handoff/project.zip" "$handoff/review-summary.md" \
  --acceptance-plan "$handoff/acceptance-plan.json"
```

`status: consistent` means the Markdown matches the evidence. `gate.status: failed` records the regression found during review. The PR-ready Markdown is saved as `review-summary.md` in the handoff directory. [Offline summary verification](docs/cli-contract.md#离线核对收到的-pr-摘要)

The development version can verify a repair plan and save a Markdown review summary in the same run. CI receives a distinct exit code for passed, failed, and blocked results. A recipient can check the evidence from a handoff ZIP offline. [GitHub Actions example](docs/cli-contract.md#将验收摘要写入-github-actions)

## Status

On [async-lru PR #799](https://github.com/aio-libs/async-lru/pull/799), all three selected upstream tests failed on the baseline and passed on the candidate. The [replay case guide](docs/replay-handoff-case.md) records the pinned commits, test scope, and recipient replay steps.

The desktop workbench supports [replay handoff](docs/project-workbench.md#核对导出导入与明确重放). Recipients can save the replay evidence and receipt together. Senders can verify the returned folder offline using their original evidence, then attach the Markdown receipt to a PR or CI summary.

This release offers the Python and pytest workbench, [Vitest 4 comparison](docs/vitest.md) for JavaScript and TypeScript projects, and [single-package Go comparison](docs/go.md) in the desktop Project Check area. Go can discover tests from the candidate snapshot. The Vitest and Go workflows also run from the CLI and produce verifiable, replayable evidence.

Use the linked guides for local development features and GitHub Releases for the scope of each public prerelease. See the [changes](CHANGELOG.md), [platform status](docs/platforms.md) and [validation notes](docs/release-readiness.md).

The macOS arm64 desktop build uses an ad-hoc signature. The 0.19.0a22 Windows x64 portable build passed its native and frozen executable checks in GitHub Actions. Later development changes require their own validation. Downloadable builds are listed in Releases.

## Choose a workflow

| Task | Workflow |
| --- | --- |
| Compare a project change using existing tests | Project Workbench |
| Verify a specific bug fix against a declared old failure | Strict case verification |
| Prepare tests with optional AI assistance | Reviewable AI acceptance plans |
| Inspect, share or replay execution records | Reports and evidence |

The start page offers a small authored fix example and a shopping-cart project example. Creating the project example only prepares code and a frozen session; collection and execution require a separate action.

## Project Workbench

- Freeze two Git commits, or a baseline and the current working tree, without modifying the source repository.
- Prepare snapshots and discover tests in one action, then review the selected scope before running. Explicitly reuse a project's previous interpreter and timeout.
- Discover exact parametrized pytest targets and select 1–2000 per run, with one pytest process per version and normal per-version fixture lifecycles.
- Browse changes, filter tests by directory or file, and see checked tests hidden by filters.
- Reuse named selections across sessions, import/export selection files, and restore selection preferences.
- Refresh the working tree into a new session with the original pinned baseline and selection intent; review every missing ID and retain earlier evidence.
- Inspect per-test outcomes, recorded exceptions and phases, saved test code and practical next steps.
- Compare two saved runs, distinguishing changed outcomes from differences in selected scope.
- Export offline HTML, CSV summaries and evidence ZIPs; validate imports and explicitly replay into new reports.

Both versions use the after-version selected test files while retaining their own configuration and fixtures. Project regression results cover the selected tests; use strict verification to establish a declared target failure and its repair.

## Install

If you have received a matching macOS arm64 validation ZIP, extract it and open `ReproLab.app`. Python and pytest are bundled. Select an environment with your project dependencies.

Public downloads are available only for versions actually listed in the repository's Releases. When an attachment is unavailable, follow the [source installation steps](INSTALL.md).

CLI users need Python >=3.11. Run these commands from the source checkout.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
reprolab demo
```

To check pending changes in a trusted project, replace the repository, interpreter and test ID. Keep the output outside the target repository.

```sh
reprolab project review /path/to/repo --before HEAD --working-tree \
  --python /path/to/project-env/bin/python --test 'tests/test_feature.py::test_behavior' \
  --output /tmp/reprolab-review --progress
```

This freezes both versions, discovers tests and runs the selected scope. The JSON result's `report_dir` contains `report.html`. To compare commits, use `--before BASE --after CANDIDATE`. To inspect the test list before execution, use `project prepare`, `collect` and `run`. See [CLI contracts and CI integration](docs/cli-contract.md) for exit codes, selection files and rechecks.

See [runtime variables](docs/runtime-variables.md) for configuration and supported desktop entry points. Local values are not serialized by the tool into configuration or condition records; test output can still disclose them. Cross-run equality of unrecorded local values remains unknown.

## Boundaries

Trusted code only: collection, configuration, fixtures and tests may execute arbitrary code with current-user file and network access. Subprocesses and copies are not a security sandbox. See the [validation notes](docs/release-readiness.md) for the platforms and workflows exercised.

Evidence consistency, the ability to rerun a check, and repeated identical results are different properties. Dependencies, external services, time and randomness are not fully frozen.

Environment errors, collection/fixture failures, timeouts, skips, xfail and incomplete records do not verify a fix. Snapshots, logs and model prompts may contain project data. Hash manifests check consistency, not author identity, and dependencies are not fully locked.

AI is optional and uses explicitly configured API services. Existing-test verification does not call a model. Review generated code before execution. Real service compatibility and generated-test quality need project-specific evaluation.

See [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), and the [build recipe](packaging/README.md). ReproLab is [MIT licensed](LICENSE); bundled examples and components retain their own notices and licenses.

## Optional model connections

Select DeepSeek, OpenAI, Anthropic, Zhipu GLM or a custom endpoint, enter a key, fetch and select the models you want to keep, then choose a default. A connection test checks only that selected model. Saved models can be switched in settings and the AI acceptance workflow without re-entering credentials. See the [model connection guide](docs/model-connections.md).

AI can draft regression tests, prepare reviewable acceptance plans, and explain recorded results. It does not determine execution verdicts or automatically repair business code. Existing-test comparisons and evidence checks work without a model account.
