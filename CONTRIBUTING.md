# Contributing

## Setup

```bash
git clone https://github.com/demirelh/bitcoin-education.git
cd bitcoin-education
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,web]"
```

## Code Quality

Ruff enforces linting and formatting (config in `pyproject.toml`):

```bash
ruff check btcedu/ tests/            # lint
ruff check btcedu/ tests/ --fix      # auto-fix
ruff format btcedu/ tests/           # format
```

Rules: line-length 100, Python 3.12+, select E/W/F/I/UP, ignore UP042.

## Testing

```bash
pytest                               # full suite (~1189 tests)
pytest tests/test_pipeline.py -x -q  # specific file, stop on first failure
pytest -k "test_render" -x           # pattern match
```

All external APIs must be mocked — no real API calls in tests.

## Pre-commit Hooks

```bash
pip install pre-commit
pre-commit install
```

Runs ruff linter + formatter, checks trailing whitespace, validates YAML/JSON.

## CI Pipeline

Pull requests must pass:
1. **Lint** — ruff linter and formatter checks
2. **Test** — pytest on Python 3.12 and 3.13

## Workflow

1. Branch from `main`: `git checkout -b feature/your-feature`
2. Make changes, run `ruff check --fix` and `ruff format`
3. Run `pytest` — all tests must pass
4. Commit and push
5. Open a pull request against `main`
