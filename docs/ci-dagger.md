# Dagger CI pipeline

pfSentinel's lint, test, security-audit and package-build steps are defined
once, in Python, as a [Dagger](https://dagger.io) module in `.dagger/`. The
same module runs on a laptop and in GitHub Actions, so "it passed CI" and
"it passes locally" mean the same thing.

This does not replace the existing jobs in `.github/workflows/ci.yml` or
`.github/workflows/security.yml`. Those jobs' names are required status
checks in the repository's branch protection ruleset, so they stay as-is.
`.github/workflows/dagger.yml` is an additional, container-based
verification of the same steps.

## Scope

The Dagger module covers the Linux-only parts of CI:

| Function  | Mirrors                                             |
|-----------|------------------------------------------------------|
| `lint`    | `ci.yml` Lint job: `ruff check` / `ruff format --check` |
| `test`    | `ci.yml` Test job (Ubuntu leg): `pytest` with coverage |
| `audit`   | `ci.yml` Security Audit job: `pip-audit` + hash dry-run |
| `bandit`  | `security.yml` Bandit job                            |
| `build`   | `ci.yml` Build Python Package job: `python -m build` + `twine check` |
| `check`   | Runs all of the above, in order, failing on the first error |

Windows tests are **not** part of this module. Dagger only runs Linux
containers; pfSentinel's Windows-specific behavior (scheduler via Task
Scheduler, toast notifications via PowerShell) is exercised by the native
`windows-latest` runner in `ci.yml` and is out of scope here by design.

## Running locally

Install the Dagger CLI (see [Dagger's install docs](https://docs.dagger.io/install));
this repo pins the engine to `v0.21.9` in `dagger.json`, and `dagger` will
fetch a matching engine automatically. From the repository root:

```bash
# Run everything (lint, test, audit, bandit, build)
dagger call check --source=.

# Or run a single step
dagger call lint --source=.
dagger call test --source=.
dagger call audit --source=.
dagger call bandit --source=.
dagger call build --source=.
```

Each function mounts the working tree (excluding `.git`, `.venv*`, `build`,
`dist`, `docs-internal` and caches) into a pinned `python:3.14-slim`
container and installs dependencies exactly like CI does:

```bash
python -m pip install --only-binary :all: --require-hashes --no-deps -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
```

No local Python environment or virtualenv setup is required; only Docker
(or another OCI-compatible container runtime) and the Dagger CLI.

## Running in GitHub Actions

`.github/workflows/dagger.yml` runs `dagger call check --source=.` via
[`dagger/dagger-for-github`](https://github.com/dagger/dagger-for-github),
pinned by commit SHA, on `push` to `main` and on every `pull_request`.

## Module layout

```
dagger.json                   # engine version pin, sdk, source dir (points at .dagger)
.dagger/
  src/pfsentinel_ci/main.py   # the module: lint, test, audit, bandit, build, check
  pyproject.toml              # module's own (separate) Python project, depends on dagger-io
  sdk/                        # vendored Dagger Python SDK, generated, gitignored
```

The root-level `dagger.json` records the pinned engine version
(`engineVersion: v0.21.9`) and points at `.dagger` as the module source.

## Future work

A later step could replace the Linux-only jobs in `ci.yml` (lint,
security-audit, the Ubuntu leg of the test matrix, and build-wheel) with
calls into this same Dagger module, so there is exactly one definition of
each step instead of two. That is intentionally not done in this change:
those job names are required status checks in a branch protection ruleset,
and swapping their implementation is a separate, deliberate change to make
alongside a ruleset update.
