"""CLI commands for self-update management."""

from __future__ import annotations

import typer

from pfsentinel.cli.formatters import console, print_error, print_info, print_success
from pfsentinel.services.updater import UpdateError, UpdateService

app = typer.Typer(help="Check for updates and manage versions", no_args_is_help=True)


_PRE_HELP = "Include pre-releases (release candidates such as 1.0.0rc1)"


@app.command("check")
def update_check(
    pre: bool = typer.Option(False, "--pre", help=_PRE_HELP),
) -> None:
    """Check if a newer version of pfSentinel is available."""
    svc = UpdateService()

    print_info("Checking for updates...")
    try:
        result = svc.check(force=True, prerelease=pre)
    except Exception as e:
        print_error(f"Update check failed: {e}")
        raise typer.Exit(1)

    if result is None:
        from pfsentinel import __version__

        print_success(f"You are up to date (v{__version__})")
        return

    console.print()
    console.print(f"  [bold]Current version:[/]  v{result['current']}")
    console.print(f"  [bold]Latest version:[/]   [green]v{result['latest']}[/]")
    console.print(f"  [bold]Install method:[/]   {result['install_method']}")
    console.print(f"  [bold]Release notes:[/]    {result['release_url']}")
    console.print()
    print_info("Run [bold]pfs update install[/] to update", markup=True)


@app.command("install")
def update_install(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    pre: bool = typer.Option(False, "--pre", help=_PRE_HELP),
) -> None:
    """Download and install the latest version of pfSentinel.

    A binary is installed only after its SHA-256 matches the release checksums
    and its signed build attestation verifies.
    """
    svc = UpdateService()

    print_info("Checking for updates...")
    try:
        result = svc.check(force=True, prerelease=pre)
    except Exception as e:
        print_error(f"Update check failed: {e}")
        raise typer.Exit(1)

    if result is None:
        from pfsentinel import __version__

        print_success(f"Already up to date (v{__version__})")
        return

    console.print()
    console.print(f"  [bold]Update:[/] v{result['current']} -> [green]v{result['latest']}[/]")
    console.print(f"  [bold]Method:[/] {result['install_method']}")
    console.print()

    if not yes:
        confirm = typer.confirm("Proceed with update?", default=True)
        if not confirm:
            print_info("Update cancelled.")
            return

    try:
        msg = svc.install(prerelease=pre)
        print_success(msg)
        print_info("Restart pfSentinel to use the new version.")
    except UpdateError as e:
        print_error(str(e))
        if svc.last_method == "binary" and not svc.restore_failed:
            # A failed binary update never leaves a new binary in place: it
            # stops before the swap, or puts the old one back. Revert here
            # would go back one more version.
            print_info("The installed version was not changed.")
        else:
            print_info("Run [bold]pfs update revert[/] to roll back.", markup=True)
        raise typer.Exit(1)


@app.command("revert")
def update_revert(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Revert to the previous version after a failed update."""
    svc = UpdateService()

    if not yes:
        confirm = typer.confirm("Revert to the previous version?", default=False)
        if not confirm:
            print_info("Revert cancelled.")
            return

    try:
        msg = svc.revert()
        print_success(msg)
        print_info("Restart pfSentinel to use the reverted version.")
    except UpdateError as e:
        print_error(str(e))
        raise typer.Exit(1)
