"""Regression guards for assumptions pfSentinel makes about its dependencies.

Both of these were real defects found in review:

1. The CLI caught the *external* ``click.Abort`` while typer >=0.26 vendors
   click and raises its own ``typer.Abort``. Every abort handler was dead
   code, so Ctrl+C during a password prompt produced a traceback.
2. ``SSHConnector`` passed ``disabled_algorithms={"pubkeys": [...]}`` naming
   algorithms that are not in paramiko's preference tuple, so the setting
   filtered nothing while looking like hardening.

Neither failure is visible at runtime under normal use, so they are pinned
here rather than left to a future reviewer to re-derive.
"""

from __future__ import annotations

import click
import pytest
import typer
from typer.testing import CliRunner


class TestTyperAbortContract:
    """typer.Abort is the exception the CLI must catch, not click.Abort."""

    def test_typer_abort_is_not_click_abort(self) -> None:
        """If this ever becomes True, typer stopped vendoring click."""
        assert typer.Abort is not click.Abort, (
            "typer.Abort and click.Abort converged - re-check every abort "
            "handler in cli/commands/, they may need to catch both again"
        )

    def test_prompt_cancellation_raises_typer_abort(self) -> None:
        """An aborted prompt must raise typer.Abort, which is what we catch."""
        app = typer.Typer()
        caught: list[str] = []

        @app.command()
        def ask() -> None:
            try:
                typer.prompt("secret", hide_input=True)
            except click.Abort:  # the old, broken handler
                caught.append("click")
            except typer.Abort:  # the correct handler
                caught.append("typer")

        # Empty stdin -> EOF -> same abort path as Ctrl+C.
        CliRunner().invoke(app, [], input="")

        assert caught == ["typer"], (
            f"expected typer.Abort, got {caught!r}. The CLI's abort handlers "
            "catch typer.Abort; if typer changed, they must be updated."
        )

    def test_cli_modules_do_not_import_click(self) -> None:
        """click is not a declared dependency - importing it would break installs."""
        import pathlib

        cli_dir = pathlib.Path(__file__).resolve().parents[2] / "src" / "pfsentinel" / "cli"
        offenders = [
            str(p.relative_to(cli_dir.parent))
            for p in cli_dir.rglob("*.py")
            if "click" in p.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            f"click referenced in {offenders}; typer vendors click, so an "
            "external click import would fail at runtime once it is uninstalled"
        )


class TestParamikoPubkeyAlgorithms:
    """SHA-1 public-key algorithms must stay out of paramiko's preferences."""

    def test_no_sha1_pubkey_algorithms_offered(self) -> None:
        """paramiko must not offer ssh-rsa / rsa-sha1 (CVE-2026-44405 class).

        This replaces a ``disabled_algorithms`` setting that named these
        algorithms but filtered nothing, because paramiko only filters against
        entries that are actually present in its preference tuple.
        """
        from paramiko.transport import Transport

        offered = set(Transport._preferred_pubkeys)
        forbidden = {"ssh-rsa", "rsa-sha1"}

        assert not (offered & forbidden), (
            f"paramiko offers SHA-1 public-key algorithms {offered & forbidden}. "
            "Re-add disabled_algorithms={'pubkeys': [...]} in "
            "services/connection.py - it now has something to filter."
        )

    def test_only_modern_signature_algorithms_offered(self) -> None:
        """Every offered pubkey algorithm is Ed25519, ECDSA or RSA-SHA2."""
        from paramiko.transport import Transport

        allowed_prefixes = ("ssh-ed25519", "ecdsa-sha2-", "rsa-sha2-")
        unexpected = [a for a in Transport._preferred_pubkeys if not a.startswith(allowed_prefixes)]
        assert not unexpected, f"unrecognised pubkey algorithms offered: {unexpected}"


@pytest.mark.parametrize("module", ["typer", "paramiko", "lxml"])
def test_critical_dependencies_importable(module: str) -> None:
    """Smoke check that the deps these contracts rely on are actually present."""
    __import__(module)
