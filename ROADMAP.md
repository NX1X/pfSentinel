# pfSentinel - Roadmap

Goal: get pfSentinel to a solid **1.0**, then move it to **maintenance mode**.
Maintenance mode means a slower pace focused on the Planned list, while security fixes, dependency
updates and pfSense compatibility fixes keep landing (see the end of this file).

Last updated: 2026-09-19

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

---

## Before 1.0 (required)

### Full cross-platform parity (Windows and Linux)

1.0 means every feature works the same way on Windows and Linux, and CI proves it on both.

- [ ] **Desktop notifications on Linux.** Windows has toasts; Linux gets freedesktop notifications
      (the same mechanism `notify-send` uses), with the same on/off setting
- [ ] **System log on both.** Replace the Windows-only `windows_event_log_enabled` flag (never implemented)
      with one `system_log_enabled` setting: Windows Event Log on Windows, journald/syslog on Linux
- [ ] **File permissions on Windows.** On Linux, config, backups and the secret store are `0600`/`0700`.
      On Windows `chmod` does nothing, so set explicit owner-only ACLs as well
- [ ] **Scheduling without admin on Windows.** Linux needs no root; Windows currently needs an elevated shell
      because tasks live in a `\pfSentinel` folder. Register in a location a normal user can write
- [ ] **One scheduling story in the docs:** what runs when you are logged out, asleep or powered off,
      on each OS, side by side
- [ ] **Parity check in CI:** a test that fails if a user-facing feature is gated to one OS without a
      counterpart on the other

### Verification

These features exist and are unit tested, but have never been observed working for real:

- [ ] **Windows scheduled backup, end to end.** On a real machine, trigger the registered task and confirm
      it reads the password from Credential Manager and completes a backup
- [ ] **Linux systemd timer, end to end.** `pfs schedule enable`, then
      `systemctl --user start pfsentinel-backup.service` and check `journalctl --user -u pfsentinel-backup`
- [ ] **Real pfSense smoke test.** SSH and HTTPS backup against current pfSense CE and pfSense Plus
      (the e2e suite only covers fake servers)
- [ ] **Windows toast** shows on a real desktop (new built-in implementation)

### Clean-up

- [ ] **TUI: remove it, or ship it.** `src/pfsentinel/tui/` is not reachable from `pfs`, and it imports
      `textual`, which is not a declared dependency. Recommendation: remove it
- [ ] **mypy clean and gated in CI** (40 errors today, most in the TUI)
- [ ] **Test coverage 67% -> 80%**, focused on `services/` (backup, connection, orchestrator)
- [ ] **Document restore.** A written "how to restore this file to pfSense" page
- [ ] **Move the Linux CI jobs onto the Dagger pipeline** once it has run green for a while

### Release hygiene

- [ ] `SECURITY.md` states which versions get fixes (latest minor only)
- [ ] README states the project status (1.0, maintenance mode) and what that means for users

---

## Optional before 1.0

Small features worth doing only if there is time. Each one is self-contained.

- [ ] `--json` output for `backup list`, `device list` and `schedule status` (for scripting)
- [ ] Richer Telegram messages (size, pfSense version, hostname)
- [ ] Email (SMTP) notifications

---

## Planned (1.x)

Bigger features that are planned after 1.0. Maintenance mode pauses new work, not these.

- [ ] **pfSense REST API backend** (pfSense Plus 23.09+ and the REST API package), as a third
      connection method next to SSH and HTTPS
- [ ] **Multiple backup destinations:** keep the local copy and also upload to S3-compatible storage
      and/or a remote SFTP server, with the same retention rules
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
