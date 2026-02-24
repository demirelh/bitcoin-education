# Contributing to Bitcoin Education Pipeline

Thank you for your interest in contributing to this project! This document provides guidelines for contributing code.

## Development Setup

1. Clone the repository:
```bash
git clone https://github.com/demirelh/bitcoin-education.git
cd bitcoin-education
```

2. Create a virtual environment and install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

3. Set up pre-commit hooks (recommended):
```bash
pip install pre-commit
pre-commit install
```

## Code Quality Standards

This project enforces strict code quality standards using automated linting tools.

### Linting

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting Python code.

**Run linting checks locally:**
```bash
# Check for linting errors
ruff check btcedu/ tests/

# Auto-fix linting errors (where possible)
ruff check btcedu/ tests/ --fix

# Check code formatting
ruff format --check btcedu/ tests/

# Auto-format code
ruff format btcedu/ tests/
```

### Pre-commit Hooks

Pre-commit hooks automatically run linting checks before each commit. Install them with:

```bash
pre-commit install
```

The hooks will:
- Run ruff linter and auto-fix issues where possible
- Format code with ruff
- Check for trailing whitespace
- Validate YAML and JSON files
- Check for large files

### CI Pipeline

All pull requests **must pass** the automated CI pipeline, which includes:

1. **Lint Job** - Runs ruff linter and formatter checks
   - Fails on any linting errors
   - Fails on formatting violations

2. **Test Job** - Runs the test suite on Python 3.12 and 3.13
   - Only runs after linting passes
   - Includes code coverage reporting

The CI pipeline runs automatically on:
- Pull requests to `main` branch
- Pushes to `main` branch

### Linting Configuration

Linting rules are configured in `pyproject.toml`:
- Line length: 100 characters
- Python version target: 3.12+
- Enabled checks: pycodestyle (E/W), pyflakes (F), isort (I), pyupgrade (UP)

## Making Changes

1. Create a new branch for your changes:
```bash
git checkout -b feature/your-feature-name
```

2. Make your changes and ensure code quality:
```bash
# Run linter
ruff check btcedu/ tests/ --fix

# Format code
ruff format btcedu/ tests/

# Run tests
pytest tests/ -v
```

3. Commit your changes:
```bash
git add .
git commit -m "Description of your changes"
```

4. Push to your fork and create a pull request:
```bash
git push origin feature/your-feature-name
```

## Testing

Run the test suite:
```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_translator.py -v

# Run with coverage
pytest tests/ --cov=btcedu --cov-report=term-missing
```

## Common Issues

### Linting Errors

If you encounter linting errors:

1. **Unused imports (F401)**: Remove the unused import
2. **Line too long (E501)**: Break long lines into multiple lines
3. **Import order (I001)**: Let ruff auto-fix with `ruff check --fix`

### Pre-commit Hook Failures

If pre-commit hooks fail:

1. Review the error messages
2. Fix the issues manually or run `ruff check --fix`
3. Stage the fixed files: `git add .`
4. Commit again

## Need Help?

- Check existing issues and pull requests
- Review the [MASTERPLAN.md](MASTERPLAN.md) for architecture details
- Review sprint documentation in [docs/sprints/](docs/sprints/)
- Open a new issue for questions or bug reports

## Code of Conduct

- Be respectful and inclusive
- Provide constructive feedback
- Follow project conventions and standards
- Write clear commit messages
- Document your code where necessary
