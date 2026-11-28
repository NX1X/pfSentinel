"""`pfs backup history` through the CLI and the fake SSH server."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--no-notify"]


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


def _history(cli_runner: CliRunner, *args: str) -> list[dict]:
    result = cli_runner.invoke(app, ["backup", "history", "--json", *args])
    assert result.exit_code == 0, _out(result)
    return json.loads(result.stdout)


def test_an_empty_history_says_so(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["backup", "history"])
    assert result.exit_code == 0
    assert "No runs in the history yet" in _out(result)
    assert _history(cli_runner) == []


def test_a_good_run_and_a_failed_run_are_both_listed(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0

    # The firewall is gone: the run fails and makes no file.
    ssh_server.stop()
    failed = cli_runner.invoke(app, RUN)
    assert failed.exit_code == 1, _out(failed)

    runs = _history(cli_runner)
    assert [r["result"] for r in runs] == ["failed", "ok"]
    ok = runs[1]
    assert (ok["device"], ok["run"], ok["files"]) == ("home-fw", 1, 1)
    assert ok["changes"] == "initial"
    assert ok["size_bytes"] > 0
    assert runs[0]["files"] == 0
    assert runs[0]["error"]

    # `backup list` shows only the file of the good run.
    listed = json.loads(cli_runner.invoke(app, ["backup", "list", "--json"]).stdout)
    assert len(listed) == 1

    assert [r["result"] for r in _history(cli_runner, "--problems")] == ["failed"]
    assert len(_history(cli_runner, "--limit", "1")) == 1
    assert _history(cli_runner, "-d", "other-fw") == []


def test_the_table_names_the_result_and_the_file(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    out = _out(cli_runner.invoke(app, ["backup", "history"]))
    assert "Backup runs" in out
    assert "OK" in out and "#1" in out
    assert "run_history.jsonl" in out


def test_no_problems_says_so(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    out = _out(cli_runner.invoke(app, ["backup", "history", "--problems"]))
    assert "No run with a problem" in out
