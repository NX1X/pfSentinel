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

## ZFS Snapshot Backups

pfSense 2.5+ (CE) and pfSense Plus use ZFS by default. pfSentinel can create ZFS snapshots on the device and stream them to local storage.

### How It Works

1. **Detect ZFS**: Runs `zfs list` to verify ZFS is available
2. **Create Snapshot**: `zfs snapshot {dataset}@pfsentinel-{timestamp}`
3. **Stream**: `zfs send [snapshot] | gzip` piped over SSH to local file
4. **Incremental**: If a previous snapshot exists, uses `zfs send -i` for delta transfer
5. **Cleanup**: Removes old remote snapshots (configurable retention)

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
      "dataset": "zroot/ROOT",
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
| `dataset` | `"zroot/ROOT"` | ZFS dataset to snapshot |
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
