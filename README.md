# pfSentinel

[![CI](https://github.com/NX1X/pfSentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/NX1X/pfSentinel/actions/workflows/ci.yml)
[![GitHub Release](https://img.shields.io/github/v/release/NX1X/pfSentinel)](https://github.com/NX1X/pfSentinel/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.13-blue.svg)](https://www.python.org/)

Automated backup and monitoring tool for pfSense firewalls. Built for homelabs.

pfSentinel connects to your pfSense devices over SSH or HTTPS and backs up configuration files, RRD data, package configs, DHCP leases, certificates, logs, ZFS snapshots, and full filesystem archives. It includes change detection, compression, scheduled backups, and notifications via Telegram, Slack, or Windows toast.

## Features

- **Full config backup** via SSH (SFTP) or HTTPS with CSRF-aware login
- **Extended backup targets** -- RRD graphs, package configs, DHCP leases, alias files, certificates, system logs
- **ZFS snapshots** with incremental send (pfSense 2.5+)
- **Filesystem archives** (tar.gz) as a non-ZFS fallback
- **Change detection** -- only saves when config sections actually change
- **SHA-256 verification** for every backup file
- **Scheduled backups** via Windows Task Scheduler or cron
- **Notifications** -- Telegram, Slack, Windows toast, Windows Event Log
- **SSH key authentication** -- no password required
- **Credential security** -- passwords stored in OS keyring, never in config files
- **Self-update** -- check for and install new releases from GitHub
- **Cross-platform** -- Windows, Linux, macOS

## Quick Start

**Download a pre-built binary** (no Python required):

> [**Windows (pfs.exe)**](https://github.com/NX1X/pfSentinel/releases/latest) | [**Linux (pfs)**](https://github.com/NX1X/pfSentinel/releases/latest)

Or install with pip (Python 3.13+):

```bash
pip install pfsentinel
```

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
| [Installation Guide](docs/installation.md) | pip, pre-built binary, and from-source installation |
| [Usage Guide](docs/usage.md) | CLI reference, configuration, scheduling, notifications |
| [Extended Backups](docs/extended-backups.md) | RRD, packages, DHCP, certs, logs, ZFS, archives |
| [Security Policy](SECURITY.md) | Vulnerability reporting, design decisions, credential storage |
| [Contributing](CONTRIBUTING.md) | Development setup, code style, pull requests |
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
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTPS requests |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 / BSD-3-Clause | SSH key handling |
| [Loguru](https://github.com/Delgan/loguru) | MIT | Logging |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | YAML parsing |
| [Requests](https://github.com/psf/requests) | Apache-2.0 | HTTP client |
| [Packaging](https://github.com/pypa/packaging) | Apache-2.0 / BSD-2-Clause | Version parsing |

Paramiko is the only runtime dependency with a copyleft license (LGPL-2.1). When installed via pip, users can freely replace it. For binary releases, pfSentinel uses `--onedir` bundling so Paramiko remains replaceable. See [docs/LICENSE_AUDIT.md](docs/LICENSE_AUDIT.md) for the full dependency license audit.

## Contributing

Contributions are welcome! Please read the [Contributing Guide](CONTRIBUTING.md) before submitting a pull request.

If you find a bug or have a feature request, [open an issue](https://github.com/NX1X/pfSentinel/issues).

## License

[MIT](LICENSE)
