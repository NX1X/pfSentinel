"""Dagger CI module for pfSentinel.

Mirrors the Linux jobs in .github/workflows/ci.yml and security.yml so the
same lint / test / audit / bandit / build steps run identically on a laptop
(`dagger call check --source=.`) and in GitHub Actions.

Windows tests are intentionally NOT covered here: Dagger only runs Linux
containers, and pfSentinel's Windows-specific behaviour (scheduler, toast
notifications) is exercised by the native windows-latest runner in ci.yml.
"""

from typing import Annotated

import dagger
from dagger import DefaultPath, Ignore, dag, function, object_type

# python:3.14-slim (Debian 13 "trixie"), pinned by digest.
# Verified with:
#   docker pull python:3.14-slim
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.14-slim
# Matches the Python 3.14 used by the lint / security-audit / bandit /
# build-wheel jobs in ci.yml and security.yml.
PYTHON_IMAGE = (
    "python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2"
)

# Paths that never need to reach the container: VCS metadata, virtualenvs,
# build output, and internal-only docs.
SOURCE_IGNORE = [
    ".git",
    ".venv",
    ".venv*",
    "build",
    "dist",
    "docs-internal",
    ".dagger",
    "**/__pycache__",
    "**/*.egg-info",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
]

SourceDir = Annotated[dagger.Directory, DefaultPath("."), Ignore(SOURCE_IGNORE)]


@object_type
class PfsentinelCi:
    """CI functions for pfSentinel: lint, test, audit, bandit, build, check."""

    def _base(self, source: dagger.Directory) -> dagger.Container:
        """Container with the dev lockfile installed exactly like CI does.

        Mirrors:
            python -m pip install --only-binary :all: --require-hashes \
                --no-deps -r requirements-dev.lock
            python -m pip install --no-deps --no-build-isolation -e .
        """
        return (
            dag.container()
            .from_(PYTHON_IMAGE)
            .with_workdir("/src")
            .with_mounted_directory("/src", source)
            .with_exec(
                [
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "--only-binary",
                    ":all:",
                    "--require-hashes",
                    "--no-deps",
                    "-r",
                    "requirements-dev.lock",
                ]
            )
            .with_exec(
                [
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-build-isolation",
                    "-e",
                    ".",
                ]
            )
        )

    @function
    async def lint(self, source: SourceDir) -> str:
        """Run ruff check and ruff format --check on src/ and tests/."""
        ctr = self._base(source)
        ctr = ctr.with_exec(["ruff", "check", "src/", "tests/"])
        ctr = ctr.with_exec(["ruff", "format", "--check", "src/", "tests/"])
        return await ctr.stdout()

    @function
    async def test(self, source: SourceDir) -> str:
        """Run pytest with coverage (fail_under enforced via pyproject.toml)."""
        ctr = self._base(source).with_exec(
            [
                "pytest",
                "tests/",
                "--tb=short",
                "-ra",
                "--cov=src/pfsentinel",
                "--cov-report=term-missing",
                "--cov-report=xml:coverage.xml",
                "-q",
            ]
        )
        return await ctr.stdout()

    @function
    async def audit(self, source: SourceDir) -> str:
        """Run pip-audit against the pinned runtime lockfile.

        Mirrors the security-audit job: strip --hash lines from
        requirements.lock (pip-audit doesn't take a hashed requirements
        file) and scan the result, then dry-run verify the hashed lock.
        """
        ctr = self._base(source).with_exec(
            [
                "sh",
                "-c",
                "sed '/^ *--hash=/d' requirements.lock | sed 's/ *\\\\$//' "
                "> audit-requirements.txt",
            ]
        )
        ctr = ctr.with_exec(
            ["pip-audit", "--strict", "--desc", "on", "-r", "audit-requirements.txt"]
        )
        ctr = ctr.with_exec(
            [
                "python",
                "-m",
                "pip",
                "install",
                "--only-binary",
                ":all:",
                "--require-hashes",
                "--dry-run",
                "-r",
                "requirements.lock",
            ]
        )
        return await ctr.stdout()

    @function
    async def bandit(self, source: SourceDir) -> str:
        """Run Bandit SAST against src/ (matches security.yml).

        Bandit is already pinned in requirements-dev.lock, so this reuses
        the same hash-verified install as the other functions instead of a
        separate ad hoc `pip install`.
        """
        ctr = self._base(source).with_exec(
            [
                "bandit",
                "-c",
                "pyproject.toml",
                "-r",
                "src/",
                "--severity-level",
                "medium",
                "--confidence-level",
                "medium",
            ]
        )
        return await ctr.stdout()

    @function
    async def build(self, source: SourceDir) -> dagger.Directory:
        """Build the wheel/sdist and verify them with twine check."""
        ctr = self._base(source)
        ctr = ctr.with_exec(["python", "-m", "build"])
        # dist/* needs shell glob expansion; with_exec does not invoke a shell.
        ctr = ctr.with_exec(["sh", "-c", "twine check dist/*"])
        return ctr.directory("/src/dist")

    @function
    async def check(self, source: SourceDir) -> str:
        """Run lint, test, audit, bandit and build; fail on the first error."""
        results = []
        results.append(await self.lint(source))
        results.append(await self.test(source))
        results.append(await self.audit(source))
        results.append(await self.bandit(source))
        await self.build(source)
        results.append("build: ok (dist/ produced, twine check passed)")
        return "\n\n".join(results)
