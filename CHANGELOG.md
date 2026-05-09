# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **License Notice:** As of May 2026, pfSentinel is licensed under the
> [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).
> This applies **retroactively to all previous releases** (v0.1.0, v0.1.1, v0.1.2)
> and to all future versions. Earlier releases were originally published under the
> MIT License; the copyright holder has relicensed them under Apache 2.0.

## [Unreleased]

### Changed

- Project license changed from MIT to Apache License 2.0 (applies retroactively to all prior versions)

### Security

- Bump paramiko from 3.x to 4.0.0 (CVE-2026-44405 — SHA-1 in RSA keys; no fully patched release yet, ignored in CI until upstream fix ships)

## [0.1.2] - 2026-05-07

### Security

- Add upper bound version caps to all dependencies to limit supply chain attack blast radius
- Add `pip-audit` vulnerability scanning to CI pipeline
- Add hash-pinned lock file verification (`requirements.lock`) in CI
- Add Sigstore build provenance attestations to PyPI/TestPyPI publish steps
- Configure Renovate to maintain lock file in sync with dependency updates

### Added

- `pip-audit` and `pip-tools` added to dev dependencies

## [0.1.1] - 2026-05-07

### Fixed

- Slack webhook URL validation now uses exact domain matching to prevent spoofed hostnames (CWE-20)
- Telegram API URL assertion in tests uses `startswith()` for stricter validation

### Removed

- Unused TUI module (`pfsentinel.tui`) — the project uses CLI only

## [0.1.0] - 2026-04-30

First public beta release.

### Added

- **Core Backup Engine**
  - XML configuration backup via SSH (SFTP) or HTTPS (web UI with CSRF handling)
  - Extended backup targets: RRD data, package configs, DHCP leases, alias files, certificates, system logs
  - ZFS snapshot backups with incremental send support (pfSense 2.5+)
  - Filesystem archive backups (tar.gz of critical directories)
  - Backup orchestrator coordinating all backup types in a single operation
  - Change detection across config sections (interfaces, firewall, system, users, packages, VPN, DHCP, routes)
  - SHA-256 checksum verification for all backup types
  - gzip compression with configurable per-type retention policies
- **CLI (`pfs` command)**
  - `pfs setup` -- guided first-time wizard
  - `pfs status` -- overview of devices, backups, and configuration
  - `pfs backup run` with `--all-extras`, `--include`, `--config-only` flags
  - `pfs backup list / verify / delete / diff / restore / info / search / watch`
  - `pfs backup snapshot` -- ZFS snapshot management
  - `pfs backup archive` -- filesystem archive creation
  - `pfs device add / list / test / remove / edit` with SSH key auth support
  - `pfs config show / init / validate / path / set`
  - `pfs schedule enable / disable / status` (Windows Task Scheduler + in-process fallback)
  - `pfs notify telegram / slack setup` -- notification channels
  - `pfs update` -- self-update from GitHub Releases
  - Interactive backup type selector when running without explicit flags
- **Security**
  - Credentials stored in OS keyring (Windows Credential Manager / SecretService / keyrings.alt)
  - SSH key authentication (ed25519, RSA)
  - Configurable SSL verification per device
  - No passwords or tokens written to config files or logs
- **Notifications**
  - Telegram bot notifications
  - Slack incoming webhook notifications
  - Windows toast notifications (winotify)
  - Windows Event Log integration
- **Cross-Platform**
  - Windows 10/11, Ubuntu, Debian, macOS
  - Python 3.13+ or standalone binary (PyInstaller)
  - CI/CD with GitHub Actions (Ubuntu + Windows, Python 3.13 and 3.14)
