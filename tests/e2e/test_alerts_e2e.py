"""Alerts end to end: risky changes, the restore check, and no recent backup."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services.notifications import AlertKind, NotificationService
from pfsentinel.services.retention import RetentionService
from tests.unit.test_config_checks import BASE

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--config-only"]


def _output(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def alerts():
    """Every alert sent, as (kind, title, message)."""
    sent: list[tuple[AlertKind, str, str]] = []

    def capture(self, kind, title, message):
        sent.append((kind, title, message))
        return {}

    with patch.object(NotificationService, "alert", capture):
        yield sent


def _index(config: AppConfig):
    return RetentionService(config.backup_policy.resolved_root, config.backup_policy).load_index(
        "home-fw"
    )


def test_a_new_admin_user_raises_one_risky_change_alert(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer, alerts
) -> None:
    ssh_server.set_file("/cf/conf/config.xml", BASE)
    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert alerts == [], "the first backup has nothing to compare with"

    ssh_server.set_file(
        "/cf/conf/config.xml",
        BASE.replace(
            "<user><name>backup-user</name>",
            "<user><name>eve</name><uid>3000</uid><priv>page-all</priv></user>"
            "<user><name>backup-user</name>",
        ),
    )
    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)

    assert [(k, m.splitlines()[1]) for k, _, m in alerts] == [
        (AlertKind.RISKY_CHANGE, "Change: New admin user 'eve'")
    ]
    latest = _index(seeded_config).latest()
    assert latest is not None
    assert latest.risky_changes == ["New admin user 'eve'"]
    assert latest.restore_check is not None and latest.restore_check.startswith("OK: 3 users")


def test_a_config_without_users_fails_the_restore_check(
    cli_runner: CliRunner, seeded_config: AppConfig, alerts
) -> None:
    """The seeded sample config has no users: backed up, but flagged."""
    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)
    assert [k for k, _, _ in alerts] == [AlertKind.RESTORE_CHECK_FAILED]
    assert "no system users" in alerts[0][2]

    check = cli_runner.invoke(app, ["backup", "check", "-d", "home-fw"])
    assert check.exit_code == 1
    assert "no system users" in _output(check)


def test_backup_check_passes_on_a_good_config(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    ssh_server.set_file("/cf/conf/config.xml", BASE)
    assert cli_runner.invoke(app, RUN).exit_code == 0
    check = cli_runner.invoke(app, ["backup", "check"])
    assert check.exit_code == 0, _output(check)
    assert "OK: 2 users" in check.stdout


def test_check_recent_alerts_only_when_backups_are_missing(
    cli_runner: CliRunner, seeded_config: AppConfig, alerts
) -> None:
    none_yet = cli_runner.invoke(app, ["backup", "check-recent", "--non-interactive"])
    assert none_yet.exit_code == 1
    assert [k for k, _, _ in alerts] == [AlertKind.NO_RECENT_BACKUP]
    assert "never" in alerts[0][2]

    assert cli_runner.invoke(app, RUN).exit_code == 0
    alerts.clear()
    fresh = cli_runner.invoke(app, ["backup", "check-recent", "--non-interactive"])
    assert fresh.exit_code == 0, _output(fresh)
    assert alerts == []

    # Age the backup past the limit (default 3 days).
    index = _index(seeded_config)
    for record in index.records:
        record.created_at = datetime.now() - timedelta(days=4)
    RetentionService(
        seeded_config.backup_policy.resolved_root, seeded_config.backup_policy
    ).save_index(index)
    stale = cli_runner.invoke(app, ["backup", "check-recent"])
    assert stale.exit_code == 1
    assert [k for k, _, _ in alerts] == [AlertKind.NO_RECENT_BACKUP]


@pytest.mark.release_check
def test_update_available_alerts_once_per_release(
    cli_runner: CliRunner, seeded_config: AppConfig, alerts, tmp_path, monkeypatch
) -> None:
    from pfsentinel.services import updater

    monkeypatch.setattr(updater, "app_config_dir", lambda: tmp_path / "state")
    newer = {"current": "0.3.0", "latest": "9.9.9", "release_url": "https://x", "tag": "v9.9.9"}
    monkeypatch.setattr(updater.UpdateService, "check", lambda self, force=False: newer)
    assert cli_runner.invoke(app, RUN).exit_code == 0
    alerts.clear()

    for _ in range(2):
        cli_runner.invoke(app, ["backup", "check-recent"])
    assert [k for k, _, _ in alerts] == [AlertKind.UPDATE_AVAILABLE], "once, not every day"


@pytest.mark.release_check
def test_update_check_off_makes_no_github_call(
    cli_runner: CliRunner, seeded_config: AppConfig, alerts, tmp_path, monkeypatch
) -> None:
    """Off means the check task does not contact GitHub at all, not only no alert."""
    from pfsentinel.services import updater

    monkeypatch.setattr(updater, "app_config_dir", lambda: tmp_path / "state")
    calls: list[bool] = []

    def _check(self, force=False):
        calls.append(force)
        return None

    monkeypatch.setattr(updater.UpdateService, "check", _check)
    assert cli_runner.invoke(app, RUN).exit_code == 0

    cli_runner.invoke(app, ["backup", "check-recent"])
    assert calls == [True], "on by default: one check per run"

    off = cli_runner.invoke(app, ["config", "set", "update-check", "off"])
    assert off.exit_code == 0, _output(off)
    calls.clear()
    cli_runner.invoke(app, ["backup", "check-recent"])
    assert calls == []
