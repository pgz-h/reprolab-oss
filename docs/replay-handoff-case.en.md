# Review and replay async-lru PR #799

[async-lru PR #799](https://github.com/aio-libs/async-lru/pull/799) fixes coroutine detection and weak references for cached methods. This walkthrough uses the three regression tests from that PR. ReproLab v0.19.0a24 observed all three fail on the baseline and pass on the candidate. The run selected three of the 16 tests discovered in `tests/test_basic.py`. The other 13 have no result in this report.

## Inspect a saved run without installing ReproLab

A [small outcome file](cases/async-lru-799-outcomes.json) records the three transitions from one v0.19.0a24 run and the SHA-256 of its source report. From the root of the source checkout, use the standard-library reader:

```sh
python3 -I src/reprolab/project_projection_format.py docs/cases/async-lru-799-outcomes.json
```

Expect three improvements and `source_verified: false`. The reader recomputes the three classifications from the shared rows. To establish how the upstream code behaves on your machine, run the comparison below. Each new execution has its own report digest.

## Run the comparison

Use a trusted checkout and Python 3.14. The following commands are for a POSIX shell with Git and the [released ReproLab CLI](https://github.com/pgz-h/reprolab-oss/releases/tag/v0.19.0a24) installed. The only local addition to the pinned upstream candidate is a pytest plugin declaration.

```sh
case_dir="$(mktemp -d)"
base=c2ba8001e4edef3b196ac3f547021723f8e64914
candidate=639c403a266d99d223c649cb00fddaf40f371c85
git clone https://github.com/aio-libs/async-lru.git "$case_dir/upstream"
git -C "$case_dir/upstream" checkout --detach "$candidate"
cat > "$case_dir/upstream/pyproject.toml" <<'TOML'
[tool.reprolab.pytest]
plugins = ["pytest_asyncio.plugin", "pytest_timeout"]
TOML

python3.14 -m venv "$case_dir/env"
"$case_dir/env/bin/python" -m pip install -r "$case_dir/upstream/requirements-test.txt" \
  pytest-cov==7.1.0 coverage==7.16.2
reprolab project review "$case_dir/upstream" --before "$base" --working-tree \
  --python "$case_dir/env/bin/python" --path tests/test_basic.py \
  --test tests/test_basic.py::test_alru_cache_method_is_coroutine_function \
  --test tests/test_basic.py::test_alru_cache_weakref \
  --test tests/test_basic.py::test_alru_cache_method_weakref \
  --output "$case_dir/runs" > "$case_dir/result.json"
report=$("$case_dir/env/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["report_dir"])' < "$case_dir/result.json")
reprolab project pack "$report" --output "$case_dir/handoff.zip"
reprolab project summary "$case_dir/handoff.zip" --gate strict --include-test-ids
```

## Check the replay separately

A recipient can read the summary using just the ZIP. To run the tests again, use a Python environment with the dependencies above. In this local walkthrough the same environment serves both roles.

```sh
reprolab project replay "$case_dir/handoff.zip" --confirm-run --python "$case_dir/env/bin/python" \
  --output "$case_dir/replays" --receipt-dir "$case_dir/receipt"
reprolab project check-receipt "$case_dir/handoff.zip" "$case_dir/receipt"
```

Look at both fields in the receipt check. A top-level `status` of `consistent` means the receipt agrees with the attached evidence. The `replay_verification.status` field describes how closely the new observations match the originals.

In a v0.19.0a24 run on macOS arm64, all three tests again went from failing to passing. The strict replay check returned exit code `1`: two tests matched, and the recorded baseline assertion for the coroutine test differed. In that case only the process-local addresses of Python objects in the failure text had changed. The raw messages remain available to inspect. You can locate the differing test offline:

```sh
reprolab project verify-replay "$case_dir/handoff.zip" "$case_dir/receipt/replay-evidence.zip" \
  --include-test-ids --format markdown
```

In v0.19.0a25, add `--explain` to that command to classify recorded pytest failure text differences offline. In JSON output, `entries[].failure_text_difference` identifies the baseline or candidate side. The address-pattern check preserves repeated references and treats ordinary hexadecimal values as data. It describes the stored text only. The strict exit code and original receipt still apply.

A `recorded_failure_changed` result can also indicate a substantive change. Inspect the two messages before attributing a mismatch to formatting. See the [Chinese walkthrough](replay-handoff-case.md) for earlier runs and their recorded conditions.
