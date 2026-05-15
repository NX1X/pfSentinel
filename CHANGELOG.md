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

## [0.1.4] - 2026-05-11

### Fixed

- Scheduled Windows tasks failed silently every run with `ERROR_INVALID_PARAMETER` (`0x80070057`) due to a double-quoted command line in the task registration. As a result, daily and weekly backups created via `pfs schedule enable` did not execute on Windows.
- `pfs schedule status` now reports the live Task Scheduler state — last run time and last run result — for **both** the daily and weekly tasks (previously only the daily task was shown, and a task failing every run with `0x80070057` was still displayed as healthy). A failed last result is now flagged with remediation guidance instead of appearing as "Created".

### Security

- Bump `urllib3` 2.6.3 → 2.7.0 (CVE-2026-44431: sensitive headers leaked on cross-origin redirects via low-level `ProxyManager` API; CVE-2026-44432: streaming API could decompress full response instead of requested portion)
- Pin explicit `urllib3>=2.7.0,<3` floor in `pyproject.toml` so future lock regenerations cannot drift back below the patched version
- Migrate dependency management from Dependabot to Renovate with a **7-day cooldown** on all updates (including vulnerability alerts) to defend against malicious upstream releases (supply-chain attacks)
- Pin GitHub Actions to immutable commit SHAs (`pinDigests`) - hardens against tag-rewrite attacks
- Enable OSV vulnerability feed (`osvVulnerabilityAlerts`) for broader CVE coverage beyond GHSA

### Added

- `renovate.json` - Renovate config with in-repo Dependency Dashboard, grouped pep621/github-actions updates, 14-day cooldown on major Python deps (lxml/cryptography/paramiko break frequently on majors), and a customManager tracking the `python-version` pin in CI workflows

### Changed

- `.gitignore`: ignore internal-only docs (`docs-internal/`)
- Scheduled tasks are now registered via XML with `LogonType=S4U`, so they run whether the user is signed in, locked, or signed out - no stored password required
- Scheduled tasks no longer skip on battery power (`DisallowStartIfOnBatteries=false`, `StopIfGoingOnBatteries=false`) and now wake the machine from sleep at the scheduled time (`WakeToRun=true`)
- Missed scheduled runs (e.g. machine powered off at the scheduled time) are now caught up on next availability (`StartWhenAvailable=true`)

## [0.1.3] - 2026-05-09

### Changed

- Project license changed from MIT to Apache License 2.0 (applies retroactively to all prior versions)

### Security

- Bump paramiko from 3.x to 4.0.0 (CVE-2026-44405 - SHA-1 in RSA keys; no fully patched release yet, ignored in CI until upstream fix ships)

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

- Unused TUI module (`pfsentinel.tui`) - the project uses CLI only

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
