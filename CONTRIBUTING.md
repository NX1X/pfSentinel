# Contributing to pfSentinel

Thanks for your interest in contributing! This document covers the basics for getting started.

## Reporting Bugs

[Open an issue](https://github.com/NX1X/pfSentinel/issues) with:

- The version you're running (`pfs --version`)
- Your OS and Python version
- Steps to reproduce the problem
- Expected vs. actual behavior
- Any error output or logs (`pfs --debug ...`)

## Suggesting Features

Open an issue describing the feature, why it's useful, and how you'd expect it to work. If you plan to implement it yourself, mention that so we can coordinate.

## Development Setup

**Requirements:** Python 3.13+, Git

```bash
# Clone the repository
git clone https://github.com/NX1X/pfSentinel.git
cd pfSentinel

# Create a virtual environment
python -m venv .venv

# Activate it
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1

# Install in development mode with dev dependencies
pip install -e ".[dev]"

# Verify
pfs --version
```

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
# Check for lint issues
ruff check src/ tests/

# Auto-fix lint issues
ruff check --fix src/ tests/

# Check formatting
ruff format --check src/ tests/

# Auto-format
ruff format src/ tests/
```

Key rules:

- Python 3.13 target
- 100-character line length
- Imports sorted with `isort` rules (via Ruff)

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=src/pfsentinel --cov-report=term-missing

# Run only unit tests (skip integration)
pytest tests/ -v -m "not integration"
```

## Type Checking

```bash
mypy src/pfsentinel
```

## Project Structure

```
src/pfsentinel/
  cli/           CLI commands (Typer)
  models/        Pydantic data models
  services/      Core business logic
  utils/         Stateless utility functions
tests/
  unit/          Unit tests
  integration/   Integration tests
```

- **Models** are pure data classes with no business logic.
- **Services** contain stateless business logic and are the main layer tested.
- **CLI** commands call services and format output with Rich.

## Pull Requests

1. Fork the repo and create a branch from `main`.
2. Make your changes. Add or update tests as needed.
3. Run `ruff check`, `ruff format --check`, and `pytest` to ensure everything passes.
4. Write a clear PR description explaining what changed and why.
5. Keep PRs focused -- one feature or fix per PR.

## Commit Messages

Use clear, descriptive commit messages. Prefix with the area of change when helpful:

```
fix: handle tar exit code 1 on package backup
feat: add Slack notification support
docs: update installation guide for Linux
test: add unit tests for retention service
```

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
