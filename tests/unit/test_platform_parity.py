"""docs/platforms.md lists every command group as working on Windows and Linux.

A feature for one system only must not slip in without notice: a new
command group fails here until it is in the table, and a row that is not
"Yes" for both systems fails too.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command

from pfsentinel.cli.app import app

DOC = Path(__file__).resolve().parents[2] / "docs" / "platforms.md"
SECTION = "## Features on each system"


def _command_groups() -> set[str]:
    """The group of each leaf command: "backup run" -> "backup", "docs" -> "docs"."""
    groups: set[str] = set()

    def walk(cmd: object, path: list[str]) -> None:
        subs = getattr(cmd, "commands", None)
        if subs:
            for name, sub in subs.items():
                walk(sub, [*path, name])
        else:
            groups.add(" ".join(path[:-1]) or path[0])

    walk(get_command(app), [])
    return groups


def _table_rows() -> dict[str, tuple[str, str]]:
    text = DOC.read_text(encoding="utf-8")
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    rows: dict[str, tuple[str, str]] = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*`pfs ([^`]+)`\s*\|([^|]*)\|([^|]*)\|", line)
        if m:
            rows[m.group(1).strip()] = (m.group(2).strip(), m.group(3).strip())
    return rows


def test_every_command_group_is_in_the_table() -> None:
    missing = _command_groups() - set(_table_rows())
    assert not missing, f"Add these command groups to {DOC.name}: {sorted(missing)}"


def test_no_row_names_a_command_group_that_is_gone() -> None:
    stale = set(_table_rows()) - _command_groups()
    assert not stale, f"Remove these rows from {DOC.name}: {sorted(stale)}"


def test_every_feature_works_on_both_systems() -> None:
    one_system = {
        name: cells
        for name, cells in _table_rows().items()
        if not all(c.startswith("Yes") for c in cells)
    }
    assert not one_system, (
        f"Features limited to one system: {one_system}. Make them work on both, "
        "or decide it on purpose and change this test."
    )
