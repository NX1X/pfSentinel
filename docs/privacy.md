# Privacy and network connections

pfSentinel has no telemetry. It sends no usage data, no crash reports and no
statistics to the maintainer or to anyone else. It has no accounts, and it
has no server of its own that it contacts.

Everything pfSentinel keeps stays on your PC:

- `config.json`, the run history and the backup index in `~/.pfsentinel` and
  in the backup folder.
- The backups themselves, in the backup folder you choose
  (`~/Documents/pfSentinel` by default).
- Passwords, tokens and keys in the OS keystore (Windows Credential Manager,
  or the Secret Service keyring on Linux).

## Every network connection

pfSentinel connects only to these places.

| Where to | When | Why |
|---|---|---|
| Your pfSense firewalls (SSH, or the web UI over HTTPS or HTTP) | When you add, test or back up a device, by hand or on schedule | To read the firewall config and the extras you chose |
| Your second copy target (SFTP, S3-compatible storage, Azure Blob or WebDAV) | Only if you set up a second copy, after each backup | To upload an encrypted copy of each backup |
| `api.telegram.org` | Only if you turn on Telegram alerts | To send the alert to your chat |
| `hooks.slack.com` | Only if you turn on Slack alerts | To send the alert to your channel |
| Your mail server (SMTP) | Only if you turn on email alerts | To send the alert by email |
| `api.github.com` | Once a day from the daily check task, unless you turn it off (see below) | To see if a new pfSentinel release is out |
| `api.github.com`, `github.com` and GitHub's download hosts | Only when you run `pfs update check` or `pfs update install` | To check for, download and verify a new release |
| PyPI (`pypi.org`) | Only when you run `pfs update install` on a pip or pipx install | pip or pipx downloads the new version |

Windows notifications, the Windows Event Log, Linux desktop notifications and
syslog/journald are local. They make no network connection.

## The daily release check

The daily check task (`pfs backup check-recent`) asks the GitHub API once a
day for the latest release. If a newer release is out, it alerts you once
for that release. The request goes to
`https://api.github.com/repos/NX1X/pfSentinel/releases/latest` with the
User-Agent `pfSentinel-updater`. It sends nothing about you, your firewalls
or your backups. GitHub sees your IP address, as with any web request, under
the [GitHub privacy statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).

Turn it off:

```bash
pfs config set update-check off
```

Turn it on again with `pfs config set update-check on`. When it is off,
pfSentinel contacts GitHub only when you run `pfs update check` or
`pfs update install`. `pfs notify status` shows the current setting.

In 1.0.0-rc.5 and earlier, the command does not exist. Set `"update_alerts": false` in
the `notifications` section of `config.json` instead (`pfs config path`
shows where the file is).

## Questions

Open an issue at <https://github.com/NX1X/pfSentinel/issues>. Report a
security problem as described in [SECURITY.md](../SECURITY.md).
