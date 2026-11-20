# Usage Guide

This guide covers the `pfs` CLI commands, configuration, scheduling, notifications, and troubleshooting.

For installation instructions, see [Installation](installation.md).
For extended backup types (RRD, ZFS, archives, etc.), see [Extended Backups](extended-backups.md).
The full online manual with recipes is at https://pfs.nx1xlab.dev/docs (or run `pfs docs`).

---

## Table of Contents

1. [Backup Operations](#1-backup-operations)
2. [Device Management](#2-device-management)
3. [SSH Key Authentication](#3-ssh-key-authentication)
4. [Configuration](#4-configuration)
5. [Scheduling](#5-scheduling)
6. [Notifications](#6-notifications)
7. [Self-Update](#7-self-update)
8. [Backup Storage](#8-backup-storage)
9. [Change Detection](#9-change-detection)
10. [Troubleshooting](#10-troubleshooting)
11. [CLI Reference](#11-cli-reference)

---

## 1. Backup Operations

### Run a Backup

```bash
# Back up all enabled devices
pfs backup run

# Back up a specific device
pfs backup run -d home-fw
```

When run without `--include` or `--all-extras`, an interactive selector lets you choose which backup types to include.

### Extended Backups

```bash
# Config + all extra targets (RRD, packages, DHCP, certs, logs)
pfs backup run --all-extras

# Config + specific extras
pfs backup run --include rrd,pkg,certs

# Config only (skip all extras)
pfs backup run --config-only
```

See [Extended Backups](extended-backups.md) for details on each target type.

### ZFS Snapshots

```bash
# Incremental ZFS snapshot
pfs backup snapshot -d home-fw

# Force a full snapshot
pfs backup snapshot -d home-fw --full

# List ZFS snapshots on the device
pfs backup snapshot-list -d home-fw
```

### Filesystem Archives

```bash
# Archive default directories
pfs backup archive -d home-fw

# Archive specific directories
pfs backup archive -d home-fw --dirs /cf/conf,/usr/local/etc
```

### List Backups

```bash
pfs backup list
pfs backup list -d home-fw
pfs list                        # shortcut
pfs list -d home-fw
```

### Verify Integrity

```bash
pfs backup verify home-fw_2026-04-30_020000_#001_initial.xml.gz
```

Re-computes the SHA-256 checksum and compares it to the stored value.

### Compare Two Backups

```bash
pfs backup diff FILE_A FILE_B
```

Shows a unified diff of the XML configuration, highlighting changed sections.

### Restore a Backup

```bash
pfs backup restore FILENAME --target ~/Desktop/config-restored.xml
```

Extracts the backup to a plain XML file. To restore to pfSense, upload via **Diagnostics > Backup & Restore** in the pfSense web UI.

### Search Backups

```bash
pfs backup search --changes firewall
pfs backup search --date 2026-04-30
pfs backup search -d home-fw --changes interfaces
```

### Watch for Changes

```bash
# Poll every 5 minutes (default), auto-backup on change
pfs backup watch -d home-fw

# Custom interval
pfs backup watch -d home-fw --interval 120
```

Press `Ctrl+C` to stop.

### Delete a Backup

```bash
pfs backup delete FILENAME --yes
```

### Backup Info

```bash
pfs backup info FILENAME
```

Shows metadata: timestamp, checksum, size, change categories, and verification status.

---

## 2. Device Management

### Add a Device

```bash
pfs device add
```

Interactive wizard that prompts for:

| Field | Example | Notes |
|-------|---------|-------|
| Device ID | `home-fw` | Lowercase letters, numbers, hyphens |
| Label | `Home Firewall` | Human-readable name |
| Host | `192.168.1.1` | IP or hostname |
| Connection method | `SSH` | SSH (recommended), HTTPS, or HTTP |
| SSH port | `22` | Default 22 |
| Username | `admin` | pfSense admin user |
| Password | *(hidden)* | Stored in OS keyring |

### Trust the SSH Host Key

New SSH devices use strict host key checking: pfSentinel only connects to a pfSense box whose SSH key you have confirmed once. `pfs device add` asks at the end; to do it later, or again after reinstalling pfSense:

```bash
pfs device trust-key home-fw        # shows the SHA256 fingerprint and asks
pfs device trust-key home-fw --yes  # no prompt (check the fingerprint yourself)
```

Compare the fingerprint with the pfSense console (option 8, Shell): `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. Trusted keys are stored in `~/.pfsentinel/known_hosts`, and for a trusted device they override any entry in `~/.ssh/known_hosts`. If the key later changes, backups stop with a "host key CHANGED" error instead of silently connecting: that is either a reinstall (re-run `trust-key`) or someone intercepting the connection.

Devices added with older versions keep permissive checking until you run `trust-key` once, which also switches them to strict.

### List Devices

```bash
pfs device list
```

### Test Connection

```bash
pfs device test              # test all devices
pfs device test -d home-fw   # test one device
```

### Edit a Device

```bash
pfs device edit home-fw
```

Allows changing the label, host, connection method, SSL verification, and SSH key.

### Remove a Device

```bash
pfs device remove home-fw --yes
```

Removes the device from config. Existing backup files are not deleted.

---

## 3. SSH Key Authentication

SSH key auth is more secure than passwords and avoids storing a password in the keyring.

### Setup

1. Generate a key pair (if you don't have one):

   ```bash
   ssh-keygen -t ed25519 -C "pfsentinel"
   ```

2. Copy the public key to pfSense:

   ```bash
   ssh-copy-id -i ~/.ssh/id_ed25519.pub admin@192.168.1.1
   ```

   Or in the pfSense web UI: **System > User Manager** > your user > **Authorized SSH Keys**, paste the public key.

3. When running `pfs device add`, enter the private key path when prompted:

   ```
   SSH private key path (optional): ~/.ssh/id_ed25519
   ```

   If the key has a passphrase, it will be stored in the OS keyring.

### Changing the Key on an Existing Device

```bash
pfs device edit home-fw
```

Follow the prompts to update the SSH key path.

---

## 4. Configuration

Config is stored at `~/.pfsentinel/config.json`. Passwords and tokens are stored in the OS keyring, never in the config file.

### View Config

```bash
pfs config show     # print config (passwords redacted)
pfs config path     # print the config file path
```

### Initialize Config

```bash
pfs config init     # create default config
```

### Validate Config

```bash
pfs config validate
```

### Change Backup Directory

```bash
pfs config set backup-dir /path/to/backups
```

### Key Config Fields

```json
{
  "devices": [...],
  "backup_policy": {
    "backup_root": "~/Documents/pfSentinel",
    "max_backups_per_device": 30,
    "compress": true,
    "validate_after_backup": true,
    "keep_days": 30,
    "extras": {
      "rrd": false,
      "package_configs": false,
      "dhcp_leases": false,
      "certificates": false,
      "logs": false
    },
    "max_backups_per_type": {
      "config": 30, "rrd": 10, "pkg": 10,
      "dhcp": 10, "certs": 10, "logs": 7,
      "zfs": 5, "archive": 5
    }
  },
  "schedule": { "enabled": false, "daily_time": "02:00" },
  "notifications": { "telegram_enabled": false, "slack_enabled": false }
}
```

---

## 5. Scheduling

### Enable Scheduled Backups

```bash
pfs schedule enable --daily-time 02:00
```

`pfs schedule enable` hands the schedule to your operating system, so backups keep running after the command exits and after reboots.

**Windows:** registers `pfSentinel\DailyBackup` (and `WeeklyBackup`) in Task Scheduler. Run it from an Administrator PowerShell. Tasks run while you are signed in (a locked screen counts). A run missed while you were signed out or the machine was off starts at your next sign-in.

It also registers `pfSentinel\BackupCheck`, which runs `pfs backup check-recent` 5 minutes after each sign-in and daily at 12:00 (see [No recent backup](#no-recent-backup)), and the Windows Event Log source `pfSentinel`.

To check a task, use `schtasks` (on some PCs `Get-ScheduledTask` fails with "The system cannot find the file specified"):

```powershell
schtasks /Query /TN "pfSentinel\DailyBackup" /V /FO LIST
```

**Linux:** installs systemd user timers (`pfsentinel-daily.timer`, `pfsentinel-weekly.timer`, and `pfsentinel-check.timer` for the no-recent-backup check). A run missed while the machine was off starts at the next boot. By default, user timers only fire while you are logged in. To back up while logged out (servers, headless boxes), enable lingering once:

```bash
loginctl enable-linger $USER
```

Logs: `journalctl --user -u pfsentinel-backup.service`

**No systemd (WSL without systemd, containers, macOS):** falls back to cron. pfSentinel adds its own tagged lines to your crontab and leaves the rest alone. Logs go to `~/.pfsentinel/logs/scheduled.log`.

**Credentials and unattended runs:** on a Linux desktop, passwords live in your desktop keyring (GNOME Keyring or KWallet), which stays locked until you log in. A backup that runs before login cannot read them. On headless systems pfSentinel uses its encrypted file store, which works unattended.

To run the scheduler only inside the current process instead (for example inside a container whose main process is pfSentinel), pass `--no-task-scheduler`.

### Disable

```bash
pfs schedule disable
```

### Check Status

```bash
pfs schedule status
```

---

## 6. Notifications

### Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram and get the bot token.
2. Start a conversation with your bot (send any message).
3. Run the setup wizard:

   ```bash
   pfs notify telegram setup
   ```

   This auto-detects your Chat ID and stores the token in the OS keyring.

4. Test:

   ```bash
   pfs notify test
   ```

### Slack

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps).
2. Enable **Incoming Webhooks** and add one to your channel.
3. Run the setup:

   ```bash
   pfs notify slack setup
   ```

4. Test:

   ```bash
   pfs notify test
   ```

### Windows Event Log (syslog on Linux)

```bash
pfs notify eventlog enable
```

Writes every alert to the Application log with source `pfSentinel` (Linux: syslog/journald, tag `pfsentinel`). On Windows, registering the source needs an Administrator PowerShell once; `pfs schedule enable` does that too. Each kind of alert has a fixed event ID, so you can filter or forward them:

| Event ID | Level | Alert |
|----------|-------|-------|
| 1000 | Information | Backup OK (only with `notify_on_success`) |
| 1001 | Error | Backup failed |
| 1002 | Warning | Risky change |
| 1003 | Error | No recent backup |
| 1004 | Error | Restore check failed |
| 1005 | Error | Second copy failed |

### Alerts

Every alert goes to every enabled channel (Slack, Telegram, Windows toast, Event Log). `notify_on_failure` turns all of them on or off; `notify_on_success` controls only the "backup OK" message.

**Risky change.** After each config backup, pfSentinel compares the config with the previous one and sends one alert per security-relevant change:

- a new admin user, a user or group that gained admin rights, or a changed admin user (for example a new password)
- a changed GUI authentication server (for example a switch to RADIUS)
- a new or changed WAN pass rule, or a new or changed port forward
- SSH login with a password alone turned on
- the anti-lockout rule turned on or off
- a changed web GUI protocol, port or certificate

Alerts name the setting, user or rule only; never a password hash, key or other secret. The changes are also stored with the backup (`pfs backup info FILE`).

**Restore check.** After each config backup, pfSentinel checks that config.xml parses and has users, an admin user, interfaces and a firewall filter section, and counts the CAs and certificates. A failed check is an alert; the backup is still kept. Run it on any backup with `pfs backup check [FILE]`.

<a id="no-recent-backup"></a>**No recent backup.** `pfs backup check-recent` alerts when a device has had no successful backup for `notifications.stale_after_days` days (default 3). A PC that is off, or a schedule that stopped running, gives no error on its own; this check catches it. `pfs schedule enable` runs it at sign-in and daily.

### Check Status

```bash
pfs notify status
```

---

## 7. Self-Update

Check for new releases and update the binary:

```bash
pfs update
```

This checks GitHub Releases for a newer version and downloads it if available.

---

## 8. Backup Storage

### Directory Layout

Since 0.3.0, each backup run has one folder. The config and every extra of that run are in it:

```
~/Documents/pfSentinel/
└── home-fw/
    ├── backup_index.json
    ├── zfs_snapshots.json
    └── 2026/11/20/
        ├── 020000_#141/                         # run 141
        │   ├── home-fw_2026-11-20_020000_#141_minor.xml.gz
        │   ├── home-fw_2026-11-20_020000_#141_rrd.tar.gz
        │   └── home-fw_2026-11-20_020000_#141_dhcp.txt.gz
        └── 085016_#142/                         # run 142
            └── home-fw_2026-11-20_085016_#142_firewall.xml.gz
```

The folder name is `<HHMMSS>_#<run number>`. The run number counts the device's runs and never resets, so "run #142" names one run.

Backups made before 0.3.0 keep their old paths: config files in `<device>/YYYY/MM/DD/`, and each extra in `<device>/<type>/YYYY/MM/DD/`. `backup list`, `verify`, `diff`, `restore` and retention still work on them. New run numbers continue after the old runs.

### Filename Format

```
{device-id}_{YYYY-MM-DD}_{HHMMSS}_#{RUN}_{changes}.{ext}
```

Example:

```
home-fw_2026-11-20_085016_#142_interfaces+system.xml.gz
```

| Component | Meaning |
|-----------|---------|
| `home-fw` | Device ID |
| `2026-11-20` | Backup date |
| `085016` | Backup time (24h) |
| `#142` | Run number (before 0.3.0: daily sequence number) |
| `interfaces+system` | Changed config sections |
| `.xml.gz` | Compressed XML config |

### Retention

Retention is enforced automatically after each backup. Configurable per backup type via `max_backups_per_type` and globally via `keep_days` in the config file.

**Long-term retention (since 0.3.0).** Besides those limits, pfSentinel keeps the newest config backup of every week for 12 weeks (about 3 months) and of every month for 12 months. These keepers do not count toward `max_backups_per_type` and do not expire with `keep_days`. Settings in `backup_policy.long_term`:

```json
{
  "long_term": {
    "enabled": true,
    "weekly_weeks": 12,
    "monthly_months": 12,
    "types": ["config"]
  }
}
```

With daily backups and the defaults (30 per type, `keep_days` 30), this keeps about 48 config backups: the last 30 days, 8 weekly and 10 monthly ones. Only `config` is listed by default; the extras of an old run are removed by their own limits, and its folder keeps the config.

### Second Copy

Keep a second copy of the encrypted backups on a target you run yourself. Turn on encryption first: only encrypted files leave the PC.

```bash
pfs copy setup     # folder or SFTP
pfs copy now       # copy the existing encrypted backups
pfs copy status
pfs copy disable   # copies already made stay on the target
```

- **folder:** a local path, for example a network share or a folder that the Nextcloud desktop client syncs.
- **sftp:** an SSH server with key login. Setup shows the server's host key fingerprint, and pfSentinel then refuses any other key, as it does for pfSense.

After each backup run, pfSentinel copies every encrypted backup that the target does not have yet, into the same `<device>/YYYY/MM/DD/<run>/` folders. A failed copy never fails the backup: it is an alert (event ID 1005), and the next run copies what is missing. On the target, day folders older than `keep_days` (default 90) are removed. Backups from before 0.3.0 are not copied.

---

## 9. Change Detection

Each backup is compared to the previous one. pfSentinel parses the XML and diffs each major section, and puts the changed sections in the file name. Every run saves a config backup, also when nothing changed (then the label is `minor`). Only `pfs backup watch` waits for a change before it backs up.

| Category | Triggered When |
|----------|---------------|
| `initial` | First backup for this device |
| `interfaces` | WAN/LAN/OPT interface changes |
| `firewall` | Firewall rules or aliases changed |
| `system` | Hostname, DNS, system settings |
| `users` | User accounts changed |
| `packages` | Installed packages changed |
| `dhcp` | DHCP configuration changed |
| `vpn` | OpenVPN / IPsec configuration |
| `routes` | Static routes changed |
| `minor` | No change in the tracked sections (or a change outside them) |

Multiple categories are joined with `+` in the filename. All categories are stored in `backup_index.json`.

---

## 10. Troubleshooting

**Windows: scheduled task fails with `0x80070002`.** The task points at a pfs executable that is no longer there (moved, uninstalled, or registered by an older pfSentinel version). Re-run `pfs schedule enable` from an Administrator PowerShell to re-register it against the current executable, then check `pfs schedule status`.


### SSH connection refused

Verify SSH is enabled on pfSense: **System > Advanced > Admin Access > Enable Secure Shell**.

Check host and port: `pfs device list`, then `pfs device test -d home-fw`.

### SSL certificate errors (HTTPS)

pfSense uses a self-signed certificate by default. Set `verify_ssl` to `false` for the device:

```bash
pfs device edit home-fw
```

SSH is recommended over HTTPS to avoid this entirely.

### Keyring errors on WSL / headless Linux

If you see `keyring.errors.NoKeyringError`, the `keyrings.alt` package provides a file-based fallback. pfSentinel detects this automatically. If it still fails:

```bash
export PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
pfs device add
```

Secure the file: `chmod 600 ~/.local/share/python_keyring/keyring_pass.cfg`

### Enable debug logging

```bash
pfs --debug backup run
```

Logs are written to `~/.pfsentinel/logs/pfsentinel.log`.

---

## 11. CLI Reference

### Global Options

| Flag | Description |
|------|-------------|
| `--version` | Show version and exit |
| `--help` | Show help |

### `pfs backup`

| Command | Description |
|---------|-------------|
| `backup run` | Backup all devices |
| `backup run -d ID` | Backup one device |
| `backup run --all-extras` | Include all extended targets |
| `backup run --include rrd,pkg,...` | Include specific extras |
| `backup run --config-only` | Config only, skip extras |
| `backup run --non-interactive` | Never prompt; use the backup policy from config (what scheduled runs use) |
| `backup list` | List all backups |
| `backup list -d ID` | List backups for one device |
| `backup verify FILENAME` | Verify SHA-256 checksum |
| `backup diff FILE_A FILE_B` | Diff two backup configs |
| `backup restore FILE --target PATH` | Extract backup to file |
| `backup delete FILE --yes` | Delete a backup |
| `backup info FILE` | Show backup metadata |
| `backup search --changes TYPE` | Search by change category |
| `backup search --date YYYY-MM-DD` | Search by date |
| `backup watch -d ID` | Auto-backup on change |
| `backup snapshot -d ID` | ZFS incremental snapshot |
| `backup snapshot -d ID --full` | ZFS full snapshot |
| `backup snapshot-list -d ID` | List ZFS snapshots |
| `backup archive -d ID` | Filesystem tar archive |
| `backup check [FILE]` | Restore check of a config backup (default: the latest) |
| `backup check-recent` | Alert when a device has no recent backup |
| `backup decrypt FILE` | Decrypt one `.enc` file (`--recovery-key`, `--passphrase`) |

### `pfs device`

| Command | Description |
|---------|-------------|
| `device add` | Interactive device setup |
| `device list` | List all devices |
| `device test` | Test all connections |
| `device test -d ID` | Test one connection |
| `device edit ID` | Edit device settings |
| `device trust-key ID` | Trust (or re-trust) the device's SSH host key |
| `device remove ID --yes` | Remove a device |

### `pfs config`

| Command | Description |
|---------|-------------|
| `config show` | Print config (redacted) |
| `config init` | Create default config |
| `config validate` | Validate config file |
| `config path` | Print config file path |
| `config set backup-dir PATH` | Change backup directory |

### `pfs schedule`

| Command | Description |
|---------|-------------|
| `schedule enable --daily-time HH:MM` | Enable daily backups |
| `schedule disable` | Disable scheduled backups |
| `schedule status` | Show schedule status |

### `pfs notify`

| Command | Description |
|---------|-------------|
| `notify telegram setup` | Configure Telegram bot |
| `notify slack setup` | Configure Slack webhook |
| `notify eventlog enable` | Write alerts to the Windows Event Log (syslog on Linux) |
| `notify test` | Send test notification |
| `notify status` | Show notification status |

### `pfs copy`

| Command | Description |
|---------|-------------|
| `copy setup` | Set up the second copy target (folder or SFTP) |
| `copy now [-d ID]` | Copy every encrypted backup the target does not have yet |
| `copy status` | Show the target |
| `copy disable` | Stop copying |

### `pfs encryption`

| Command | Description |
|---------|-------------|
| `encryption setup` | Turn on encryption at rest (keystore or passphrase) |
| `encryption status` | Mode, key ID, encrypted and plain backup counts |
| `encryption recovery-key` | Show the recovery key again (keystore mode) |
| `encryption encrypt-existing [--wipe]` | Encrypt older plain backups |
| `encryption disable` | Write new backups plain again |

### `pfs update`

| Command | Description |
|---------|-------------|
| `update` | Check for updates and install |

### Top-Level Commands

| Command | Description |
|---------|-------------|
| `pfs setup` | First-time setup wizard |
| `pfs status` | Overview of devices and backups |
| `pfs list` | Shortcut for `backup list` |
| `pfs docs` | Open the online manual (`--no-browser` to only print the link) |
