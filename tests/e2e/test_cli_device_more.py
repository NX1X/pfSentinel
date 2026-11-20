"""More `pfs device` flows against the fake SSH server: add, test, edit, trust-key.

Complements test_device_commands.py and test_host_keys.py. Name resolution
is the only faked edge (see ``unresolvable``); SSH is real.
"""

from __future__ import annotations

import secrets
import socket
from pathlib import Path

import paramiko
import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services import host_keys
from pfsentinel.services.credentials import CredentialService

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

BAD_HOST = "fw.unresolvable.test"


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(autouse=True)
def no_http_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip urllib3's retry back-off, so refused HTTP(S) probes fail fast."""
    from urllib3.util.retry import Retry

    monkeypatch.setattr(Retry, "sleep", lambda self, response=None: None)


@pytest.fixture
def empty_config(e2e_home: Path) -> AppConfig:
    config = AppConfig()
    config.save()
    return config


@pytest.fixture
def unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make BAD_HOST fail DNS without asking a real resolver."""
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host == BAD_HOST:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _ssh_add_args(server: FakeSSHServer, device_id: str = "lab-fw") -> list[str]:
    return [
        "device",
        "add",
        "--id",
        device_id,
        "--label",
        "Lab",
        "--host",
        server.host,
        "--method",
        "ssh",
        "--username",
        server.username,
        "--ssh-port",
        str(server.port),
    ]


# ---------------------------------------------------------------------------
# device add
# ---------------------------------------------------------------------------


def test_add_ssh_device_pins_the_host_key_and_tests_the_connection(
    cli_runner: CliRunner, empty_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    # key path (blank), password twice, trust key, test connection
    result = cli_runner.invoke(
        app,
        _ssh_add_args(ssh_server),
        input=f"\n{ssh_server.password}\n{ssh_server.password}\ny\ny\n",
    )

    assert result.exit_code == 0, _out(result)
    device = AppConfig.load().get_device("lab-fw")
    assert device is not None
    assert device.strict_host_keys is True
    assert host_keys.trusted_key(ssh_server.host, ssh_server.port) is not None
    assert "Best method: ssh" in _out(result)


def test_add_ssh_device_without_trusting_the_key_warns(
    cli_runner: CliRunner, empty_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    result = cli_runner.invoke(app, _ssh_add_args(ssh_server), input="\npw\npw\nn\nn\n")

    assert result.exit_code == 0, _out(result)
    assert "pfs device trust-key lab-fw" in _out(result)
    assert host_keys.trusted_key(ssh_server.host, ssh_server.port) is None


def test_add_prompts_for_every_field_when_no_flags_are_given(
    cli_runner: CliRunner, empty_config: AppConfig
) -> None:
    # host, id (default), label (default), user (default), method, password x2,
    # http port, test now
    answers = "192.0.2.20\n\n\n\nhttp\npw\npw\n8080\nn\n"
    result = cli_runner.invoke(app, ["device", "add"], input=answers)

    assert result.exit_code == 0, _out(result)
    device = AppConfig.load().get_device("192-0-2-20")
    assert device is not None
    assert device.primary_method == ConnectionMethod.HTTP
    assert device.http_port == 8080
    assert device.username == "admin"


def test_add_asks_again_for_an_invalid_device_id(
    cli_runner: CliRunner, empty_config: AppConfig
) -> None:
    args = ["device", "add", "--host", "192.0.2.21", "--label", "L", "-u", "admin"]
    args += ["--method", "https", "--https-port", "8443"]
    result = cli_runner.invoke(app, args, input="Bad ID!\ngood-id\npw\npw\nn\n")

    assert result.exit_code == 0, _out(result)
    assert "Invalid ID" in _out(result)
    assert AppConfig.load().get_device("good-id") is not None


def test_add_with_an_invalid_method_saves_nothing(
    cli_runner: CliRunner, empty_config: AppConfig
) -> None:
    args = ["device", "add", "--id", "x-fw", "--label", "X", "--host", "192.0.2.22"]
    result = cli_runner.invoke(app, [*args, "-u", "admin", "--method", "telnet"])

    assert result.exit_code == 1
    assert "Invalid method" in _out(result)
    assert AppConfig.load().devices == []


def test_add_a_duplicate_id_is_refused(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    args = ["device", "add", "--id", "home-fw", "--label", "Again", "--host", "192.0.2.23"]
    args += ["-u", "admin", "--method", "https", "--https-port", "8443"]
    result = cli_runner.invoke(app, args, input="pw\npw\n")

    assert result.exit_code == 1
    assert AppConfig.load().get_device("home-fw").label == "Home pfSense"


def test_add_unresolvable_host_asks_for_another_one(
    cli_runner: CliRunner, empty_config: AppConfig, unresolvable: None
) -> None:
    args = ["device", "add", "--id", "dns-fw", "--label", "D", "--host", BAD_HOST]
    args += ["-u", "admin", "--method", "https", "--https-port", "8443"]
    result = cli_runner.invoke(app, args, input="n\n192.0.2.24\npw\npw\nn\n")

    assert result.exit_code == 0, _out(result)
    assert "does not resolve" in _out(result)
    assert AppConfig.load().get_device("dns-fw").host == "192.0.2.24"


def test_add_unresolvable_host_can_be_kept_on_purpose(
    cli_runner: CliRunner, empty_config: AppConfig, unresolvable: None
) -> None:
    args = ["device", "add", "--id", "vpn-fw", "--label", "V", "--host", BAD_HOST]
    args += ["-u", "admin", "--method", "https", "--https-port", "8443"]
    result = cli_runner.invoke(app, args, input="y\npw\npw\nn\n")

    assert result.exit_code == 0, _out(result)
    assert AppConfig.load().get_device("vpn-fw").host == BAD_HOST


def _key_args(tmp_host: str = "192.0.2.30") -> list[str]:
    return ["device", "add", "--id", "key-fw", "--label", "K", "--host", tmp_host]


def test_add_with_an_unencrypted_key_needs_no_passphrase(
    cli_runner: CliRunner, empty_config: AppConfig, ssh_server: FakeSSHServer, tmp_path: Path
) -> None:
    key_file = tmp_path / "id_ecdsa"
    paramiko.ECDSAKey.generate().write_private_key_file(str(key_file))
    args = _ssh_add_args(ssh_server, "key-fw")
    # key path, password (skip), trust key (no), test (no)
    result = cli_runner.invoke(app, args, input=f"{key_file}\n\nn\nn\n")

    assert result.exit_code == 0, _out(result)
    output = _out(result)
    assert "SSH key auth only" in output
    assert "no passphrase needed" in output
    device = AppConfig.load().get_device("key-fw")
    assert device.ssh_key_path == key_file
    assert CredentialService().get("key-fw") is None


def test_add_with_an_encrypted_key_stores_its_passphrase(
    cli_runner: CliRunner, empty_config: AppConfig, ssh_server: FakeSSHServer, tmp_path: Path
) -> None:
    key_file = tmp_path / "id_ecdsa_enc"
    passphrase = secrets.token_hex(8)  # a throwaway key passphrase for this test
    paramiko.ECDSAKey.generate().write_private_key_file(str(key_file), password=passphrase)
    args = _ssh_add_args(ssh_server, "key-fw")
    # key path, password (skip), passphrase, trust key (no), test (no)
    result = cli_runner.invoke(app, args, input=f"{key_file}\n\n{passphrase}\nn\nn\n")

    assert result.exit_code == 0, _out(result)
    assert "the key is encrypted" in _out(result)
    assert CredentialService().get_ssh_key_passphrase("key-fw") == passphrase


def test_add_with_a_missing_key_file_warns_but_continues(
    cli_runner: CliRunner, empty_config: AppConfig, ssh_server: FakeSSHServer, tmp_path: Path
) -> None:
    missing = tmp_path / "no_such_key"
    args = _ssh_add_args(ssh_server, "key-fw")
    # key path, password (skip), passphrase (blank: key unreadable), trust (no), test (no)
    result = cli_runner.invoke(app, args, input=f"{missing}\n\n\nn\nn\n")

    assert result.exit_code == 0, _out(result)
    assert "Key file not found" in _out(result)
    assert AppConfig.load().get_device("key-fw") is not None


# ---------------------------------------------------------------------------
# device test
# ---------------------------------------------------------------------------


def _save_device(**fields) -> DeviceConfig:
    config = AppConfig.load()
    device = DeviceConfig(**fields)
    config.add_device(device)
    config.save()
    return device


def test_test_unknown_device_fails(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["device", "test", "-d", "ghost"])

    assert result.exit_code == 1
    assert "not found" in _out(result)


def test_test_without_devices_fails(cli_runner: CliRunner, empty_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["device", "test"])

    assert result.exit_code == 1
    assert "No enabled devices" in _out(result)


def test_test_without_credentials_says_so(cli_runner: CliRunner, empty_config: AppConfig) -> None:
    _save_device(id="nocred-fw", label="N", host="192.0.2.40")
    result = cli_runner.invoke(app, ["device", "test", "-d", "nocred-fw"])

    assert result.exit_code == 0, _out(result)
    assert "No credentials stored" in _out(result)


def test_test_unreachable_device_shows_troubleshooting(
    cli_runner: CliRunner, empty_config: AppConfig
) -> None:
    _save_device(
        id="down-fw",
        label="Down",
        host="127.0.0.1",
        ssh_port=_closed_port(),
        https_port=_closed_port(),
        http_port=_closed_port(),
        timeout=5,
    )
    CredentialService().store("down-fw", "pw")
    result = cli_runner.invoke(app, ["device", "test", "-d", "down-fw"])

    output = _out(result)
    assert result.exit_code == 0, output
    assert "Cannot reach 127.0.0.1" in output
    assert "Troubleshooting" in output


def test_test_https_device_shows_the_gui_hint_when_unreachable(
    cli_runner: CliRunner, empty_config: AppConfig
) -> None:
    _save_device(
        id="gui-fw",
        label="GUI",
        host="127.0.0.1",
        primary_method=ConnectionMethod.HTTPS,
        ssh_port=_closed_port(),
        https_port=_closed_port(),
        http_port=_closed_port(),
        timeout=5,
    )
    CredentialService().store("gui-fw", "pw")
    result = cli_runner.invoke(app, ["device", "test", "-d", "gui-fw"])

    assert "web GUI" in _out(result)


def test_test_warns_when_the_host_key_is_not_pinned(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    result = cli_runner.invoke(app, ["device", "test"])

    output = _out(result)
    assert "Host key NOT pinned" in output
    assert "pfs device trust-key home-fw" in output


def test_test_key_only_device_skips_the_web_methods(
    cli_runner: CliRunner, empty_config: AppConfig, tmp_path: Path
) -> None:
    key_file = tmp_path / "id_ecdsa"
    paramiko.ECDSAKey.generate().write_private_key_file(str(key_file))
    _save_device(
        id="keyonly-fw",
        label="K",
        host="127.0.0.1",
        ssh_port=_closed_port(),
        ssh_key_path=key_file,
        timeout=5,
    )
    result = cli_runner.invoke(app, ["device", "test", "-d", "keyonly-fw"])

    assert "Skipped" in _out(result)


# ---------------------------------------------------------------------------
# device edit
# ---------------------------------------------------------------------------


def test_edit_unknown_device_fails(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["device", "edit", "ghost"])

    assert result.exit_code == 1
    assert "not found" in _out(result)


def test_edit_invalid_method_changes_nothing(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    result = cli_runner.invoke(app, ["device", "edit", "home-fw"], input="New\n\n\ntelnet\n")

    assert result.exit_code == 1
    assert AppConfig.load().get_device("home-fw").label == "Home pfSense"


def test_edit_changes_the_password(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    # label, host, user, method (defaults), change pw: y, pw x2, change key: n, toggle ssl: n
    result = cli_runner.invoke(
        app, ["device", "edit", "home-fw"], input="\n\n\n\ny\nnewpw\nnewpw\nn\nn\n"
    )

    assert result.exit_code == 0, _out(result)
    assert CredentialService().get("home-fw") == "newpw"


def test_edit_sets_a_key_and_its_passphrase(
    cli_runner: CliRunner, seeded_config: AppConfig, tmp_path: Path
) -> None:
    key_file = tmp_path / "id_new"
    # defaults x4, change pw: n, change key: y, key path, set passphrase: y,
    # passphrase x2, toggle ssl: n
    key_pass = secrets.token_urlsafe(12)
    answers = f"\n\n\n\nn\ny\n{key_file}\ny\n{key_pass}\n{key_pass}\nn\n"
    result = cli_runner.invoke(app, ["device", "edit", "home-fw"], input=answers)

    assert result.exit_code == 0, _out(result)
    assert AppConfig.load().get_device("home-fw").ssh_key_path == key_file
    assert CredentialService().get_ssh_key_passphrase("home-fw") == key_pass


def test_edit_can_clear_the_key(
    cli_runner: CliRunner, seeded_config: AppConfig, tmp_path: Path
) -> None:
    config = AppConfig.load()
    config.get_device("home-fw").ssh_key_path = tmp_path / "old_key"
    config.save()
    # defaults x4, change pw: n, change key: y, blank path, toggle ssl: n
    result = cli_runner.invoke(app, ["device", "edit", "home-fw"], input="\n\n\n\nn\ny\n\nn\n")

    assert result.exit_code == 0, _out(result)
    assert AppConfig.load().get_device("home-fw").ssh_key_path is None


def test_edit_toggles_ssl_verification(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    before = AppConfig.load().get_device("home-fw").verify_ssl
    # defaults x4, change pw: n, change key: n, toggle ssl: y
    result = cli_runner.invoke(app, ["device", "edit", "home-fw"], input="\n\n\n\nn\nn\ny\n")

    assert result.exit_code == 0, _out(result)
    assert AppConfig.load().get_device("home-fw").verify_ssl is not before


# ---------------------------------------------------------------------------
# device trust-key
# ---------------------------------------------------------------------------


def test_trust_key_warns_when_it_replaces_another_key(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    host_keys.trust(ssh_server.host, ssh_server.port, paramiko.ECDSAKey.generate())
    result = cli_runner.invoke(app, ["device", "trust-key", "home-fw", "--yes"])

    assert result.exit_code == 0, _out(result)
    assert "REPLACES" in _out(result)
    pinned = host_keys.trusted_key(ssh_server.host, ssh_server.port)
    live = host_keys.fetch_host_key(ssh_server.host, ssh_server.port)
    assert host_keys.fingerprint(pinned) == host_keys.fingerprint(live)


def test_trust_key_for_an_unreachable_device_fails(
    cli_runner: CliRunner, empty_config: AppConfig
) -> None:
    _save_device(id="gone-fw", label="G", host="127.0.0.1", ssh_port=_closed_port(), timeout=5)
    result = cli_runner.invoke(app, ["device", "trust-key", "gone-fw", "--yes"])

    assert result.exit_code == 1
    assert "Cannot reach" in _out(result)
