# pfSentinel - Roadmap

pfSentinel reached **1.0** and is now in **maintenance mode**.
Maintenance mode means a slower pace focused on the Planned list, while security fixes, dependency
updates and pfSense compatibility fixes keep landing (see the end of this file).

Last updated: 2026-12-04

---

## Shipped (v0.2.0)

- CLI (`pfs`) with backup, device, config, schedule, notify and update commands
- SSH (SFTP) and HTTPS backup methods, SSH key authentication
- Extended backups: RRD, packages, DHCP, aliases, certificates, logs
- ZFS snapshot backups with incremental send; filesystem archives (tar.gz)
- Change detection across config sections, SHA-256 checksums, gzip, per-type retention
- Hardened XML parsing (XXE and entity attacks rejected), with property-based fuzzing
- Credentials in the OS keystore, or an AES-256-GCM encrypted file store on headless systems
- Persistent scheduling: Windows Task Scheduler, systemd user timers, cron fallback
- Notifications: Telegram, Slack, Windows toast
- Self-update from GitHub Releases; PyInstaller binaries for Windows and Linux
- Strict SSH host key checking by default, with `pfs device trust-key`
- Built-in help: `pfs docs` and the online manual, plus a plain-text version for AI assistants
- Supply chain: hash-verified lockfiles, SHA-pinned Actions, 14-day update cooldown,
  CodeQL, Bandit, zizmor, OSV-Scanner, pip-audit, dependency review
- CI pipeline defined with [Dagger](https://dagger.io), so the same checks run locally and in CI

## Shipped (v0.3.0)

From the homelab field test of 2026-11-20:

- Encryption at rest: AES-256-GCM `.enc` files, keystore key with a recovery key, or a passphrase
- One folder per backup run, with a run number per device
- Alerts: risky config changes, a restore check after each backup, and a no-recent-backup check
  at sign-in and daily
- System log on both systems: Windows Event Log with fixed event IDs, syslog/journald on Linux
- Long-term retention: one backup per week for 3 months and per month for a year
- Second copy of the encrypted backups to a folder or an SFTP server

---

## Shipped (v1.0.0)

1.0.0 came out after a release candidate ran 14 days in a row on a homelab PC without a failed
backup (2026-11-20 to 2026-12-04, Windows 11 against pfSense 2.8.1).

**The same on Windows and Linux**

- Desktop notifications on Linux (`notify-send`), next to the Windows toast
- Scheduling without administrator rights on Windows
- Owner-only access on Windows too, for the config folder and each device's backup folder
- One page on what runs when the PC is off, asleep or signed out (docs/platforms.md)
- A CI test that fails when a command group works on one system only

**Self-update hardening**

- The signed build attestation and the SHA-256 are checked before a binary is installed
- The download is staged next to the binary, owner-only, and capped at 200 MB
- Downloads only over https from GitHub hosts, checked again after each redirect
- Revert uses only a backup path that the updater derives itself
- As root, a binary in a folder that is not root-owned is refused

**Alerts and destinations**

- Email (SMTP) alerts, a daily digest, and one "backup OK" message per run with every file
- Alerts for a backup that failed in part (1008) and a backup folder that cannot lock (1009)
- An alert when firewall rules, users or certificates drop sharply, and an update alert
- Second copy to S3-compatible storage, Azure Blob Storage or WebDAV (experimental)
- The `age` encryption mode, readable with the standard `age` tool

**Operations**

- `pfs backup history`: every run with its result, failed runs included
- A lock per device, so two runs at once cannot lose records of the backup index
- ZFS snapshots of the dataset mounted at `/`; restore and decrypt copy in 1 MiB steps
- `--json` output for `backup list`, `backup history`, `device list` and `schedule status`
- A restore guide (docs/restore.md)
- A container image for `linux/amd64` and `linux/arm64` (experimental)
- The TUI was removed; mypy is required in CI; test coverage is above 80%

**Not done, moved to Planned**

- A Linux-native default backup folder. The default stays `~/Documents/pfSentinel` on both
  systems, because a new default would move the backups of everyone who never set it.
- The Linux CI jobs still run beside the Dagger pipeline, not on it.

---

## Planned (1.x)

Maintenance mode slows new work down. These items are still planned.

- [ ] **Baseline comparison:** mark a backup as "known good" and report every change against it
- [ ] **Slack Block Kit cards** for richer Slack notifications
- [ ] **A read-only web UI** (not soon)
- [ ] **A Linux-native default backup folder**, with a migration for existing installs
- [ ] **ZFS snapshots of the child datasets** (`/var`, `/cf`, ...), not only the root dataset
- [ ] Move the Linux CI jobs onto the Dagger pipeline

---

## Not planned

- OPNsense support (different config paths and web UI; effectively a second product)
- The pfSense REST API as a third connection method (dropped 2026-11-20)
- Windows installer, AppImage
- Code signing for the binaries (paid certificate)

**Want something on this list, or something that is not on the roadmap at all?** Open an issue at
https://github.com/NX1X/pfSentinel/issues and describe your use case. Requests from real users are
considered positively, including items listed as not planned, and well-scoped pull requests are welcome.

---

## Maintenance mode

What keeps happening:

- **Security fixes** in pfSentinel code, released as patch versions
- **Dependency updates** via Renovate: minor and patch automerge after a 14-day cooldown;
  security fixes after 3 days; majors reviewed by hand
- **pfSense compatibility** fixes when a new pfSense release changes paths or the web UI
- CI stays green on Windows and Linux, Python 3.13 and 3.14. A new Python minor is added when
  lxml, cryptography and paramiko publish wheels for it

What stops: new features beyond the Planned list above, unless users ask for them (see Not planned).
