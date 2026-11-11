# Contributing

Start with the task you want to complete, a reproducible example, and expected versus actual behavior. Use this repository's Issues for ordinary bugs and feature requests. Security and privacy reports follow [SECURITY.md](SECURITY.md).

See the [documentation and repository map](docs/README.md) for the public layout and task-based guides. Build outputs, working projects, credentials and internal logs stay outside the source tree.

## Local setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[desktop]"
.venv/bin/reprolab-desktop
```

For CLI-only development, omit the desktop extra. Supported Python starts at 3.11; desktop development is currently concentrated on macOS arm64.

See the [architecture and contribution map](docs/architecture.md) for entry points, execution contracts and focused checks.

## Changes and validation

Keep changes focused on a user-facing task. Reuse existing runner, persistence and evidence rules. Add targeted regression coverage for important rules and recurring failures; use proportional checks for low-impact edits. State what was actually run, its result, and what remains unverified. A build alone does not prove runtime behavior.

The existing suite can be run with `.venv/bin/python -m pytest -q` when runtime validation is in scope. The GitHub workflow runs a selected set of CLI/core contract tests on Python 3.11/pytest 8 and Python 3.14/pytest 9, builds distributions, and checks a freshly installed wheel outside the checkout. It does not run desktop UI tests; local Qt checks use `QT_QPA_PLATFORM=offscreen`. Configuration is not proof that a remote run passed.

Never convert a skip, unavailable dependency, setup/teardown error, timeout or missing execution record into a passing verdict. Preserve original evidence and make changes in execution inputs explicit. An exception-message match does not establish root cause.

Use synthetic or appropriately licensed, non-sensitive examples and label teaching fixtures. Do not claim upstream adoption without verifiable evidence. Review logs, screenshots, snapshots and model material before sharing; never include credentials or private code without authorization.

Before the first public release, maintainers should enable Issues and private vulnerability reporting, review the prepared source snapshot, and follow the [release guide](docs/github-release.md).
