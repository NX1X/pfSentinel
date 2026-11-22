# Extended Backup Targets

pfSentinel v0.3.0 expands beyond XML config backups to support a comprehensive set of backup targets. All extended targets require SSH access to the pfSense device.

## Overview

| Target | Type Key | Remote Path | Format | Default |
|--------|----------|-------------|--------|---------|
| XML Config | `config` | `/cf/conf/config.xml` | `.xml[.gz]` | Always |
| RRD Data | `rrd` | `/var/db/rrd/*.rrd` | `.tar[.gz]` | Off |
| Package Configs | `pkg` | `/usr/local/etc/` | `.tar.gz` | Off |
| DHCP Leases | `dhcp` | `/var/dhcpd/var/db/dhcpd.leases` | `.txt[.gz]` | Off |
| Alias Files | `aliases` | URL tables, alias dirs | `.tar[.gz]` | Off |
| Certificates | `certs` | `/etc/ssl/`, `/usr/local/etc/ssl/` | `.tar[.gz]` | Off |
| System Logs | `logs` | `/var/log/filter.log`, etc. | `.tar[.gz]` | Off |
| ZFS Snapshot | `zfs` | ZFS dataset stream | `.zfs.gz` | Off |
| Filesystem Archive | `archive` | Tar of critical dirs | `.tar.gz` | Off |

## Quick Start

```bash
# Back up config + all extra targets
pfs backup run -d home-fw --all-extras

# Back up config + specific extras
pfs backup run -d home-fw --include rrd,pkg,certs

# Config only (legacy behavior)
pfs backup run -d home-fw --config-only

# ZFS snapshot (standalone)
pfs backup snapshot -d home-fw

# Filesystem archive (standalone)
pfs backup archive -d home-fw
```

## Scheduled Backups

A scheduled backup (Task Scheduler, systemd timer or cron) cannot answer the prompt. It backs up the config plus the extras that are turned on in the config file. In a new config, every extra is off, so scheduled backups save the config only. Your answers at the interactive prompt are not saved.

Turn on the extras for scheduled backups:

```bash
pfs config set extras rrd,pkg,dhcp,aliases,certs   # these five
pfs config set extras all                          # all six, logs included
pfs config set extras none                         # config only
```

The command turns on exactly the targets you name and turns the others off. `pfs schedule status` shows what a scheduled backup saves, in the `Backs up` line. ZFS snapshots and filesystem archives have their own settings (see below).

## Extra Backup Targets

### RRD Data

RRD (Round Robin Database) files contain traffic graphs and performance metrics. They are stored at `/var/db/rrd/` on the pfSense device.

- All `*.rrd` files are downloaded via SFTP
- Bundled into a single `.tar[.gz]` archive
- Useful for preserving historical traffic data across reinstalls

### Package Configs

Package configuration files from `/usr/local/etc/` capture settings for installed packages (Suricata, pfBlockerNG, HAProxy, etc.).

- Entire `/usr/local/etc/` directory is streamed as a tar archive via SSH
- Preserves directory structure within the archive

### DHCP Leases

The active DHCP lease table from `/var/dhcpd/var/db/dhcpd.leases`.

- Single file download via SFTP
- Stored as `.txt[.gz]` (optionally compressed)

### Alias Files

URL tables and external alias files used by firewall rules.

- Checks `/usr/local/share/pfSense/aliases/`, `/usr/local/etc/aliases/`, and `/var/db/aliastables/`
- All found files bundled into a tar archive

### Certificates

SSL/TLS certificates and keys from the filesystem.

- Scans `/etc/ssl/` and `/usr/local/etc/ssl/` for `*.pem`, `*.crt`, `*.key`, `*.csr`
- Note: Certificates are also stored in `config.xml` - this target captures filesystem-level copies

### System Logs

Log files from the pfSense device.

- Default log files: `/var/log/filter.log`, `/var/log/system.log`
- Customizable via `extras.log_files` in config
- Downloaded via SFTP and bundled into tar archive

## Non-root SSH user

A dedicated backup user without admin rights is safer than `admin` or `root`. In pfSense, give it a group with only "User - System: Shell account access", and log in with an SSH key.

Some files on pfSense are readable only by root. pfSentinel skips them with one "needs root" line, not an error. The rest of the backup still runs.

Observed on pfSense CE 2.8.1 with a non-root user (field test, 2026-11-20):

| Target | Non-root user |
|--------|---------------|
| `config` | Works. `/cf/conf/config.xml` is `-rw-r--r-- root wheel`. |
| `rrd` | Works. |
| `pkg` | Works, except root-only files such as `sasldb2.db`, which are skipped. |
| `dhcp` | Works. |
| `aliases` | Works. On 2.8.1 the files are in `/var/db/aliastables`. |
| `certs` | Works. |
| `logs` | Needs root. `/var/log/filter.log` and `/var/log/system.log` are root-only, so the target is skipped. |

ZFS snapshots and filesystem archives were not tested with a non-root user. `zfs snapshot` and `zfs send` usually need root, or rights given with `zfs allow`.

## ZFS Snapshot Backups

pfSense 2.5+ (CE) and pfSense Plus use ZFS by default. pfSentinel can create ZFS snapshots on the device and stream them to local storage.

### How It Works

1. **Detect ZFS**: Runs `zfs list` to verify ZFS is available
2. **Find the dataset**: With `dataset` empty (the default), runs `zfs list -H -o name /` and uses the ZFS dataset that is mounted at `/`. On pfSense 2.8.1 this is `pfSense/ROOT/default`. An inactive boot environment is never picked, also when it is mounted somewhere else.
3. **Create Snapshot**: `zfs snapshot {dataset}@pfsentinel-{timestamp}`
4. **Stream**: `zfs send [snapshot]` over SSH, gzipped on this computer into the local file. If `zfs send` fails or sends nothing, there is no backup, and the new snapshot is destroyed on the device.
5. **Incremental**: If the newest local ZFS backup is the previous snapshot, uses `zfs send -i` for delta transfer
6. **Cleanup**: Removes old remote snapshots (configurable retention)

The snapshot is of one dataset only. Child datasets have their own snapshots, so they are not in the file. On pfSense 2.8.1 these include `/cf` (the config), `/var`, `/tmp` and `/home`. The config backup and the extras still cover the config and the files under `/var`.

After a pfSense upgrade, `/` can be on a new boot environment. The next run then makes a full send of the new dataset.

An incremental backup restores only on top of every send before it, back to a full send. Such a full send and its incrementals form a chain. Retention deletes a chain only as a whole, once none of its files is kept. So a chain is never longer than the ZFS limit (`max_backups_per_type.zfs`, default 5): when it reaches the limit, the next run makes a new full send. With the default, up to 10 ZFS files can be stored while the old chain ages out.

### Usage

```bash
# Incremental snapshot (default)
pfs backup snapshot -d home-fw

# Force full snapshot
pfs backup snapshot -d home-fw --full

# List remote snapshots
pfs backup snapshot-list -d home-fw
```

### Configuration

```json
{
  "backup_policy": {
    "zfs": {
      "enabled": true,
      "dataset": "",
      "incremental": true,
      "cleanup_remote": true,
      "max_snapshots_remote": 3
    }
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable ZFS snapshots in orchestrated runs |
| `dataset` | `""` | ZFS dataset to snapshot. Empty: the dataset mounted at `/`. Before 1.0.0 the default was `"zroot/ROOT"`, which pfSense 2.8.1 does not have; pfSentinel now reads that old value as empty. To see the names, run `zfs list -o name,mountpoint` on the device. |
| `incremental` | `true` | Use incremental send when possible |
| `cleanup_remote` | `true` | Delete old snapshots on device after transfer |
| `max_snapshots_remote` | `3` | Max snapshots to keep on the device |

### Snapshot Tracking

ZFS snapshots are tracked in a per-device `zfs_snapshots.json` file alongside `backup_index.json`. This tracks which snapshots have been transferred and enables incremental sends.

## Filesystem Archive Backups

For pfSense systems using UFS or where ZFS snapshots are not suitable, pfSentinel can create tar archives of critical directories.

### How It Works

1. Runs `tar czf - {directories}` on the remote device via SSH
2. Streams the compressed output directly to a local file
3. No temporary files on the remote device

### Usage

```bash
# Archive with default directories
pfs backup archive -d home-fw

# Archive specific directories
pfs backup archive -d home-fw --dirs /cf/conf,/usr/local/etc,/var/db/rrd
```

### Configuration

```json
{
  "backup_policy": {
    "archive": {
      "enabled": false,
      "directories": [
        "/cf/conf",
        "/usr/local/etc",
        "/var/db/rrd",
        "/boot/loader.conf",
        "/boot/loader.conf.local"
      ],
      "exclude_patterns": ["*.core", "*.tmp"]
    }
  }
}
```

## Backup Orchestrator

When using `pfs backup run`, the `BackupOrchestrator` coordinates all backup types in a single operation:

1. **Config backup** (always runs first)
2. **Extra targets** (if `--include`, `--all-extras`, or configured in `extras`)
3. **ZFS snapshot** (if `zfs.enabled` is true)
4. **Filesystem archive** (if `archive.enabled` is true, or as ZFS fallback)

If ZFS snapshot fails and `archive.enabled` is true, the orchestrator automatically falls back to a filesystem archive.

## Per-Type Retention

Retention limits are applied independently per backup type. Configure in `backup_policy.max_backups_per_type`:

```json
{
  "max_backups_per_type": {
    "config": 30,
    "rrd": 10,
    "pkg": 10,
    "dhcp": 10,
    "aliases": 10,
    "certs": 10,
    "logs": 7,
    "zfs": 5,
    "archive": 5
  }
}
```

The `keep_days` setting applies globally across all types.

Long-term keepers (one per week and one per month) are on top of these limits; see `backup_policy.long_term` in the [Usage Guide](usage.md#retention).

## Backup Verification

`pfs backup verify` uses type-appropriate validation:

| Type | Verification |
|------|-------------|
| `config` | SHA-256 checksum + XML structure validation |
| `rrd`, `pkg`, `certs`, `logs`, `aliases`, `archive` | SHA-256 checksum + tar archive integrity |
| `zfs` | SHA-256 checksum only |
| `dhcp` | SHA-256 checksum only |

## Migration from v0.2.x

Existing backup indexes are automatically migrated when loaded:

- `backup_index.json` schema version is bumped from 1 to 2
- All existing records get `backup_type: "config"` added
- No manual migration steps required
- Existing backup files are not moved or renamed
