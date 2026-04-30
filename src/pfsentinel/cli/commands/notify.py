"""CLI commands for notification setup and testing."""

from __future__ import annotations

import click
import typer

from pfsentinel.cli.formatters import console, print_error, print_info, print_success, print_warning
from pfsentinel.models.config import AppConfig
from pfsentinel.services.credentials import CredentialService

app = typer.Typer(help="Notification setup (Telegram, Slack)")


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
    except click.Abort:
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
        except click.Abort:
            print_info("Aborted.")
            return
        if use_detected:
            chat_id = detected
        else:
            try:
                chat_id = typer.prompt("Enter chat ID manually")
            except click.Abort:
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
        except click.Abort:
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
    except click.Abort:
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
        )
        resp.raise_for_status()
        print_success("Test message sent!")
    except Exception as e:
        print_error(f"Failed to send test message: {e}")


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
    except click.Abort:
        print_info("Aborted.")
        return

    if not webhook_url.startswith("https://hooks.slack.com/"):
        print_warning(
            "URL doesn't look like a Slack webhook"
            " (expected https://hooks.slack.com/...). Continuing anyway."
        )

    creds.store_slack_webhook(webhook_url)
    config.notifications.slack_enabled = True
    config.save()

    print_success("Slack configured")
    print_info("  Webhook URL: stored in keyring")

    try:
        if typer.confirm("\nSend a test message now?", default=True):
            _slack_send_test(webhook_url)
    except click.Abort:
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
        )
        resp.raise_for_status()
        print_success("Test message sent!")
    except requests.exceptions.RequestException:
        print_error("Failed to send test message — check your webhook URL and network")
    except Exception:
        print_error("Failed to send test message")


# ---------------------------------------------------------------------------
# Test all / Status
# ---------------------------------------------------------------------------


@app.command("test")
def notify_test() -> None:
    """Send a test notification to all enabled channels."""
    config, creds = _load()
    from pfsentinel.services.notifications import NotificationService

    channels = []
    if config.notifications.telegram_enabled:
        channels.append("Telegram")
    if config.notifications.slack_enabled:
        channels.append("Slack")
    if config.notifications.windows_toast_enabled:
        channels.append("Windows Toast")

    if not channels:
        print_warning("No notification channels enabled.")
        print_info("Run: pfs notify telegram setup")
        print_info("     pfs notify slack setup")
        return

    svc = NotificationService(config.notifications, creds)
    results = svc.notify_info("pfSentinel - Test", "This is a test notification from pfSentinel.")

    any_ok = False
    any_fail = False
    for channel, err in results.items():
        if err is None:
            print_success(f"  {channel}: sent")
            any_ok = True
        else:
            print_error(f"  {channel}: failed — {err}")
            any_fail = True

    if any_ok and not any_fail:
        print_success("All notifications sent successfully.")
    elif any_ok and any_fail:
        print_warning("Some notifications failed (see above).")
    else:
        print_error("All notifications failed.")


@app.command("status")
def notify_status() -> None:
    """Show all notification channel statuses."""
    config, creds = _load()
    from pfsentinel.utils.platform import is_windows

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

    wt_ok = config.notifications.windows_toast_enabled and is_windows()
    wt_str = "[green]enabled[/]" if wt_ok else "[dim]disabled[/] (Windows only)"
    console.print(f"  Windows Toast: {wt_str}")

    console.print()
    on_success = "Yes" if config.notifications.notify_on_success else "No"
    on_failure = "Yes" if config.notifications.notify_on_failure else "No"
    console.print(f"  Notify on success: {on_success}")
    console.print(f"  Notify on failure: {on_failure}")
    console.print()
