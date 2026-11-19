### ReproLab comparison

**Needs attention** · Python / pytest

- Baseline · ` 1cb8bdec8edd22ecb8aa896149ba35fe35850608 ` · Completed
- Candidate · ` 707c6e44e9ad63228e0d9834ef7a8dd07edb0d28 ` · Completed
- Scope · 2 selected tests

| Outcome | Count |
| --- | ---: |
| New failure | 0 |
| Blocked | 0 |
| Existing failure | 1 |
| Improved | 0 |
| Passed on both versions | 1 |

**Pytest discovery** · complete · baseline 2 · candidate 2 · baseline only 0

**Pytest worker resources**

- Before · worker-only peak RSS 38469632 bytes · worker CPU 0.075 s
- After · worker-only peak RSS 38502400 bytes · worker CPU 0.081 s

**GitHub PR context** · ` merge `
Event SHA-256 · ` 169b187d0665f58747030022e05b916a857c793aba627eb5caf00adf002a8162 `

**CI gate** · Passed (` no-new-failures `) · Existing failures allowed by policy

- **Existing failure** · ` test_receipts.py::test_receipt `

The comparison covers the selected scope. An improvement records a fail-to-pass change.

Report SHA-256 · ` ae04f51540a1514fb23545366c8147da096a4676b1afec64363ef9b431552c59 `
