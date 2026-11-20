# pfSentinel - Roadmap

Goal: get pfSentinel to a solid **1.0**, then move it to **maintenance mode**.
Maintenance mode means a slower pace focused on the Planned list, while security fixes, dependency
updates and pfSense compatibility fixes keep landing (see the end of this file).

Last updated: 2026-11-20

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

## Path to 1.0 (decided 2026-11-20)

Four steps, each ending with a real-world test on a homelab:

- **0.4.0 Hardening:** self-update hardening, Windows file permissions, an alert when the number of
  firewall rules, users or certificates drops sharply, an update-available alert, a binary smoke test
  in CI, the TUI removed and mypy required in CI.
- **0.5.0 Alerts and destinations:** email (SMTP) alerts for any mail server, a daily digest, Linux
  desktop notifications, an S3-compatible second copy (experimental; encrypted backups only), and an
  `age` encryption mode readable with the standard `age` tool.
- **0.6.0 Polish:** scheduling without admin on Windows, `--json` output, 80% test coverage, and the
  remaining docs below.
- **1.0.0:** a release candidate that runs 14 days in a row without a failed backup, then 1.0.0.

The lists below hold the details.

## Before 1.0 (required)

### Full cross-platform parity (Windows and Linux)

1.0 means every feature works the same way on Windows and Linux, and CI proves it on both.

- [ ] **Desktop notifications on Linux.** Windows has toasts; Linux gets freedesktop notifications
      (the same mechanism `notify-send` uses), with the same on/off setting
- [x] **System log on both** (0.3.0): Windows Event Log on Windows, syslog/journald on Linux. The
      setting keeps its first name, `windows_event_log_enabled`, so existing configs work
- [ ] **Linux-native backup directory.** The default is `~/Documents/pfSentinel` on both systems.
      Linux should use `~/pfSentinel` (or an XDG path), with a migration for anyone who never set
      `backup_policy.backup_root`, so existing backups are not orphaned
- [ ] **File permissions on Windows.** On Linux, config, backups and the secret store are `0600`/`0700`.
      On Windows `chmod` does nothing, so set explicit owner-only ACLs as well
- [ ] **Scheduling without admin on Windows.** Linux needs no root; Windows currently needs an elevated shell
      because tasks live in a `\pfSentinel` folder. Register in a location a normal user can write
- [ ] **One scheduling story in the docs:** what runs when you are logged out, asleep or powered off,
      on each OS, side by side
- [ ] **Parity check in CI:** a test that fails if a user-facing feature is gated to one OS without a
      counterpart on the other

### Self-update hardening

From the 2026-11 security review. The updater already checks SHA-256 against the release checksums, but:

- [ ] **Verify the signed build provenance** before installing. Releases are attested from 0.2.5;
      the updater should check that attestation, not only a checksum fetched from the same release
- [ ] **Stage the download next to the binary** (`mkstemp` in the install folder, owner-only, hash
      while streaming), so there is no window between the checksum and the replace. This also fixes
      updates failing when `~/.pfsentinel` and the install folder are on different file systems
- [ ] **Revert only to a backup path the updater derives itself**, not one read from the state file
- [ ] **Refuse a binary update run as root** unless the staging folder is root-owned
- [ ] Cap the download size, and accept download URLs only on `https` GitHub hosts

### Verification

These features exist and are unit tested, but have never been observed working for real:

- [x] **Windows scheduled backup, end to end.** Observed 2026-11-20 on Windows 11 (v0.2.6): the
      registered task ran and reported `Last result: OK`. Not yet observed: a key passphrase read from
      Credential Manager by a scheduled run
- [ ] **Linux systemd timer, end to end.** `pfs schedule enable`, then
      `systemctl --user start pfsentinel-backup.service` and check `journalctl --user -u pfsentinel-backup`
- [ ] **Real pfSense smoke test.** SSH and HTTPS backup against current pfSense CE and pfSense Plus
      (the e2e suite only covers fake servers)
- [ ] **Windows toast** shows on a real desktop (new built-in implementation)

### Clean-up

- [x] **TUI: remove it, or ship it.** Removed in 0.4.0. `src/pfsentinel/tui/` is not reachable from `pfs`, and it imports
      `textual`, which is not a declared dependency. Recommendation: remove it
- [x] **mypy clean and gated in CI** (0.4.0: no errors; part of the required lint job)
- [x] **Test coverage 67% -> 80%**: 92% with the CLI tests added before 1.0; CI fails below 80%
- [ ] **Document restore.** A written "how to restore this file to pfSense" page
- [ ] **Move the Linux CI jobs onto the Dagger pipeline** once it has run green for a while

### Release hygiene

- [x] `SECURITY.md` states which versions get fixes (latest minor only)
- [ ] README states the project status (1.0, maintenance mode) and what that means for users

---

## Optional before 1.0

Small features worth doing only if there is time. Each one is self-contained.

- [ ] `--json` output for `device list` and `schedule status` (`backup list` has it)
- [ ] Richer Telegram messages (size, pfSense version, hostname)
- [ ] Email (SMTP) notifications for any mail server (planned for 0.5.0)
- [ ] Daily digest: one message a day for all devices (planned for 0.5.0)
- [ ] S3-compatible second copy, experimental (planned for 0.5.0)
- [ ] `age` encryption mode (planned for 0.5.0)
- [ ] Alert when rules, users or certificates drop sharply between backups (planned for 0.4.0)

---

## Planned (1.x)

Bigger features that are planned after 1.0. Maintenance mode pauses new work, not these.

- [ ] **pfSense REST API backend** (pfSense Plus 23.09+ and the REST API package), as a third
      connection method next to SSH and HTTPS
- [ ] **More backup destinations:** a folder and SFTP shipped in 0.3.0 (`pfs copy`), S3 is planned for
      0.5.0; WebDAV (Nextcloud without the desktop client) remains
- [ ] **Baseline comparison:** mark a backup as "known good" and report every change against it
- [ ] **Slack Block Kit cards** for richer Slack notifications

---

## Not planned

- OPNsense support (different config paths and web UI; effectively a second product)
- Web UI, Docker image, Windows installer, AppImage
- Code signing for the binaries (paid certificate)

**Want something on this list, or something that is not on the roadmap at all?** Open an issue at
https://github.com/NX1X/pfSentinel/issues and describe your use case. Requests from real users are
considered positively, including items listed as not planned, and well-scoped pull requests are welcome.

---

## Maintenance mode (after 1.0)

What keeps happening:

- **Security fixes** in pfSentinel code, released as patch versions
- **Dependency updates** via Renovate: minor and patch automerge after a 14-day cooldown;
  security fixes after 3 days; majors reviewed by hand
- **pfSense compatibility** fixes when a new pfSense release changes paths or the web UI
- CI stays green on Windows and Linux, Python 3.13 and 3.14. A new Python minor is added when
  lxml, cryptography and paramiko publish wheels for it

What stops: new features beyond the Planned list above, unless users ask for them (see Not planned).
