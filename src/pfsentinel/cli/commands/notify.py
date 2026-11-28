"""CLI commands for notification setup and testing."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import typer

from pfsentinel.cli.formatters import console, print_error, print_info, print_success, print_warning
from pfsentinel.models.config import AppConfig
from pfsentinel.services.credentials import CredentialService

app = typer.Typer(help="Notification setup (email, Telegram, Slack, desktop, Event Log, digest)")


def _load() -> tuple[AppConfig, CredentialService]:
    return AppConfig.load(), CredentialService()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

telegram_app = typer.Typer(help="Telegram notifications")
app.add_typer(telegram_app, name="telegram")


@telegram_app.command("setup")
def telegram_setup() -> None:
    """Configure Telegram bot notifications.

    Steps:
      1. Open Telegram, search @BotFather
      2. Send /newbot, choose a name and username (must end in 'bot')
      3. BotFather gives you a token like  123456789:ABCdef...
      4. Send any message to your new bot
      5. Run this command and paste the token - chat ID is auto-detected
    """
    config, creds = _load()

    console.print()
    console.print("[bold cyan]Telegram Setup[/]")
    console.print("[dim]──────────────────────────────────────[/]")
    console.print("[dim]Step 1: In Telegram, search for [bold]@BotFather[/][/]")
    console.print("[dim]Step 2: Send [bold]/newbot[/] and follow the prompts[/]")
    console.print("[dim]Step 3: Copy the bot token BotFather gives you[/]")
    console.print("[dim]        It looks like: 123456789:ABCdefGHIjklMNOpqrSTU[/]")
    console.print("[dim]Step 4: Send any message to your new bot in Telegram[/]")
    console.print("[dim]Step 5: Paste the token below[/]")
    console.print()

    try:
        token = typer.prompt("Bot token", hide_input=True)
    except typer.Abort:
        print_info("Aborted.")
        return

    console.print()
    console.print("[dim]Fetching your chat ID from recent bot messages...[/]")

    detected = _telegram_get_chat_id(token)
    chat_id: str | None = None

    if detected:
        console.print(f"[green]Detected chat ID:[/] {detected}")
        try:
            use_detected = typer.confirm("Use this chat ID?", default=True)
        except typer.Abort:
            print_info("Aborted.")
            return
        if use_detected:
            chat_id = detected
        else:
            try:
                chat_id = typer.prompt("Enter chat ID manually")
            except typer.Abort:
                print_info("Aborted.")
                return
    else:
        console.print("[yellow]Could not auto-detect chat ID.[/]")
        console.print("[dim]Make sure you sent a message to your bot, then retry.[/]")
        console.print(
            "[dim]Or enter the chat ID manually (open t.me/userinfobot to find yours).[/]"
        )
        try:
            chat_id = typer.prompt("Chat ID (or press Enter to cancel)", default="")
        except typer.Abort:
            print_info("Aborted.")
            return
        if not chat_id:
            print_info("Setup cancelled. Run again after sending a message to your bot.")
            return

    creds.store_telegram_token(token)
    config.notifications.telegram_chat_id = chat_id
    config.notifications.telegram_enabled = True
    config.save()

    print_success("Telegram configured")
    print_info(f"  Chat ID: {chat_id}")
    print_info("  Token: stored in keyring")

    try:
        if typer.confirm("\nSend a test message now?", default=True):
            _telegram_send_test(token, chat_id)
    except typer.Abort:
        pass


@telegram_app.command("enable")
def telegram_enable() -> None:
    """Re-enable Telegram notifications (if previously disabled)."""
    config, creds = _load()
    if not creds.get_telegram_token() or not config.notifications.telegram_chat_id:
        print_error("Telegram not configured. Run: pfs notify telegram setup")
        raise typer.Exit(1)
    config.notifications.telegram_enabled = True
    config.save()
    print_success("Telegram notifications enabled")


@telegram_app.command("disable")
def telegram_disable() -> None:
    """Disable Telegram notifications (keeps credentials)."""
    config, _ = _load()
    config.notifications.telegram_enabled = False
    config.save()
    print_success("Telegram notifications disabled")


@telegram_app.command("status")
def telegram_status() -> None:
    """Show Telegram configuration status."""
    config, creds = _load()
    enabled = config.notifications.telegram_enabled
    has_token = creds.get_telegram_token() is not None
    chat_id = config.notifications.telegram_chat_id

    console.print()
    console.print(f"  Enabled:  {'[green]Yes[/]' if enabled else '[dim]No[/]'}")
    console.print(f"  Token:    {'[green]stored[/]' if has_token else '[red]missing[/]'}")
    console.print(f"  Chat ID:  {chat_id or '[red]not set[/]'}")
    console.print()

    if enabled and has_token and chat_id:
        print_success("Telegram is fully configured")
    elif has_token and chat_id and not enabled:
        print_info("Configured but disabled. Run: pfs notify telegram enable")
    else:
        print_info("Run: pfs notify telegram setup")


def _telegram_get_chat_id(token: str) -> str | None:
    try:
        import requests

        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            timeout=10,
        )
        data = resp.json()
        results = data.get("result", [])
        if results:
            return str(results[-1]["message"]["chat"]["id"])
    except Exception:
        pass
    return None


def _telegram_send_test(token: str, chat_id: str) -> None:
    try:
        import requests

        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "*pfSentinel* \u2713 Test notification\nTelegram is configured correctly.",
                "parse_mode": "Markdown",
            },
            timeout=10,
            allow_redirects=False,
        )
        resp.raise_for_status()
        print_success("Test message sent!")
    except Exception as e:
        from pfsentinel.services.notifications import describe_send_error

        # Never print the raw error: requests quotes the URL, which holds the token.
        print_error(f"Failed to send test message: {describe_send_error(e)}")


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

slack_app = typer.Typer(help="Slack notifications")
app.add_typer(slack_app, name="slack")


@slack_app.command("setup")
def slack_setup() -> None:
    """Configure Slack webhook notifications.

    Steps:
      1. Go to https://api.slack.com/apps and create an app (or pick existing)
      2. Under 'Incoming Webhooks', activate and add to a channel
      3. Copy the Webhook URL (starts with https://hooks.slack.com/services/...)
    """
    config, creds = _load()

    console.print()
    console.print("[bold cyan]Slack Setup[/]")
    console.print("[dim]──────────────────────────────────────[/]")
    console.print("[dim]1. Visit [link=https://api.slack.com/apps]api.slack.com/apps[/link][/]")
    console.print("[dim]2. Create app → 'Incoming Webhooks' → Add to channel[/]")
    console.print("[dim]3. Copy the Webhook URL and paste it below[/]")
    console.print()

    try:
        webhook_url = typer.prompt("Webhook URL", hide_input=True)
    except typer.Abort:
        print_info("Aborted.")
        return

    from urllib.parse import urlparse

    parsed = urlparse(webhook_url.strip())
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host == "hooks.slack.com" or host.endswith(".hooks.slack.com")
    ):
        # Backup notifications are only ever sent to Slack's webhook host.
        print_error("That is not a Slack webhook URL (expected https://hooks.slack.com/...).")
        print_info("  Nothing was saved. Copy the URL from Incoming Webhooks and try again.")
        raise typer.Exit(1)
    webhook_url = webhook_url.strip()

    creds.store_slack_webhook(webhook_url)
    config.notifications.slack_enabled = True
    config.save()

    print_success("Slack configured")
    print_info("  Webhook URL: stored in keyring")

    try:
        if typer.confirm("\nSend a test message now?", default=True):
            _slack_send_test(webhook_url)
    except typer.Abort:
        pass


@slack_app.command("enable")
def slack_enable() -> None:
    """Re-enable Slack notifications (if previously disabled)."""
    config, creds = _load()
    if not creds.get_slack_webhook():
        print_error("Slack not configured. Run: pfs notify slack setup")
        raise typer.Exit(1)
    config.notifications.slack_enabled = True
    config.save()
    print_success("Slack notifications enabled")


@slack_app.command("disable")
def slack_disable() -> None:
    """Disable Slack notifications (keeps webhook URL)."""
    config, _ = _load()
    config.notifications.slack_enabled = False
    config.save()
    print_success("Slack notifications disabled")


@slack_app.command("status")
def slack_status() -> None:
    """Show Slack configuration status."""
    config, creds = _load()
    enabled = config.notifications.slack_enabled
    has_webhook = creds.get_slack_webhook() is not None

    console.print()
    console.print(f"  Enabled:  {'[green]Yes[/]' if enabled else '[dim]No[/]'}")
    console.print(f"  Webhook:  {'[green]stored[/]' if has_webhook else '[red]missing[/]'}")
    console.print()

    if enabled and has_webhook:
        print_success("Slack is fully configured")
    elif has_webhook and not enabled:
        print_info("Configured but disabled. Run: pfs notify slack enable")
    else:
        print_info("Run: pfs notify slack setup")


def _slack_send_test(webhook_url: str) -> None:
    try:
        import requests

        resp = requests.post(
            webhook_url,
            json={
                "attachments": [
                    {
                        "color": "good",
                        "title": ":white_check_mark: pfSentinel - Test notification",
                        "text": "Slack is configured correctly.",
                        "footer": "pfSentinel",
                    }
                ]
            },
            timeout=10,
            allow_redirects=False,
        )
        resp.raise_for_status()
        print_success("Test message sent!")
    except requests.exceptions.RequestException:
        print_error("Failed to send test message. Check your webhook URL and network.")
    except Exception:
        print_error("Failed to send test message")


# ---------------------------------------------------------------------------
# Windows Event Log (syslog / journald on Linux)
# ---------------------------------------------------------------------------

eventlog_app = typer.Typer(help="Windows Event Log (syslog/journald on Linux)")
app.add_typer(eventlog_app, name="eventlog")


@eventlog_app.command("enable")
def eventlog_enable() -> None:
    """Write every alert to the Windows Event Log (Application, source pfSentinel).

    On Windows the source is registered once, which needs an administrator
    PowerShell ('pfs schedule enable' also does it). On Linux, alerts go to
    syslog/journald with the tag 'pfsentinel'.
    """
    from pfsentinel.services.notifications import register_event_source
    from pfsentinel.utils.platform import is_elevated, is_windows

    config, _ = _load()
    if is_windows():
        try:
            register_event_source()
        except RuntimeError as e:
            if not is_elevated():
                print_error("Registering the event source needs an administrator PowerShell.")
                print_info(
                    "Open PowerShell with 'Run as administrator' and run this command again."
                )
            else:
                print_error(f"Could not register the event source: {e}")
            raise typer.Exit(1)
    config.notifications.windows_event_log_enabled = True
    config.save()
    target = "Windows Event Log (Application, source pfSentinel)" if is_windows() else "syslog"
    print_success(f"Alerts are written to the {target}")
    print_info("Event IDs: 1000 backup OK, 1001 backup failed, 1002 risky change,")
    print_info("           1003 no recent backup, 1004 restore check failed, 1005 copy failed,")
    print_info("           1006 update available, 1008 backup partly failed")


@eventlog_app.command("disable")
def eventlog_disable() -> None:
    """Stop writing alerts to the Event Log / syslog."""
    config, _ = _load()
    config.notifications.windows_event_log_enabled = False
    config.save()
    print_success("Event Log / syslog alerts disabled")


# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------

email_app = typer.Typer(help="Email notifications (SMTP, any mail server)")
app.add_typer(email_app, name="email")

SecurityMode = Literal["starttls", "tls", "none"]
_SECURITY_MODES: dict[str, SecurityMode] = {"starttls": "starttls", "tls": "tls", "none": "none"}
_DEFAULT_PORTS = {"starttls": 587, "tls": 465, "none": 25}


def _ask(text: str, default: str | None = None, hide: bool = False) -> str:
    try:
        value = typer.prompt(text, default=default, hide_input=hide, show_default=not hide)
    except typer.Abort:
        print_info("Aborted. Nothing was saved.")
        raise typer.Exit(1)
    return str(value).strip()


@email_app.command("setup")
def email_setup(
    host: str | None = typer.Option(None, "--host", help="SMTP server name or IP address"),
    port: int | None = typer.Option(
        None, "--port", min=1, max=65535, help="Default: 587 starttls, 465 tls, 25 none"
    ),
    security: str | None = typer.Option(
        None, "--security", help="starttls (default), tls (implicit, port 465) or none"
    ),
    username: str | None = typer.Option(None, "--username", help="SMTP login name"),
    no_login: bool = typer.Option(False, "--no-login", help="The server needs no login"),
    sender: str | None = typer.Option(None, "--from", help="From address"),
    recipients: str | None = typer.Option(None, "--to", help="To address(es), separated by commas"),
    timeout: int = typer.Option(20, "--timeout", min=1, max=300, help="Seconds"),
    ca_cert: Path | None = typer.Option(
        None,
        "--ca-cert",
        help="PEM file of your CA, for a lab mail server with its own certificate",
    ),
    test: bool = typer.Option(True, "--test/--no-test", help="Send a test email after setup"),
) -> None:
    """Configure email alerts through any SMTP server.

    Options not given on the command line are asked for. The password is
    always asked for (hidden) and stored in the OS keystore, never in
    config.json. Examples:

      Provider:  pfs notify email setup --host smtp.example.com --username me@example.com

      Mailpit:   pfs notify email setup --host 192.168.30.21 --port 1025 --security none
    """
    from pfsentinel.services.email_sender import (
        SMTP_KEYSTORE_NAME,
        parse_addresses,
        validate_address,
    )

    config, creds = _load()
    n = config.notifications

    console.print()
    console.print("[bold cyan]Email Setup[/]")
    console.print("[dim]──────────────────────────────────────[/]")

    host = host or _ask("SMTP server", default=n.email_host)
    if not host or any(c.isspace() for c in host):
        print_error("The SMTP server name is empty or has spaces. Nothing was saved.")
        raise typer.Exit(1)
    mode = (security or _ask("Security: starttls, tls or none", default=n.email_security)).lower()
    if mode in _SECURITY_MODES:
        n.email_security = _SECURITY_MODES[mode]
    else:
        print_error(f"Unknown security mode {mode!r}: use starttls, tls or none.")
        raise typer.Exit(1)
    if port is None:
        try:
            port = int(_ask("Port", default=str(_DEFAULT_PORTS[mode])))
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            print_error("The port must be a number from 1 to 65535. Nothing was saved.")
            raise typer.Exit(1)

    if mode == "none":
        if username:
            print_error("A login needs an encrypted connection: use --security starttls or tls.")
            raise typer.Exit(1)
        print_warning("Security 'none' sends mail unencrypted: use it only for a local server.")
        username = None
    elif no_login:
        username = None
    elif username is None:
        username = _ask("Username (Enter for no login)", default=n.email_username or "") or None

    password: str | None = None
    if username:
        stored = creds.get_secret(SMTP_KEYSTORE_NAME)
        prompt = "Password (Enter to keep the stored one)" if stored else "Password"
        password = _ask(prompt, default="" if stored else None, hide=True) or stored
        if not password:
            print_error("A password is needed for the login. Nothing was saved.")
            raise typer.Exit(1)

    try:
        default_from = n.email_from or (username if username and "@" in username else None)
        from_addr = validate_address(sender or _ask("From address", default=default_from))
        default_to = ", ".join(n.email_to) or None
        to_addrs = parse_addresses(recipients or _ask("To address(es)", default=default_to))
    except ValueError as e:
        print_error(f"{e}. Nothing was saved.")
        raise typer.Exit(1)

    if username and password:
        creds.store_secret(SMTP_KEYSTORE_NAME, password)
    else:
        creds.delete(SMTP_KEYSTORE_NAME)
    n.email_host = host
    n.email_port = port
    n.email_username = username
    n.email_from = from_addr
    n.email_to = to_addrs
    n.email_timeout = timeout
    n.email_ca_cert = str(ca_cert.expanduser()) if ca_cert and mode != "none" else None
    n.email_enabled = True
    config.save()

    print_success("Email configured")
    print_info(f"  Server: {host}:{port} ({mode})")
    print_info(f"  Login: {username or 'none'}")
    if username:
        print_info(f"  Password: stored in {creds.backend_name()}")
        if not creds.is_persistent:
            print_warning("  The password is kept in memory only and is lost when pfs exits.")
    print_info(f"  From: {from_addr}")
    print_info(f"  To: {', '.join(to_addrs)}")
    if n.email_ca_cert:
        print_info(f"  Trusted CA: {n.email_ca_cert}")

    if test:
        _email_send_test(config, creds)


def _email_send_test(config: AppConfig, creds: CredentialService) -> bool:
    from pfsentinel.services.email_sender import SMTP_KEYSTORE_NAME, EmailError, send_email

    n = config.notifications
    password = creds.get_secret(SMTP_KEYSTORE_NAME) if n.email_username else None
    try:
        send_email(
            n,
            password,
            "pfSentinel - Test email",
            "This is a test email from pfSentinel.\nEmail alerts are configured correctly.",
        )
    except EmailError as e:
        print_error(f"Test email failed: {e}")
        return False
    print_success(f"Test email sent to {', '.join(n.email_to)}")
    return True


def _email_problem(config: AppConfig, creds: CredentialService) -> str | None:
    """Why email cannot send, or None when it is fully configured."""
    from pfsentinel.services.email_sender import SMTP_KEYSTORE_NAME

    n = config.notifications
    if not n.email_host or not n.email_from or not n.email_to:
        return "server, From or To missing"
    if n.email_username and not creds.get_secret(SMTP_KEYSTORE_NAME):
        return "password missing from the keystore"
    return None


@email_app.command("enable")
def email_enable() -> None:
    """Re-enable email alerts (if previously disabled)."""
    config, creds = _load()
    problem = _email_problem(config, creds)
    if problem:
        print_error(f"Email not configured ({problem}). Run: pfs notify email setup")
        raise typer.Exit(1)
    config.notifications.email_enabled = True
    config.save()
    print_success("Email notifications enabled")


@email_app.command("disable")
def email_disable() -> None:
    """Disable email alerts (keeps the settings and the password)."""
    config, _ = _load()
    config.notifications.email_enabled = False
    config.save()
    print_success("Email notifications disabled")


@email_app.command("status")
def email_status() -> None:
    """Show email configuration status. Never shows the password."""
    from pfsentinel.services.email_sender import SMTP_KEYSTORE_NAME

    config, creds = _load()
    n = config.notifications
    if n.email_username:
        has_password = creds.get_secret(SMTP_KEYSTORE_NAME) is not None
        stored_label = "[green]stored[/]" if has_password else "[red]missing[/]"
    else:
        stored_label = "[dim]not used[/]"
    tls = f"checked (also trusts {n.email_ca_cert})" if n.email_ca_cert else "checked"
    server = f"{n.email_host}:{n.email_port}" if n.email_host else "[red]not set[/]"

    console.print()
    console.print(f"  Enabled:   {'[green]Yes[/]' if n.email_enabled else '[dim]No[/]'}")
    console.print(f"  Server:    {server}")
    console.print(f"  Security:  {n.email_security} (certificate {tls})")
    console.print(f"  Login:     {n.email_username or '[dim]none[/]'}")
    console.print(f"  Password:  {stored_label}")
    console.print(f"  From:      {n.email_from or '[red]not set[/]'}")
    console.print(f"  To:        {', '.join(n.email_to) or '[red]not set[/]'}")
    console.print(f"  Timeout:   {n.email_timeout} s")
    console.print()

    problem = _email_problem(config, creds)
    if n.email_enabled and not problem:
        print_success("Email is fully configured")
    elif not problem:
        print_info("Configured but disabled. Run: pfs notify email enable")
    else:
        print_info("Run: pfs notify email setup")


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------

digest_app = typer.Typer(help="Daily digest: one summary a day instead of a message per backup")
app.add_typer(digest_app, name="digest")


def _apply_check_schedule(config: AppConfig, quiet: bool = False) -> None:
    """Re-register the daily check task, so it runs at the digest time too (or no longer)."""
    from pfsentinel.cli.commands.schedule import scheduler_for
    from pfsentinel.utils.platform import is_elevated, is_windows

    if not config.schedule.enabled or not config.schedule.use_windows_task_scheduler:
        if not quiet:
            print_warning("The digest is sent by the daily check task, which is not scheduled.")
            print_info("  Run: pfs schedule enable")
        return
    if is_windows() and not is_elevated():
        if not quiet:
            print_info("  To run the check task at the digest time, run this command again")
            print_info("  from an administrator PowerShell. Until then, the digest goes out")
            print_info("  with the next check after its time (at sign-in or at 12:00).")
        return
    if scheduler_for(config).apply_schedule():
        print_info("  Daily check task updated")
    else:
        print_warning("  Could not update the daily check task. Run: pfs schedule status")


@digest_app.command("enable")
def digest_enable(
    at: str | None = typer.Option(
        None, "--time", help="Time of day, HH:MM (default: keep the current one, at first 08:00)"
    ),
) -> None:
    """Send one summary a day of every enabled device.

    Successful backups are then reported only in the digest. Failures, risky
    changes and other problems are still sent at once.
    """
    from pfsentinel.utils import unix_schedule

    config, _ = _load()
    n = config.notifications
    try:
        n.digest_time = unix_schedule.normalize_hhmm(at or n.digest_time)
    except unix_schedule.ScheduleSpecError as e:
        print_error(str(e))
        raise typer.Exit(2)
    n.digest_enabled = True
    config.save()
    print_success(f"Daily digest enabled at {n.digest_time}")
    print_info("  Successful backups are now only in the digest. Problems are still sent at once.")
    if not _enabled_channels(config):
        print_warning("  No notification channel is enabled. Run: pfs notify status")
    _apply_check_schedule(config)


@digest_app.command("disable")
def digest_disable() -> None:
    """Go back to one message per successful backup."""
    config, _ = _load()
    config.notifications.digest_enabled = False
    config.save()
    print_success("Daily digest disabled")
    _apply_check_schedule(config, quiet=True)


@digest_app.command("status")
def digest_status() -> None:
    """Show the daily digest settings and when it was last sent."""
    from pfsentinel.services.digest import last_sent_date

    config, _ = _load()
    n = config.notifications
    last = last_sent_date()
    console.print()
    console.print(f"  Enabled:    {'[green]Yes[/]' if n.digest_enabled else '[dim]No[/]'}")
    console.print(f"  Time:       {n.digest_time}")
    console.print(f"  Last sent:  {last.isoformat() if last else '[dim]never[/]'}")
    console.print()
    if not n.digest_enabled:
        print_info("Run: pfs notify digest enable --time 08:00")


@digest_app.command("send")
def digest_send(
    preview: bool = typer.Option(False, "--preview", help="Only print it, send nothing"),
) -> None:
    """Send the digest now. The scheduled digest still goes out today."""
    from pfsentinel.services.backup import BackupService
    from pfsentinel.services.digest import build_digest, send_digest

    config, creds = _load()
    svc = BackupService(config, creds)
    if preview:
        digest = build_digest(config, svc.list_backups)
        console.print(digest.title, markup=False, highlight=False)
        console.print(digest.body, markup=False, highlight=False)
        return
    digest, results = send_digest(config, svc.list_backups, svc.notifications, remember=False)
    console.print(digest.body, markup=False, highlight=False)
    console.print()
    _print_results(results)


# ---------------------------------------------------------------------------
# Desktop notifications (Windows toast, notify-send on Linux)
# ---------------------------------------------------------------------------

desktop_app = typer.Typer(help="Desktop notifications (Windows toast, notify-send on Linux)")
app.add_typer(desktop_app, name="desktop")


def _set_desktop(enabled: bool) -> None:
    from pfsentinel.utils.platform import is_linux, is_windows

    config, _ = _load()
    if is_windows():
        config.notifications.windows_toast_enabled = enabled
    elif is_linux():
        config.notifications.linux_desktop_enabled = enabled
    else:
        print_error("Desktop notifications are supported on Windows and Linux only.")
        raise typer.Exit(1)
    config.save()
    print_success(f"Desktop notifications {'enabled' if enabled else 'disabled'}")


def _desktop_hint() -> None:
    import os
    import shutil

    from pfsentinel.utils.platform import is_linux

    if not is_linux():
        return
    if shutil.which("notify-send") is None:
        print_warning("  notify-send was not found, so nothing is shown. Install the")
        print_info("  package that has it (libnotify-bin on Debian and Ubuntu, libnotify")
        print_info("  on Fedora and Arch).")
    elif not any(os.environ.get(v) for v in ("DISPLAY", "WAYLAND_DISPLAY")):
        print_info("  No desktop session here: alerts are shown only when pfs runs in one.")


@desktop_app.command("enable")
def desktop_enable() -> None:
    """Show alerts on this desktop (on by default)."""
    _set_desktop(True)
    _desktop_hint()


@desktop_app.command("disable")
def desktop_disable() -> None:
    """Stop showing alerts on this desktop."""
    _set_desktop(False)


@desktop_app.command("status")
def desktop_status() -> None:
    """Show desktop notification status."""
    import shutil

    from pfsentinel.utils.platform import is_linux, is_windows

    config, _ = _load()
    n = config.notifications
    console.print()
    if is_windows():
        on = n.windows_toast_enabled
        console.print(f"  Windows toast:  {'[green]enabled[/]' if on else '[dim]disabled[/]'}")
    elif is_linux():
        on = n.linux_desktop_enabled
        console.print(f"  Enabled:      {'[green]Yes[/]' if on else '[dim]No[/]'}")
        console.print(f"  notify-send:  {shutil.which('notify-send') or '[red]not found[/]'}")
    else:
        console.print("  [dim]Not supported on this system[/]")
    console.print()
    _desktop_hint()


# ---------------------------------------------------------------------------
# Test all / Status
# ---------------------------------------------------------------------------


def _enabled_channels(config: AppConfig) -> list[str]:
    from pfsentinel.utils.platform import is_linux, is_windows

    n = config.notifications
    channels = []
    if n.telegram_enabled:
        channels.append("Telegram")
    if n.slack_enabled:
        channels.append("Slack")
    if n.email_enabled:
        channels.append("Email")
    if n.windows_toast_enabled and is_windows():
        channels.append("Windows Toast")
    if n.linux_desktop_enabled and is_linux():
        channels.append("Desktop")
    if n.windows_event_log_enabled:
        channels.append("Windows Event Log" if is_windows() else "System log")
    return channels


def _print_results(results: dict[str, str | None]) -> None:
    any_ok = any(err is None for err in results.values())
    any_fail = any(err is not None for err in results.values())
    for channel, err in results.items():
        if err is None:
            print_success(f"  {channel}: sent")
        else:
            print_error(f"  {channel}: failed: {err}")

    if not results:
        print_warning("Nothing was sent: no channel is enabled or available.")
    elif any_ok and not any_fail:
        print_success("All notifications sent successfully.")
    elif any_ok and any_fail:
        print_warning("Some notifications failed (see above).")
    else:
        print_error("All notifications failed.")


@app.command("test")
def notify_test() -> None:
    """Send a test notification to all enabled channels."""
    config, creds = _load()
    from pfsentinel.services.notifications import NotificationService

    channels = _enabled_channels(config)
    if not channels:
        print_warning("No notification channels enabled.")
        print_info("Run: pfs notify email setup")
        print_info("     pfs notify telegram setup")
        print_info("     pfs notify slack setup")
        return

    svc = NotificationService(config.notifications, creds)
    results = svc.notify_info("pfSentinel - Test", "This is a test notification from pfSentinel.")
    if "Desktop" in channels and "Desktop" not in results:
        print_info("  Desktop: skipped (no desktop session, or notify-send not found)")
    _print_results(results)


@app.command("status")
def notify_status() -> None:
    """Show all notification channel statuses."""
    config, creds = _load()
    from pfsentinel.utils.platform import is_linux, is_windows

    console.print()
    console.print("[bold]Notification Channels[/]")
    console.print("[dim]──────────────────────────────────────[/]")

    tg_ok = (
        config.notifications.telegram_enabled
        and creds.get_telegram_token()
        and config.notifications.telegram_chat_id
    )
    console.print(f"  Telegram:     {'[green]enabled[/]' if tg_ok else '[dim]disabled[/]'}")

    sl_ok = config.notifications.slack_enabled and creds.get_slack_webhook()
    console.print(f"  Slack:        {'[green]enabled[/]' if sl_ok else '[dim]disabled[/]'}")

    em_ok = config.notifications.email_enabled and _email_problem(config, creds) is None
    console.print(f"  Email:        {'[green]enabled[/]' if em_ok else '[dim]disabled[/]'}")

    if is_windows():
        wt_ok = config.notifications.windows_toast_enabled
        console.print(f"  Windows Toast: {'[green]enabled[/]' if wt_ok else '[dim]disabled[/]'}")
    elif is_linux():
        dt_ok = config.notifications.linux_desktop_enabled
        console.print(f"  Desktop:      {'[green]enabled[/]' if dt_ok else '[dim]disabled[/]'}")
    el = "Event Log" if is_windows() else "syslog"
    el_ok = config.notifications.windows_event_log_enabled
    console.print(f"  {el + ':':<14}{'[green]enabled[/]' if el_ok else '[dim]disabled[/]'}")

    console.print()
    digest = config.notifications.digest_enabled
    on_success = "Yes" if config.notifications.notify_on_success else "No"
    on_failure = "Yes" if config.notifications.notify_on_failure else "No"
    if digest:
        on_success = f"in the daily digest at {config.notifications.digest_time}"
    console.print(f"  Daily digest: {'Yes' if digest else 'No'}")
    console.print(f"  Notify on success: {on_success}")
    console.print(f"  Notify on failure: {on_failure} (also risky changes and other alerts)")
    console.print(f"  No-recent-backup alert after: {config.notifications.stale_after_days} day(s)")
    release_check = "daily" if config.notifications.update_alerts else "off"
    console.print(f"  Release check: {release_check} (pfs config set update-check on|off)")
    console.print()
