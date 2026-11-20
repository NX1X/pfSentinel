"""CLI commands for the second copy of the encrypted backups."""

from __future__ import annotations

from pathlib import Path

import typer

from pfsentinel.cli.formatters import console, print_error, print_info, print_success, print_warning
from pfsentinel.models.config import AppConfig, SecondCopyPolicy
from pfsentinel.services.credentials import CredentialService

app = typer.Typer(
    help="Second copy of the encrypted backups (folder, SFTP; experimental: S3, Azure, WebDAV)",
    no_args_is_help=True,
)

_KINDS = ("folder", "sftp", "s3", "azure", "webdav")
_EXPERIMENTAL = ("s3", "azure", "webdav")


def _load() -> tuple[AppConfig, CredentialService]:
    return AppConfig.load(), CredentialService()


def _trust_sftp_host(host: str, port: int) -> bool:
    from pfsentinel.services import host_keys
    from pfsentinel.services.connection import HostKeyError

    try:
        key = host_keys.fetch_host_key(host, port)
    except HostKeyError as e:
        print_error(str(e))
        return False
    fp = host_keys.fingerprint(key)
    current = host_keys.trusted_key(host, port)
    if current is not None and host_keys.fingerprint(current) == fp:
        print_success(f"Host key already trusted: {key.get_name()} {fp}")
        return True
    console.print(f"  Host:        {host}:{port}")
    console.print(f"  Key type:    {key.get_name()}")
    console.print(f"  Fingerprint: [bold]{fp}[/]")
    console.print("[dim]  Compare it on the server: ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub[/]")
    if not typer.confirm("Trust this host key?", default=False):
        return False
    host_keys.trust(host, port, key)
    return True


def _prompt_secret(creds: CredentialService, name: str, label: str) -> None:
    """Ask for a secret and store it. Blank keeps the stored one, if there is one."""
    stored = creds.get_secret(name) is not None
    value = typer.prompt(
        f"{label} (blank keeps the stored one)" if stored else label,
        hide_input=True,
        default="" if stored else None,
        show_default=False,
    ).strip()
    if value:
        creds.store_secret(name, value)
    elif not stored:
        print_error(f"{label} is required.")
        raise typer.Exit(1)


def _prompt_ca(policy: SecondCopyPolicy) -> None:
    value = typer.prompt(
        "CA certificate file, for a server with a private certificate (blank: system CAs)",
        default=policy.ca_cert_path or "",
        show_default=bool(policy.ca_cert_path),
    ).strip()
    if value and not Path(value).expanduser().is_file():
        print_error(f"File not found: {value}")
        raise typer.Exit(1)
    policy.ca_cert_path = str(Path(value).expanduser()) if value else None


def _prompt_prefix(policy: SecondCopyPolicy, where: str) -> None:
    policy.prefix = (
        typer.prompt(
            f"Folder inside the {where} (blank: top level)",
            default=policy.prefix or "",
            show_default=bool(policy.prefix),
        ).strip()
        or None
    )


def _experimental(kind: str) -> None:
    print_warning(
        f"The {kind} target is EXPERIMENTAL. After the first copy, check the files on the "
        "target, and keep the local backups."
    )


def _setup_s3(policy: SecondCopyPolicy, creds: CredentialService) -> None:
    from pfsentinel.services.cloud_targets import S3_SECRET_NAME

    _experimental("s3")
    console.print("[dim]  Endpoint and region for each provider: docs/second-copy.md[/]")
    policy.kind = "s3"
    policy.endpoint = typer.prompt(
        "Endpoint URL", default=policy.endpoint or "https://s3.us-east-1.amazonaws.com"
    ).strip()
    policy.region = typer.prompt(
        "Region (auto for Cloudflare R2 and Google Cloud Storage)",
        default=policy.region or "us-east-1",
    ).strip()
    policy.bucket = typer.prompt("Bucket", default=policy.bucket or "").strip()
    _prompt_prefix(policy, "bucket")
    policy.path_style = typer.confirm(
        "Path-style addressing? (yes for MinIO and most non-AWS services, no for AWS)",
        default=policy.path_style,
    )
    policy.access_key_id = typer.prompt("Access key ID", default=policy.access_key_id or "").strip()
    _prompt_secret(creds, S3_SECRET_NAME, "Secret access key")
    _prompt_ca(policy)


def _setup_azure(policy: SecondCopyPolicy, creds: CredentialService) -> None:
    from pfsentinel.services.cloud_targets import AZURE_KEY_NAME, AZURE_SAS_NAME

    _experimental("azure")
    policy.kind = "azure"
    policy.account = typer.prompt("Storage account name", default=policy.account or "").strip()
    policy.container = typer.prompt("Container", default=policy.container or "").strip()
    _prompt_prefix(policy, "container")
    console.print("[bold]sas[/] A container SAS token (recommended: limited to one container).")
    console.print("[bold]key[/] The storage account key (full access to the account).")
    auth = typer.prompt("Sign in with", default=policy.azure_auth).strip().lower()
    if auth == "sas":
        policy.azure_auth = "sas"
        console.print(
            "[dim]  The SAS needs these permissions: read, add, create, write, delete, list.[/]"
        )
        _prompt_secret(creds, AZURE_SAS_NAME, "SAS token")
    elif auth == "key":
        policy.azure_auth = "key"
        _prompt_secret(creds, AZURE_KEY_NAME, "Storage account key")
    else:
        print_error("Sign in with sas or key")
        raise typer.Exit(1)
    _prompt_ca(policy)


def _setup_webdav(policy: SecondCopyPolicy, creds: CredentialService) -> None:
    from pfsentinel.services.cloud_targets import WEBDAV_PASSWORD_NAME

    _experimental("webdav")
    console.print(
        "[dim]  Nextcloud: https://HOST/remote.php/dav/files/USER/FOLDER "
        "(create the folder first)[/]"
    )
    policy.kind = "webdav"
    url = typer.prompt("WebDAV folder URL", default=policy.url or "").strip()
    policy.url = url
    policy.allow_http = False
    if url.lower().startswith("http://"):
        print_warning(
            "This URL uses plain http: the password crosses the network unprotected. "
            "The files stay encrypted."
        )
        if not typer.confirm("Allow plain http (only for a server on your LAN)?", default=False):
            print_error("Use an https:// URL.")
            raise typer.Exit(1)
        policy.allow_http = True
    policy.username = typer.prompt("User name", default=policy.username or "").strip()
    _prompt_secret(creds, WEBDAV_PASSWORD_NAME, "Password (an app password for Nextcloud)")
    _prompt_ca(policy)


@app.command("setup")
def copy_setup() -> None:
    """Set up the second copy target (folder, SFTP, or experimental S3, Azure, WebDAV)."""
    from pfsentinel.services.second_copy import SecondCopyError, open_target

    config, creds = _load()
    if config.backup_policy.encryption.mode == "off":
        print_error("Turn on encryption first: only encrypted backups leave this PC.")
        print_info("Run: pfs encryption setup")
        raise typer.Exit(1)
    policy = config.backup_policy.second_copy

    console.print("[bold]folder[/] A local path: a network share or a Nextcloud-synced folder.")
    console.print("[bold]sftp[/]   An SSH server you run, with key login.")
    console.print("[bold]s3[/]     S3-compatible object storage (EXPERIMENTAL).")
    console.print("[bold]azure[/]  Azure Blob Storage (EXPERIMENTAL).")
    console.print("[bold]webdav[/] A WebDAV folder, for example Nextcloud (EXPERIMENTAL).")
    kind = typer.prompt("Target", default=policy.kind).strip().lower()
    if kind == "folder":
        path = Path(typer.prompt("Folder", default=policy.path or "")).expanduser()
        if not path.is_dir():
            print_error(f"Folder does not exist: {path}")
            raise typer.Exit(1)
        policy.kind, policy.path = "folder", str(path)
    elif kind == "sftp":
        policy.kind = "sftp"
        policy.host = typer.prompt("SSH host", default=policy.host or "")
        policy.port = int(typer.prompt("SSH port", default=str(policy.port)))
        policy.username = typer.prompt("SSH user", default=policy.username or "")
        policy.key_path = typer.prompt("Private key file", default=policy.key_path or "")
        policy.path = typer.prompt("Remote folder", default=policy.path or "pfsentinel")
        passphrase = typer.prompt(
            "Key passphrase (blank if none)", hide_input=True, default="", show_default=False
        )
        if passphrase:
            creds.store_secret("__second_copy_keypass__", passphrase)
        if not _trust_sftp_host(policy.host, policy.port):
            print_error("Host key not trusted; second copy not set up.")
            raise typer.Exit(1)
    elif kind == "s3":
        _setup_s3(policy, creds)
    elif kind == "azure":
        _setup_azure(policy, creds)
    elif kind == "webdav":
        _setup_webdav(policy, creds)
    else:
        print_error("Target must be one of: " + ", ".join(_KINDS))
        raise typer.Exit(1)
    policy.keep_days = int(typer.prompt("Keep copies for days", default=str(policy.keep_days)))

    try:
        open_target(policy, creds).close()
    except SecondCopyError as e:
        print_error(str(e))
        raise typer.Exit(1)
    policy.enabled = True
    config.save()
    print_success("Second copy is on: after each backup, encrypted files are copied there.")
    print_info("Copy the existing encrypted backups now: pfs copy now")


@app.command("now")
def copy_now(
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (default: all)"),
) -> None:
    """Copy every encrypted backup the target does not have yet."""
    from pfsentinel.services.second_copy import SecondCopyService

    config, creds = _load()
    if not config.backup_policy.second_copy.enabled:
        print_error("Second copy is off. Run: pfs copy setup")
        raise typer.Exit(1)
    svc = SecondCopyService(config, creds)
    failed = False
    for device_id in [device] if device else [d.id for d in config.devices]:
        result = svc.sync(device_id)
        print_info(
            f"{device_id}: {len(result.copied)} copied, "
            f"{len(result.removed_days)} old day folder(s) removed"
        )
        if result.skipped_plain:
            print_warning(
                f"  {len(result.skipped_plain)} plain file(s) not copied. "
                "Encrypt them: pfs encryption encrypt-existing"
            )
        for error in result.errors:
            print_error(f"  {error}")
            failed = True
    if failed:
        raise typer.Exit(1)


@app.command("status")
def copy_status() -> None:
    """Show the second copy target."""
    config, _ = _load()
    policy = config.backup_policy.second_copy
    if not policy.enabled:
        print_warning("Second copy is OFF. Run: pfs copy setup")
        return
    print_success(f"Second copy is ON ({policy.kind}): {_describe(policy)}")
    if policy.kind in _EXPERIMENTAL:
        print_warning(f"The {policy.kind} target is EXPERIMENTAL.")
    console.print(f"  Kept for: {policy.keep_days} days")


def _describe(policy: SecondCopyPolicy) -> str:
    if policy.kind == "sftp":
        return f"sftp://{policy.username}@{policy.host}:{policy.port}/{policy.path}"
    prefix = f"/{policy.prefix.strip('/')}" if policy.prefix else ""
    if policy.kind == "s3":
        style = "path-style" if policy.path_style else "virtual-host"
        return f"bucket {policy.bucket}{prefix} at {policy.endpoint} ({policy.region}, {style})"
    if policy.kind == "azure":
        auth = "SAS token" if policy.azure_auth == "sas" else "account key"
        return f"{policy.account}/{policy.container}{prefix} ({auth})"
    if policy.kind == "webdav":
        return f"{policy.url} as {policy.username}"
    return policy.path or ""


@app.command("disable")
def copy_disable() -> None:
    """Stop copying. Files already on the target stay there."""
    config, _ = _load()
    config.backup_policy.second_copy.enabled = False
    config.save()
    print_success("Second copy is off. Files already copied stay on the target.")
