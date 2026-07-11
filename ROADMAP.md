# pfSentinel - Roadmap

## Completed

Everything shipped in the [v0.1.0 release](CHANGELOG.md):

- Full CLI (`pfs`) with backup, device, config, schedule, notify, and update commands
- SSH (SFTP) and HTTPS backup methods
- Extended backups: RRD, packages, DHCP, aliases, certificates, logs
- ZFS snapshot backups with incremental send
- Filesystem archive backups (tar.gz)
- Change detection across config sections
- SHA-256 checksums and gzip compression
- Per-type retention policies
- SSH key authentication
- Credential storage via OS keyring
- Scheduled backups (Windows Task Scheduler + in-process fallback)
- Notifications: Telegram, Slack, Windows toast, Windows Event Log
- Self-update from GitHub Releases
- PyInstaller binary builds (Windows + Linux)
- GitHub Actions CI/CD pipeline
- Unit and integration test suite

---

## Up Next

- [ ] `--json` output mode for all CLI commands (scripting/CI friendly)
- [ ] Linux cron integration for scheduled backups
- [ ] Richer Telegram messages (backup size, pfSense version, hostname)
- [ ] Slack Block Kit layout for notification cards
- [ ] Email notifications (SMTP)
- [ ] OPNsense support (config path + web UI differences)
- [ ] 100% test coverage

---

## Backlog / Ideas

These are ideas under consideration. No commitment to timing or priority.

- Web UI (FastAPI + HTMX) as alternative to CLI
- pfSense REST API support (pfSense Plus 23.09+)
- Multiple backup destinations (local + remote SFTP/S3)
- Backup comparison against a "known good" baseline
- Config encryption at rest
- Docker image for server deployment
- SSH agent forwarding support
- AppImage or standalone binary for Linux
- Windows installer (NSIS or Inno Setup)
- Code signing certificate for Windows and Linux binaries
