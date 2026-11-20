"""Second copy to S3-compatible storage, Azure Blob Storage and WebDAV (EXPERIMENTAL).

The fake servers in fake_object_stores.py run on 127.0.0.1 over TLS and
check every request signature or login, so these tests cover the real
wire format end to end.
"""

from __future__ import annotations

import base64
import re
import socket
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, quote

import pytest
import responses
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig, SecondCopyPolicy
from pfsentinel.services import cloud_targets
from pfsentinel.services.backup_crypto import BackupCrypto
from pfsentinel.services.cloud_targets import (
    AZURE_KEY_NAME,
    AZURE_SAS_NAME,
    S3_SECRET_NAME,
    WEBDAV_PASSWORD_NAME,
    AzureBlobTarget,
    S3Target,
    WebDavTarget,
)
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.notifications import AlertKind, NotificationService
from pfsentinel.services.second_copy import SecondCopyError, SecondCopyService, open_target
from pfsentinel.utils.request_signing import sigv4_authorization
from tests.unit.test_config_checks import BASE

from .fake_object_stores import FakeAzure, FakeS3, FakeWebDav
from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--include", "dhcp", "--no-notify"]

S3_KEY_ID = "AKIDPFSTEST"
S3_SECRET = "s3-Secret-Zq9vX7wP2kL5mN8"
AZURE_KEY = base64.b64encode(b"azure-account-key-for-tests-0123").decode()
AZURE_SIG = "Sx9+Fake/SAS=sig"
AZURE_SAS = "?sv=2025-01-05&sr=c&sp=racwdl&se=2030-01-01T00%3A00%3A00Z&sig=" + quote(
    AZURE_SIG, safe=""
)
DAV_PASSWORD = "dav-App-Pass-4Rt7"
DAV_BASE = "/remote.php/dav/files/alice/pfs backups"


def _output(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def with_leases(ssh_server: FakeSSHServer) -> FakeSSHServer:
    ssh_server.set_file("/cf/conf/config.xml", BASE)
    ssh_server.set_file("/var/dhcpd/var/db/dhcpd.leases", b"lease 192.0.2.50 { }\n")
    return ssh_server


@pytest.fixture
def alerts():
    sent: list[tuple[AlertKind, str]] = []

    def capture(self, kind, title, message):
        sent.append((kind, message))
        return {}

    with patch.object(NotificationService, "alert", capture):
        yield sent


@pytest.fixture
def s3() -> Iterator[FakeS3]:
    server = FakeS3(
        access_key_id=S3_KEY_ID, secret=S3_SECRET, bucket="pfs-backups", region="auto"
    ).start()
    yield server
    server.stop()


@pytest.fixture
def azure_sas() -> Iterator[FakeAzure]:
    server = FakeAzure(account="pfsstore", container="backups", sas_sig=AZURE_SIG).start()
    yield server
    server.stop()


@pytest.fixture
def azure_key() -> Iterator[FakeAzure]:
    server = FakeAzure(account="pfsstore", container="backups", account_key=AZURE_KEY).start()
    yield server
    server.stop()


@pytest.fixture
def dav() -> Iterator[FakeWebDav]:
    server = FakeWebDav(username="alice", password=DAV_PASSWORD, base=DAV_BASE).start()
    yield server
    server.stop()


def _enable(kind: str, encrypt: bool = True, **settings) -> AppConfig:
    config = AppConfig.load()
    if encrypt:
        BackupCrypto(config.backup_policy.encryption, CredentialService()).setup_keystore()
    policy = config.backup_policy.second_copy
    for key, value in {"enabled": True, "kind": kind, **settings}.items():
        setattr(policy, key, value)
    config.save()
    return config


def _s3_settings(server: FakeS3) -> dict:
    return {
        "endpoint": server.url,
        "region": "auto",
        "bucket": "pfs-backups",
        "path_style": True,
        "access_key_id": S3_KEY_ID,
        "prefix": "fw-copies",
        "ca_cert_path": str(server.cert_path),
    }


def _azure_settings(server: FakeAzure, auth: str) -> dict:
    return {
        "account": "pfsstore",
        "container": "backups",
        "azure_auth": auth,
        "azure_endpoint": server.url,
        "prefix": "pfsentinel",
        "ca_cert_path": str(server.cert_path),
    }


def _dav_settings(server: FakeWebDav) -> dict:
    return {
        "url": server.url + quote(DAV_BASE),
        "username": "alice",
        "ca_cert_path": str(server.cert_path),
    }


def _local_files(config: AppConfig) -> dict[str, bytes]:
    root = config.backup_policy.resolved_root
    return {"/".join(p.relative_to(root).parts): p.read_bytes() for p in root.rglob("*.enc")}


def _under(store: dict[str, bytes], prefix: str) -> dict[str, bytes]:
    return {k[len(prefix) :]: v for k, v in store.items() if k.startswith(prefix)}


def _today_folder() -> str:
    return f"home-fw/{datetime.now():%Y/%m/%d}/"


# --- S3 ---------------------------------------------------------------------


def test_s3_copies_every_encrypted_file_once(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, s3: FakeS3, alerts
) -> None:
    CredentialService().store_secret(S3_SECRET_NAME, S3_SECRET)
    config = _enable("s3", **_s3_settings(s3))

    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)
    assert alerts == []
    local = _local_files(config)
    assert len(local) == 2
    assert _under(s3.objects, "fw-copies/") == local
    assert all("%23" in r.path for r in s3.seen if r.method == "PUT"), "# is encoded"

    puts = s3.count("PUT")
    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert len(_under(s3.objects, "fw-copies/")) == 4
    assert s3.count("PUT") == puts + 2, "files already on the target are not sent again"
    assert S3_SECRET not in s3.wire_text()


def test_s3_retention_pages_through_the_listing(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, s3: FakeS3
) -> None:
    CredentialService().store_secret(S3_SECRET_NAME, S3_SECRET)
    old = [
        "fw-copies/home-fw/2020/01/02/020000_#001/home-fw_config.xml.gz.enc",
        "fw-copies/home-fw/2020/01/02/020000_#001/home-fw_dhcp.gz.enc",
        "fw-copies/home-fw/2020/01/03/020000_#002/home-fw_config.xml.gz.enc",
    ]
    keep = ["fw-copies/home-fw/notes.txt", "fw-copies/other-fw/2020/01/02/x.enc", "other/y"]
    for key in old + keep:
        s3.objects[key] = b"x"
    _enable("s3", keep_days=90, **_s3_settings(s3))

    assert cli_runner.invoke(app, RUN).exit_code == 0
    for key in old:
        assert key not in s3.objects
    for key in keep:
        assert key in s3.objects
    assert any(k.startswith("fw-copies/" + _today_folder()) for k in s3.objects)
    assert any("continuation-token=" in r.path for r in s3.seen), "page size 2 forces pages"


def test_s3_never_uploads_plain_files(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, s3: FakeS3, tmp_path: Path
) -> None:
    CredentialService().store_secret(S3_SECRET_NAME, S3_SECRET)
    assert cli_runner.invoke(app, RUN).exit_code == 0  # plain backups
    config = _enable("s3", encrypt=False, **_s3_settings(s3))

    result = SecondCopyService(config, CredentialService()).sync("home-fw")
    assert len(result.skipped_plain) == 2
    assert s3.count("PUT") == 0

    plain = tmp_path / "config.xml.gz"
    plain.write_bytes(b"plain")
    target = open_target(config.backup_policy.second_copy, CredentialService())
    try:
        with pytest.raises(SecondCopyError, match="plain"):
            target.put(plain, "home-fw/2026/01/01/x/config.xml.gz")
    finally:
        target.close()
    assert s3.count("PUT") == 0


def test_s3_wrong_secret_fails_without_showing_it(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, s3: FakeS3, alerts
) -> None:
    wrong = "Wrong-Secret-Value-9911"
    CredentialService().store_secret(S3_SECRET_NAME, wrong)
    _enable("s3", **_s3_settings(s3))

    assert cli_runner.invoke(app, RUN).exit_code == 0, "a failed copy never fails the backup"
    assert [k for k, _ in alerts] == [AlertKind.SECOND_COPY_FAILED]
    message = alerts[0][1]
    assert "403" in message and "SignatureDoesNotMatch" in message
    assert wrong not in message
    assert wrong not in s3.wire_text()

    result = cli_runner.invoke(app, ["copy", "now"])
    assert result.exit_code == 1
    assert wrong not in _output(result)


def test_s3_retries_once_on_a_server_error(
    seeded_config: AppConfig, s3: FakeS3, tmp_path: Path
) -> None:
    CredentialService().store_secret(S3_SECRET_NAME, S3_SECRET)
    policy = SecondCopyPolicy(enabled=True, kind="s3", **_s3_settings(s3))

    s3.fail_next = [503]
    target = S3Target(policy, CredentialService())
    local = tmp_path / "a.xml.gz.enc"
    local.write_bytes(b"ciphertext")
    s3.fail_next = [500]
    target.put(local, "home-fw/2026/01/01/x/a.xml.gz.enc")
    assert target.size("home-fw/2026/01/01/x/a.xml.gz.enc") == len(b"ciphertext")
    target.close()

    s3.fail_next = [503, 503]
    with pytest.raises(SecondCopyError, match="HTTP 503"):
        S3Target(policy, CredentialService())


def test_s3_refuses_http_and_files_over_the_limit(
    seeded_config: AppConfig, s3: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = CredentialService()
    creds.store_secret(S3_SECRET_NAME, S3_SECRET)
    settings = _s3_settings(s3)
    plain_http = SecondCopyPolicy(
        kind="s3", **{**settings, "endpoint": settings["endpoint"].replace("https", "http")}
    )
    with pytest.raises(SecondCopyError, match="https://"):
        S3Target(plain_http, creds)

    monkeypatch.setattr(cloud_targets, "S3_MAX_UPLOAD", 4)
    big = tmp_path / "big.xml.gz.enc"
    big.write_bytes(b"12345")
    target = S3Target(SecondCopyPolicy(kind="s3", **settings), creds)
    try:
        with pytest.raises(SecondCopyError, match="at most 5 GiB"):
            target.put(big, "home-fw/2026/01/01/x/big.xml.gz.enc")
    finally:
        target.close()
    assert s3.count("PUT") == 0


def test_s3_virtual_host_addressing_signs_the_bucket_host(
    seeded_config: AppConfig, tmp_path: Path
) -> None:
    CredentialService().store_secret(S3_SECRET_NAME, S3_SECRET)
    policy = SecondCopyPolicy(
        kind="s3",
        endpoint="https://s3.eu-central-1.amazonaws.com",
        region="eu-central-1",
        bucket="pfs-backups",
        access_key_id=S3_KEY_ID,
    )
    seen = []

    def reply(request):
        seen.append(request)
        path, _, raw_query = request.path_url.partition("?")

        signed = request.headers["authorization"].split("SignedHeaders=")[1].split(",")[0]
        headers = {h: request.headers[h] for h in signed.split(";")}
        expected = sigv4_authorization(
            request.method,
            path,
            parse_qsl(raw_query, keep_blank_values=True),
            headers,
            request.headers["x-amz-content-sha256"],
            access_key_id=S3_KEY_ID,
            secret_access_key=S3_SECRET,
            region="eu-central-1",
            service="s3",
            amz_date=request.headers["x-amz-date"],
        )
        assert request.headers["authorization"] == expected
        if request.method == "GET":
            return (
                200,
                {},
                "<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>",
            )
        return (200, {}, "")

    host = re.compile(r"https://pfs-backups\.s3\.eu-central-1\.amazonaws\.com/.*")
    local = tmp_path / "a.enc"
    local.write_bytes(b"x")
    with responses.RequestsMock() as mock:
        mock.add_callback(responses.GET, host, callback=reply)
        mock.add_callback(responses.PUT, host, callback=reply)
        target = S3Target(policy, CredentialService())
        target.put(local, "home-fw/2026/01/01/020000_#001/a.enc")
    assert seen[0].headers["host"] == "pfs-backups.s3.eu-central-1.amazonaws.com"
    assert seen[1].path_url == "/home-fw/2026/01/01/020000_%23001/a.enc"


# --- Azure ------------------------------------------------------------------


def test_azure_sas_copies_and_removes_old_days(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, azure_sas: FakeAzure, alerts
) -> None:
    CredentialService().store_secret(AZURE_SAS_NAME, AZURE_SAS)
    azure_sas.blobs["pfsentinel/home-fw/2020/01/02/020000_#001/old.xml.gz.enc"] = b"x"
    azure_sas.blobs["pfsentinel/home-fw/2020/01/02/020000_#001/old-dhcp.gz.enc"] = b"x"
    azure_sas.blobs["pfsentinel/home-fw/readme.txt"] = b"keep"
    config = _enable("azure", **_azure_settings(azure_sas, "sas"))

    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)
    assert alerts == []
    local = _local_files(config)
    copied = _under(azure_sas.blobs, "pfsentinel/")
    assert copied == {**local, "home-fw/readme.txt": b"keep"}
    assert all("authorization" not in r.headers for r in azure_sas.seen)


def test_azure_shared_key_copies_every_encrypted_file(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, azure_key: FakeAzure, alerts
) -> None:
    CredentialService().store_secret(AZURE_KEY_NAME, AZURE_KEY)
    config = _enable("azure", **_azure_settings(azure_key, "key"))

    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert alerts == []
    assert _under(azure_key.blobs, "pfsentinel/") == _local_files(config)
    assert AZURE_KEY not in azure_key.wire_text()
    puts = [r for r in azure_key.seen if r.method == "PUT"]
    assert puts and all(r.headers["authorization"].startswith("SharedKey pfsstore:") for r in puts)


def test_azure_errors_never_show_the_sas_token(
    seeded_config: AppConfig, azure_sas: FakeAzure
) -> None:
    creds = CredentialService()
    creds.store_secret(AZURE_SAS_NAME, AZURE_SAS)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    offline = SecondCopyPolicy(
        kind="azure",
        **{
            **_azure_settings(azure_sas, "sas"),
            "azure_endpoint": f"https://127.0.0.1:{closed_port}",
        },
    )
    with pytest.raises(SecondCopyError, match="cannot reach") as info:
        AzureBlobTarget(offline, creds)
    for secret in (AZURE_SIG, quote(AZURE_SIG, safe=""), AZURE_SAS):
        assert secret not in str(info.value)

    creds.store_secret(AZURE_SAS_NAME, "sv=2025-01-05&sp=racwdl&sig=Other%2BSig%3D")
    with pytest.raises(SecondCopyError, match="403 AuthenticationFailed") as info:
        AzureBlobTarget(SecondCopyPolicy(kind="azure", **_azure_settings(azure_sas, "sas")), creds)
    assert "Other" not in str(info.value)


def test_azure_refuses_files_over_the_limit(
    seeded_config: AppConfig, azure_key: FakeAzure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = CredentialService()
    creds.store_secret(AZURE_KEY_NAME, AZURE_KEY)
    monkeypatch.setattr(cloud_targets, "AZURE_MAX_UPLOAD", 4)
    big = tmp_path / "big.enc"
    big.write_bytes(b"12345")
    target = AzureBlobTarget(
        SecondCopyPolicy(kind="azure", **_azure_settings(azure_key, "key")), creds
    )
    try:
        with pytest.raises(SecondCopyError, match="at most 5000 MiB"):
            target.put(big, "home-fw/2026/01/01/x/big.enc")
    finally:
        target.close()
    assert azure_key.count("PUT") == 0


# --- WebDAV -----------------------------------------------------------------


def test_webdav_uploads_to_a_temporary_name_then_moves(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, dav: FakeWebDav, alerts
) -> None:
    CredentialService().store_secret(WEBDAV_PASSWORD_NAME, DAV_PASSWORD)
    config = _enable("webdav", **_dav_settings(dav))

    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)
    assert alerts == []
    assert _under(dav.files, DAV_BASE + "/") == _local_files(config)
    puts = [r for r in dav.seen if r.method == "PUT"]
    assert len(puts) == 2 and all(".uploading" in r.path for r in puts)
    assert dav.count("MOVE") == 2
    assert dav.count("MKCOL") >= 5, "device, year, month, day and run folders"
    assert not [p for p in dav.files if ".uploading" in p]

    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert dav.count("PUT") == 4, "only the new run is sent"


def test_webdav_retention_removes_old_day_folders(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, dav: FakeWebDav
) -> None:
    CredentialService().store_secret(WEBDAV_PASSWORD_NAME, DAV_PASSWORD)
    old_day = f"{DAV_BASE}/home-fw/2020/01/02"
    for folder in ("home-fw", "home-fw/2020", "home-fw/2020/01", "home-fw/2020/01/02"):
        dav.folders.add(f"{DAV_BASE}/{folder}")
    dav.folders.add(f"{old_day}/020000_#001")
    dav.files[f"{old_day}/020000_#001/old.xml.gz.enc"] = b"x"
    dav.files[f"{DAV_BASE}/home-fw/notes.txt"] = b"keep"
    _enable("webdav", keep_days=90, **_dav_settings(dav))

    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert old_day not in dav.folders
    assert f"{old_day}/020000_#001/old.xml.gz.enc" not in dav.files
    assert dav.files[f"{DAV_BASE}/home-fw/notes.txt"] == b"keep"
    assert f"{DAV_BASE}/home-fw/2020" in dav.folders, "only the day folder goes"


def test_webdav_plain_http_only_with_the_lan_opt_in(seeded_config: AppConfig) -> None:
    creds = CredentialService()
    creds.store_secret(WEBDAV_PASSWORD_NAME, DAV_PASSWORD)
    server = FakeWebDav(username="alice", password=DAV_PASSWORD, base=DAV_BASE).start(tls=False)
    try:
        policy = SecondCopyPolicy(kind="webdav", url=server.url + quote(DAV_BASE), username="alice")
        with pytest.raises(SecondCopyError, match="https://"):
            WebDavTarget(policy, creds)
        assert server.seen == [], "nothing is sent before the scheme check"

        policy.allow_http = True
        WebDavTarget(policy, creds).close()
        assert server.count("PROPFIND") == 1
    finally:
        server.stop()


def test_webdav_wrong_password_and_missing_folder(
    seeded_config: AppConfig, dav: FakeWebDav
) -> None:
    creds = CredentialService()
    wrong = "Wrong-Dav-Pass-77"
    creds.store_secret(WEBDAV_PASSWORD_NAME, wrong)
    policy = SecondCopyPolicy(kind="webdav", **_dav_settings(dav))
    with pytest.raises(SecondCopyError, match="HTTP 401") as info:
        WebDavTarget(policy, creds)
    assert wrong not in str(info.value)

    creds.store_secret(WEBDAV_PASSWORD_NAME, DAV_PASSWORD)
    policy.url = dav.url + "/remote.php/dav/files/alice/missing"
    with pytest.raises(SecondCopyError, match="does not exist"):
        WebDavTarget(policy, creds)


# --- CLI --------------------------------------------------------------------


def test_copy_setup_s3_keeps_the_secret_out_of_config(
    cli_runner: CliRunner, seeded_config: AppConfig, s3: FakeS3
) -> None:
    BackupCrypto(seeded_config.backup_policy.encryption, CredentialService()).setup_keystore()
    seeded_config.save()
    answers = [
        "s3",
        s3.url,
        "auto",
        "pfs-backups",
        "fw-copies",
        "y",
        S3_KEY_ID,
        S3_SECRET,
        str(s3.cert_path),
        "30",
    ]
    result = cli_runner.invoke(app, ["copy", "setup"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, _output(result)
    assert "EXPERIMENTAL" in _output(result)

    policy = AppConfig.load().backup_policy.second_copy
    assert (policy.enabled, policy.kind, policy.bucket, policy.prefix) == (
        True,
        "s3",
        "pfs-backups",
        "fw-copies",
    )
    assert policy.path_style and policy.keep_days == 30
    assert S3_SECRET not in AppConfig.config_path().read_text()
    assert CredentialService().get_secret(S3_SECRET_NAME) == S3_SECRET

    status = cli_runner.invoke(app, ["copy", "status"])
    assert status.exit_code == 0
    assert "pfs-backups" in _output(status) and "EXPERIMENTAL" in _output(status)
    assert S3_SECRET not in _output(status)


def test_copy_setup_webdav_refuses_http_without_consent(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    BackupCrypto(seeded_config.backup_policy.encryption, CredentialService()).setup_keystore()
    seeded_config.save()
    result = cli_runner.invoke(app, ["copy", "setup"], input="webdav\nhttp://nas.lan/dav/pfs\nn\n")
    assert result.exit_code == 1
    assert not AppConfig.load().backup_policy.second_copy.enabled
