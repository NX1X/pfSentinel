"""Read a secret from piped stdin (scripts, containers, CI)."""

from __future__ import annotations

import sys

import typer


def read_secret_line() -> str:
    """The first line of stdin, without its line ending. Exits 1 when empty.

    Hidden prompts read the Windows console directly and ignore piped input,
    so commands offer this for automation: `... --passphrase-stdin < file`.
    """
    line = sys.stdin.readline().rstrip("\r\n")
    if not line:
        from pfsentinel.cli.formatters import print_error

        print_error("No passphrase on stdin")
        raise typer.Exit(1)
    return line
