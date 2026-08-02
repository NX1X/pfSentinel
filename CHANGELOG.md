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

### Security

- Replace the unmaintained `defusedxml` dependency with a hardened `lxml` parser for `config.xml`. `defusedxml` has had no release since 0.7.1 (March 2021) and no upstream commit since October 2023, which is not a safe position for the component that guards pfSentinel's only untrusted input. `lxml` was already a dependency, so this removes a package rather than swapping one. The replacement disables entity resolution, network access, DTD loading and huge trees, and additionally rejects any config carrying a DOCTYPE so entity attacks fail loudly instead of parsing with unresolved references
- Add `TestXxeHardening` covering classic XXE file disclosure, entity-expansion ("billion laughs"), external DTD, network entity, and blind-XXE parameter entities. The previous `defusedxml` protection had no test coverage at all, so this is the first time the XML security boundary is actually verified

- Add property-based fuzzing for the XML parser (`hypothesis`). `validate_xml` is pfSentinel's only untrusted-input boundary, so the suite asserts one invariant across arbitrary text, arbitrary bytes, generated XML documents, deep nesting and XML-metacharacter soup: parsing either returns a well-formed pfSense root or raises `PfSenseXMLError`, and nothing else escapes. Verified to catch a real regression - re-enabling entity resolution makes the suite fail with actual file contents in the assertion output

### Removed

- `defusedxml` is no longer a runtime dependency
- Remove the OpenSSF Scorecard CI job. Most of its findings were not actionable for a single-maintainer repo: `Branch-Protection` was a false negative (Scorecard reads the legacy branch-protection API and cannot see the repository rulesets that are actually enforcing 11 required checks, no force-push and no deletion), and `Code-Review` scores the absence of a second reviewer. Bandit, CodeQL, zizmor, OSV-Scanner, dependency-review and pip-audit all still run - Scorecard was scoring posture, not finding vulnerabilities. Tracked for revisit in `docs-internal/WORK-STATUS.md`

## [0.1.5] - 2026-07-12

### Fixed

- **Release binaries now start correctly.** `click` is now declared as an explicit dependency. `typer` 0.26 stopped pulling `click` in transitively, but the CLI imports `click` directly (`notify`/`device` commands), so the standalone PyInstaller binaries crashed on launch with `ModuleNotFoundError: No module named 'click'`. This broke the Linux release build (and would have shipped a broken Windows `.exe`, which had no smoke test). `click` is now also an explicit PyInstaller hidden-import and the Windows build has a smoke test.
- Scheduled Windows tasks failed silently every run with `ERROR_INVALID_PARAMETER` (`0x80070057`) due to a double-quoted command line in the task registration. As a result, daily and weekly backups created via `pfs schedule enable` did not execute on Windows.
- `pfs schedule status` now reports the live Task Scheduler state — last run time and last run result — for **both** the daily and weekly tasks (previously only the daily task was shown, and a task failing every run with `0x80070057` was still displayed as healthy). A failed last result is now flagged with remediation guidance instead of appearing as "Created".
- An invalid `--weekly-day` value (typo or unexpected input) no longer produces a malformed Task Scheduler XML element name; unrecognized days now fall back to Sunday so weekly task registration cannot fail on bad input.

### Security

- Bump `paramiko` 4.0.0 → 5.0.0 (fixes CVE-2026-44405 / GHSA-r374-rxx8-8654: SHA-1 signature verification weakness). This removes the temporary `pip-audit` / OSV-Scanner ignore for that advisory that was in place while no fixed paramiko release existed
- Bump `pytest` 8 → `>=9.1.1,<10` (dev/test dependency; CVE-2025-71176 / GHSA-6w46-j5rx-g56g: predictable `/tmp/pytest-of-{user}` paths on UNIX allow a local user to cause a denial of service or possibly escalate privileges). Fixed upstream in 9.0.3; the floor is pinned so a lock regeneration cannot drift back onto a vulnerable 9.0.x
- Bump `requests` → `>=2.34.2,<3` (precautionary security update)
- Bump `cryptography` 46.0.7 → 48.0.1 (GHSA-537c-gmf6-5ccf: the OpenSSL statically linked into cryptography wheels prior to 48.0.1 was vulnerable to a High-severity issue, CVSS 7.5). Pin explicit `cryptography>=48.0.1,<49` floor in `pyproject.toml` and regenerate `requirements.lock` with hash verification
- Bump `urllib3` 2.6.3 → 2.7.0 (CVE-2026-44431: sensitive headers leaked on cross-origin redirects via low-level `ProxyManager` API; CVE-2026-44432: streaming API could decompress full response instead of requested portion)
- Pin explicit `urllib3>=2.7.0,<3` floor in `pyproject.toml` so future lock regenerations cannot drift back below the patched version
- Migrate dependency management from Dependabot to Renovate with a **14-day cooldown** on regular updates (and 14 days on majors) to defend against malicious upstream releases (supply-chain attacks), while keeping vulnerability-alert updates on a short **3-day** cooldown so genuine CVE fixes still land quickly
- Pin GitHub Actions to immutable commit SHAs (`pinDigests`) - hardens against tag-rewrite attacks
- Enable OSV vulnerability feed (`osvVulnerabilityAlerts`) for broader CVE coverage beyond GHSA
- Add **OSV-Scanner** CI gate that scans `requirements.lock` against the OSV database on every push/PR (`.github/workflows/security.yml`), with intentional ignores kept in sync via `osv-scanner.toml`
- Enable **Ruff `S` (flake8-bandit) rules** for inline SAST at lint time (hardcoded secrets, `shell=True`, pickle, weak hashes, `verify=False`, SQL injection), complementing Bandit and CodeQL
- Add a **detect-secrets** pre-commit hook (`.pre-commit-config.yaml` + `.secrets.baseline`) for local secret scanning before push, complementing server-side GitGuardian
- Harden the self-update flow: the downloaded binary is now `chmod 0o700` (owner-only) instead of `0o755`, removing group/world access (resolves CodeQL `py/overly-permissive-file`)
- Set `persist-credentials: false` on all `actions/checkout` steps in the security workflow so the `GITHUB_TOKEN` is not left on disk after checkout (zizmor `artipacked` hardening)
- Renovate now explicitly blocks major Python version-manager jumps (e.g. 3.x → 4.x) until wheel/support exists, instead of only limiting the minor/patch rule

### Added

- `.github/renovate.json` - Renovate config with in-repo Dependency Dashboard, grouped pep621/github-actions updates, 14-day cooldown on major Python deps (lxml/cryptography/paramiko break frequently on majors), and a customManager tracking the `python-version` pin in CI workflows

### Changed

- Add a smoke test to the Windows binary build so a non-runnable `.exe` can no longer pass CI
- Relocate the Renovate config to `.github/renovate.json` and migrate the deprecated `fileMatch` fields to `managerFilePatterns` (fixes the Renovate "pip-compile: dependency not found in lock file" repository warning)
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
