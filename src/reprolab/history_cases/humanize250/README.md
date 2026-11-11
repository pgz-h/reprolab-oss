# humanize PR #250 — historical single-module reproduction

This example reproduces a regression already fixed upstream in 2025. It is not a new discovery, a full-package integration, or evidence of adoption by humanize maintainers.

- Upstream fix: https://github.com/python-humanize/humanize/pull/250
- Release: https://github.com/python-humanize/humanize/releases/tag/4.12.3
- Before: 4.12.2, commit `15230fbfdcae5ca46fa831f656378b2275b88f53`
- After: 4.12.3, commit `2968d44280a68f775db73ddeb2f2fc6c05edac84`

The `before/filesize.py` and `after/filesize.py` files are unmodified upstream `src/humanize/filesize.py` files. This standalone module has no external runtime dependencies. We reviewed the module and import it directly as `filesize` so the historical package build/version machinery does not need to run. Each snapshot retains the upstream MIT `LICENCE`. URLs and hashes are recorded in `PROVENANCE.json`.

The regression test was written for ReproLab based on the publicly described behavior: `naturalsize("1")` should return `"1 Byte"`; the older version returns `"1.0 Byte"`. The official fix converts `bytes_` to an integer before rendering the singular-byte branch. The test records input, observed output and expected output for the report.

Run from an installed ReproLab environment: `python -m reprolab history`.
