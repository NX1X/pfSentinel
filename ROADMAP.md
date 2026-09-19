# pfSentinel - Roadmap

Goal: get pfSentinel to a solid **1.0**, then move it to **maintenance mode**.
Maintenance mode means no new features, but security fixes, dependency
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
- Supply chain: hash-verified lockfiles, SHA-pinned Actions, 14-day update cooldown,
  CodeQL, Bandit, zizmor, OSV-Scanner, pip-audit, dependency review

---

## Before 1.0 (required)

Verification. These features exist and are unit tested, but have never been observed working for real:

- [ ] **Windows scheduled backup, end to end.** On a real machine, trigger the registered task and confirm
      it reads the password from Credential Manager and completes a backup
- [ ] **Linux systemd timer, end to end.** `pfs schedule enable`, then
      `systemctl --user start pfsentinel-backup.service` and check `journalctl --user -u pfsentinel-backup`
- [ ] **Real pfSense smoke test.** SSH and HTTPS backup against current pfSense CE and pfSense Plus
      (the e2e suite only covers fake servers)
- [ ] **Windows toast** shows on a real desktop (new built-in implementation)

Clean-up (loose ends to remove or finish before the code freezes):

- [ ] **TUI: remove it, or ship it.** `src/pfsentinel/tui/` is not reachable from `pfs`, and it imports
      `textual`, which is not a declared dependency. It is dead code today and causes most of the mypy errors.
      Recommendation: remove it
- [ ] **Windows Event Log notifications:** the `windows_event_log_enabled` config flag exists but does
      nothing. Implement it or remove the flag. Recommendation: remove it
- [ ] **mypy clean and gated in CI** (40 errors today, most in the TUI)
- [ ] **Test coverage 67% -> 80%**, focused on `services/` (backup, connection, orchestrator)
- [ ] **Document restore.** A backup tool needs a written "how to restore this file to pfSense" page

Release hygiene:

- [ ] `SECURITY.md` states which versions get fixes (latest minor only)
- [ ] README states the project status (1.0, maintenance mode) and what that means for users

---

## Optional before 1.0

Small features worth doing only if there is time. Each one is self-contained.

- [ ] `--json` output for `backup list`, `device list` and `schedule status` (for scripting)
- [ ] Richer Telegram messages (size, pfSense version, hostname)
- [ ] Email (SMTP) notifications

---

## Not planned

Out of scope for a tool heading into maintenance. Well-scoped pull requests are still welcome.

- OPNsense support (different config paths and web UI; effectively a second product)
- Web UI, Docker image, Windows installer, AppImage
- pfSense REST API backend, multiple destinations (S3, remote SFTP), baseline comparison
- Slack Block Kit cards
- Code signing for the binaries (paid certificate)

---

## Maintenance mode (after 1.0)

What keeps happening:

- **Security fixes** in pfSentinel code, released as patch versions
- **Dependency updates** via Renovate: minor and patch automerge after a 14-day cooldown;
  security fixes after 3 days; majors reviewed by hand
- **pfSense compatibility** fixes when a new pfSense release changes paths or the web UI
- CI stays green on Windows and Linux, Python 3.13 and 3.14. A new Python minor is added when
  lxml, cryptography and paramiko publish wheels for it

What stops: new features, new platforms and new backup methods.
