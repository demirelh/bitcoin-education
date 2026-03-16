---
name: test
description: Run the btcedu test suite with pytest. Pass optional arguments to filter tests.
argument-hint: "[test pattern or file path]"
allowed-tools: Bash
---

Run the btcedu test suite. If arguments are provided, pass them to pytest.

```bash
.venv/bin/pytest $ARGUMENTS -x -q 2>&1 | tail -30
```

If no arguments provided, run the full suite:

```bash
.venv/bin/pytest -x -q 2>&1 | tail -30
```

Report: number of tests passed, any failures, and total runtime.
