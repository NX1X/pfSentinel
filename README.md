# pfSentinel

[![CI](https://github.com/NX1X/pfSentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/NX1X/pfSentinel/actions/workflows/ci.yml)
[![GitHub Release](https://img.shields.io/github/v/release/NX1X/pfSentinel)](https://github.com/NX1X/pfSentinel/releases)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.13-blue.svg)](https://www.python.org/)
![Views](https://komarev.com/ghpvc/?username=NX1X-pfSentinel&label=views&color=brightgreen)

Automated backup and monitoring tool for pfSense firewalls. Built for homelabs.

pfSentinel connects to your pfSense devices over SSH or HTTPS and backs up configuration files, RRD data, package configs, DHCP leases, certificates, logs, ZFS snapshots, and full filesystem archives. It includes change detection, a restore check, encryption at rest, a second copy off the PC, scheduled backups, and alerts by Telegram, Slack, email, desktop notification or the system log.

## A Word From the Developer

I was looking for a feature-rich, modern pfSense backup tool and only found projects that were no longer maintained. Since pfSense backups are part of my own [homelab backup infrastructure](https://github.com/NX1X/homelab), I decided to build pfSentinel and share it with the pfSense community.

If you have a request or an idea, I'd love to hear it, please [open a feature request](https://github.com/NX1X/pfSentinel/issues). You can also reach me on [LinkedIn](https://www.linkedin.com/in/edenporat) or through my [website](https://nx1xlab.dev/contact).

## Features

- **Full config backup** via SSH (SFTP) or HTTPS with CSRF-aware login
- **Extended backup targets** -- RRD graphs, package configs, DHCP leases, alias files, certificates, system logs
- **ZFS snapshots** with incremental send (pfSense 2.5+)
- **Filesystem archives** (tar.gz) as a non-ZFS fallback
- **Change detection** -- every backup names the config sections that changed (`firewall`, `users`, ...), and risky changes raise an alert
- **SHA-256 verification** for every backup file, and a **restore check** of each config backup (users, admin, interfaces, firewall rules, certificates)
- **Encryption at rest** (optional) -- AES-256-GCM with a key in the OS keystore and a recovery key, or a passphrase, or the open age format; [details](docs/encryption.md)
- **Second copy** of the encrypted backups to a folder or SFTP, and (experimental) S3-compatible storage, Azure Blob Storage or WebDAV; [details](docs/second-copy.md)
- **Long-term retention** -- daily backups, plus weekly and monthly ones kept longer
- **Scheduled backups** via Windows Task Scheduler, systemd user timers or cron
- **Alerts** -- Telegram, Slack, email (any SMTP server), Windows toast or Linux desktop notification, Windows Event Log or syslog, and an optional daily digest; for a failed backup, a risky change, no recent backup, a failed restore check or a failed second copy; [details](docs/alerts.md)
- **SSH key authentication** -- no password required
- **Strict SSH host key checking** -- confirm the pfSense key once (`pfs device trust-key`); a changed key is refused
- **Credential security** -- passwords in the OS keyring (or an AES-256-GCM encrypted store on headless systems), never in config files
- **Self-update** -- install new releases from GitHub; the binary's checksum and its signed build attestation are checked first
- **Cross-platform** -- Windows and Linux (macOS best effort)
- **Reproducible CI** -- lint, tests, security audit and build defined with [Dagger](https://dagger.io), so the same pipeline runs on your machine and in GitHub Actions; hash-verified lockfiles and SHA-pinned Actions

## Quick Start

**Download a pre-built binary** (no Python required):

> [**Windows (pfs.exe)**](https://github.com/NX1X/pfSentinel/releases/latest) | [**Linux (pfs)**](https://github.com/NX1X/pfSentinel/releases/latest)

Or install from PyPI (Python 3.13+), same command on Linux and Windows:

```bash
pipx install pfsentinel
```

Plain `pip install pfsentinel` also works in a virtualenv or on Windows; on Ubuntu 23.04+ and Debian 12+ the system Python blocks it, so use pipx there.

Then:

```bash
pfs setup          # guided first-time wizard
pfs device add     # add your pfSense device
pfs backup run     # run your first backup
```

See the [Installation Guide](docs/installation.md) for all installation methods.

## Documentation

| Document | Description |
|----------|-------------|
| [Online manual](https://pfs.nx1xlab.dev/docs) | Every command, recipes, and a copy-paste version for AI assistants (`pfs docs`) |
| [Installation Guide](docs/installation.md) | pip, pre-built binary, and from-source installation |
| [Usage Guide](docs/usage.md) | CLI reference, configuration, scheduling, notifications |
| [Extended Backups](docs/extended-backups.md) | RRD, packages, DHCP, certs, logs, ZFS, archives |
| [Encryption at Rest](docs/encryption.md) | Modes, recovery key, file format, standalone decryption |
| [Alerts](docs/alerts.md) | Email, daily digest, desktop notifications, Event Log |
| [Second Copy](docs/second-copy.md) | Folder, SFTP, S3-compatible, Azure, WebDAV (experimental) |
| [Container](docs/docker.md) | Docker image and Kubernetes CronJob (experimental) |
| [Security Policy](SECURITY.md) | Vulnerability reporting, design decisions, credential storage |
| [Contributing](CONTRIBUTING.md) | Development setup, code style, pull requests |
| [Code of Conduct](CODE_OF_CONDUCT.md) | How we treat each other, and how to report a problem |
| [CI with Dagger](docs/ci-dagger.md) | Run the CI pipeline locally |
| [Roadmap](ROADMAP.md) | Path to 1.0 and what is planned |
| [Changelog](CHANGELOG.md) | Version history |

## What Gets Backed Up

| Target | Method | Description |
|--------|--------|-------------|
| XML Config | SSH / HTTPS | Full pfSense configuration (`config.xml`) |
| RRD Data | SSH | Traffic and performance graphs |
| Package Configs | SSH | Installed package settings |
| DHCP Leases | SSH | Active DHCP lease table |
| Alias Files | SSH | URL tables and external alias files |
| Certificates | SSH | SSL/TLS certs from the filesystem |
| System Logs | SSH | Filter log, system log, custom log files |
| ZFS Snapshot | SSH | Full or incremental ZFS snapshot stream |
| Filesystem Archive | SSH | Tar archive of critical directories |

## Third-Party Credits

pfSentinel is built on these open-source libraries:

| Library | License | Purpose |
|---------|---------|---------|
| [Typer](https://github.com/fastapi/typer) | MIT | CLI framework |
| [Rich](https://github.com/Textualize/rich) | MIT | Terminal formatting |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | Data validation |
| [Paramiko](https://github.com/paramiko/paramiko) | LGPL-2.1 | SSH/SFTP connections |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 / BSD-3-Clause | SSH key handling |
| [Requests](https://github.com/psf/requests) | Apache-2.0 | HTTP client |
| [Packaging](https://github.com/pypa/packaging) | Apache-2.0 / BSD-2-Clause | Version parsing |

Paramiko is the only runtime dependency with a copyleft license (LGPL-2.1). When installed via pip, users can freely replace it. For binary releases, pfSentinel uses `--onedir` bundling so Paramiko remains replaceable. See [docs/LICENSE_AUDIT.md](docs/LICENSE_AUDIT.md) for the full dependency license audit.

## Contributing

Contributions are welcome! Please read the [Contributing Guide](CONTRIBUTING.md) before submitting a pull request.

If you find a bug or have a feature request, [open an issue](https://github.com/NX1X/pfSentinel/issues).

## Support

pfSentinel is free and open source. If it saves you time or you'd like to support continued development, you can [sponsor on GitHub](https://github.com/sponsors/NX1X) or [buy me a coffee](https://buymeacoffee.com/nx1x). Thank you!

## License

[Apache 2.0](LICENSE)
