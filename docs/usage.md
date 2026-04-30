# Usage Guide

This guide covers the `pfs` CLI commands, configuration, scheduling, notifications, and troubleshooting.

For installation instructions, see [Installation](installation.md).
For extended backup types (RRD, ZFS, archives, etc.), see [Extended Backups](extended-backups.md).

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

**Windows:** Creates a Windows Task Scheduler task that runs automatically, even after reboots.

**Linux/macOS:** Uses an in-process scheduler. For persistent scheduling, use cron:

```cron
0 2 * * * /usr/local/bin/pfs backup run >> ~/.pfsentinel/cron.log 2>&1
```

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

```
~/Documents/pfSentinel/
└── home-fw/
    ├── backup_index.json
    ├── zfs_snapshots.json
    ├── 2026/04/30/                    # config backups
    │   └── home-fw_..._initial.xml.gz
    ├── rrd/2026/04/30/                # RRD data
    ├── pkg/2026/04/30/                # package configs
    ├── dhcp/2026/04/30/               # DHCP leases
    ├── certs/2026/04/30/              # certificates
    ├── logs/2026/04/30/               # system logs
    ├── zfs/2026/04/30/                # ZFS snapshots
    └── archive/2026/04/30/            # filesystem archives
```

### Filename Format

```
{device-id}_{YYYY-MM-DD}_{HHMMSS}_#{SEQ}_{changes}.{ext}
```

Example:

```
home-fw_2026-04-30_143022_#001_interfaces+system.xml.gz
```

| Component | Meaning |
|-----------|---------|
| `home-fw` | Device ID |
| `2026-04-30` | Backup date |
| `143022` | Backup time (24h) |
| `#001` | Daily sequence number |
| `interfaces+system` | Changed config sections |
| `.xml.gz` | Compressed XML config |

### Retention

Retention is enforced automatically after each backup. Configurable per backup type via `max_backups_per_type` and globally via `keep_days` in the config file.

---

## 9. Change Detection

Each backup is compared to the previous one. pfSentinel parses the XML and diffs each major section. A backup is only saved if something changed (or it's the first backup for a device).

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
| `minor` | Small change not matching a major category |

Multiple categories are joined with `+` in the filename. All categories are stored in `backup_index.json`.

---

## 10. Troubleshooting

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

### No changes detected (backup skipped)

This is expected behavior -- pfSentinel only saves when the config changes. To force:

```bash
pfs backup run --force
```

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
| `--debug` | Enable verbose logging |
| `--help` | Show help |

### `pfs backup`

| Command | Description |
|---------|-------------|
| `backup run` | Backup all devices |
| `backup run -d ID` | Backup one device |
| `backup run --all-extras` | Include all extended targets |
| `backup run --include rrd,pkg,...` | Include specific extras |
| `backup run --config-only` | Config only, skip extras |
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

### `pfs device`

| Command | Description |
|---------|-------------|
| `device add` | Interactive device setup |
| `device list` | List all devices |
| `device test` | Test all connections |
| `device test -d ID` | Test one connection |
| `device edit ID` | Edit device settings |
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
| `notify test` | Send test notification |
| `notify status` | Show notification status |

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
