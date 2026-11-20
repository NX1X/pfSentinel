# Alerts: email, daily digest and desktop notifications

pfSentinel sends every alert to each channel that you turn on. This page
covers three channels and one option:

- Email through any SMTP server.
- The daily digest: one summary a day instead of one message per backup.
- Desktop notifications on Linux (Windows has its toast).

For Telegram, Slack and the Windows Event Log, see [usage.md](usage.md).
To see all channels, run `pfs notify status`. To send a test to every
enabled channel, run `pfs notify test`.

## Alert kinds

Every alert has a fixed event ID. The Windows Event Log and syslog use the
ID, and each email ends with a line such as `pfSentinel event 1001
(BACKUP_FAILED)`, so you can filter on it.

| ID   | Kind                 | Sent                                     |
| ---- | -------------------- | ---------------------------------------- |
| 1000 | Backup OK            | After each backup, or only in the digest |
| 1001 | Backup failed        | At once                                  |
| 1002 | Risky change         | At once                                  |
| 1003 | No recent backup     | At once (daily check)                    |
| 1004 | Restore check failed | At once                                  |
| 1005 | Second copy failed   | At once                                  |
| 1006 | Update available     | At once, once per release                |
| 1007 | Daily digest         | Once a day, if the digest is on          |

The daily check also sends 1003 when the backup index of a device cannot
be read. The title then says "Backup index of DEVICE cannot be read". See
[Rebuild the Backup Index](usage.md#rebuild-the-backup-index).

## Email (SMTP)

Email works with any mail server. You need the server name, the port, the
security mode, and a From and To address. A login is optional.

Security modes:

- `starttls` (default): the connection starts plain and switches to TLS.
  This is usually port 587. If the server does not offer STARTTLS,
  pfSentinel sends nothing. It never falls back to plain text.
- `tls`: TLS from the first byte. This is usually port 465.
- `none`: no encryption. Use it only for a local server such as Mailpit.
  A login is refused in this mode, so a password is never sent unencrypted.

### Set up a mail provider

1. Run the setup command with your server and login:

   ```
   pfs notify email setup --host smtp.example.com --username me@example.com --to ops@example.com
   ```

2. Type the password when asked. The input is hidden.
3. Press Enter to use the username as the From address, or type another one.
4. Check that the test email arrives.

The default security mode is `starttls` and the default port is 587. For
port 465, add `--security tls --port 465`.

### Set up a local server without a login (Mailpit)

1. Run:

   ```
   pfs notify email setup --host 192.168.30.21 --port 1025 --security none --from pfsentinel@lab.local --to me@lab.local
   ```

2. Check that the test email arrives in Mailpit.

### Where the password is kept

The SMTP password is in the OS keystore, like the device passwords: the
Windows Credential Manager, a Linux desktop keyring, or the encrypted file
store on a headless machine. It is never written to `config.json`. Error
messages and logs never show it.

### Certificates

pfSentinel always checks the server's TLS certificate and host name. For a
lab server whose certificate comes from your own CA (or is self-signed),
give that CA's certificate file. pfSentinel then trusts it in addition to
the system trust store:

```
pfs notify email setup --host mail.lab.local --ca-cert lab-ca.pem ...
```

For a self-signed server certificate, the certificate itself is the CA
file. Its name must match the host you give with `--host`.

### Other email commands

- `pfs notify email status` shows the settings. It never shows the password.
- `pfs notify email disable` stops email alerts and keeps the settings.
- `pfs notify email enable` turns them on again.
- To change a setting, run `pfs notify email setup` again. Press Enter at
  the password prompt to keep the stored password.

Options of `pfs notify email setup`: `--host`, `--port`, `--security`,
`--username`, `--no-login`, `--from`, `--to` (separate addresses with
commas), `--timeout` (seconds, default 20), `--ca-cert FILE` (your lab CA's certificate), and
`--no-test`. Anything you leave out is asked for.

## Daily digest

The digest is one message a day. For each enabled device it shows:

- The time of the last successful config backup.
- How many backups are stored, and how many were made in the last 24 hours.
- The problems found in the last 24 hours (failed backups, risky changes,
  failed restore checks, no recent backup, failed second copies).

When the digest is on, a successful backup sends no message of its own. A
failed backup, a risky change and every other problem are still sent at
once.

### Turn the digest on

1. Run `pfs notify digest enable --time 08:00`.
2. Make sure that scheduling is on (`pfs schedule status`). The digest is
   sent by the daily check task, which `pfs schedule enable` creates.
3. On Windows, run the command from step 1 in an administrator PowerShell.
   Only then can it add the digest time to the check task.

### When the digest is sent

The daily check task runs at sign-in, at 12:00, and at the digest time. The
first run on a day at or after the digest time sends the digest. Later runs
on the same day do not. The date of the last digest is kept in
`~/.pfsentinel/digest_state.json`.

If the computer is off at the digest time, the digest goes out at the next
run of the check task that day (at sign-in or at 12:00). If every channel
fails, the date is not saved, and the next run tries again.

If you turn on the digest without administrator rights on Windows, the
check task does not run at the digest time. The digest then goes out with
the next check after that time, at sign-in or at 12:00.

### Digest commands

- `pfs notify digest status` shows the time and the date of the last digest.
- `pfs notify digest send --preview` prints today's digest and sends nothing.
- `pfs notify digest send` sends it now. The scheduled digest still goes
  out today.
- `pfs notify digest disable` goes back to one message per backup.

The digest uses the alerts of the last 24 hours. pfSentinel keeps them in
`~/.pfsentinel/alert_history.json` while the digest is on, for at most 7
days. On Linux and macOS only your user can read the file.

## Desktop notifications

On Windows, pfSentinel shows a toast. On Linux, it uses `notify-send`
(freedesktop notifications), which works with GNOME, KDE and most other
desktops. Both are on by default.

On Linux, a notification is shown only if:

- `notify-send` is installed. It is in the package `libnotify-bin` on
  Debian and Ubuntu, and `libnotify` on Fedora and Arch.
- pfSentinel runs in a desktop session. A terminal on your desktop counts.
  The systemd user timer usually counts too while you are logged in, because
  it gets the session bus address. A cron job does not.

On a server without a desktop, nothing is shown and no error is reported.
Failed backups and other errors use the critical urgency, so most desktops
keep them on screen until you close them.

Commands:

- `pfs notify desktop status` shows whether it is on and where `notify-send` is.
- `pfs notify desktop disable` turns desktop notifications off.
- `pfs notify desktop enable` turns them on again.

## Settings in config.json

All new settings are under `notifications`. Older config files load
without changes.

| Setting                 | Default      | Meaning                                    |
| ----------------------- | ------------ | ------------------------------------------ |
| `email_enabled`         | `false`      | Send alerts by email                       |
| `email_host`            | `null`       | SMTP server                                |
| `email_port`            | `587`        | SMTP port                                  |
| `email_security`        | `"starttls"` | `starttls`, `tls` or `none`                |
| `email_username`        | `null`       | Login name; `null` means no login          |
| `email_from`            | `null`       | From address                               |
| `email_to`              | `[]`         | To addresses                               |
| `email_timeout`         | `20`         | Seconds, 1 to 300                          |
| `email_ca_cert`         | `null`       | PEM file of a CA to trust (lab CA); certificates are always checked |
| `digest_enabled`        | `false`      | Send the daily digest                      |
| `digest_time`           | `"08:00"`    | Earliest time of day for the digest        |
| `linux_desktop_enabled` | `true`       | Show alerts with `notify-send` on Linux    |
