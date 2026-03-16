---
name: validate-phase
description: Validate the current project state by running the full test suite and checking test count against baseline (867 tests)
allowed-tools: Bash
---

Run a full validation pass on the btcedu project.

```bash
echo "=== Lint ==="
.venv/bin/ruff check btcedu/ tests/ 2>&1 | tail -5

echo ""
echo "=== Tests ==="
.venv/bin/pytest -q 2>&1 | tail -10
```

Check:
1. Ruff lint: any errors?
2. Test count: should be >= 867 (current baseline). Report if lower.
3. Any test failures: report details.

Baseline: 867 tests as of mini-hardening phase (2026-03-16).
