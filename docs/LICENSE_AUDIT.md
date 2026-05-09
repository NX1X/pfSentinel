# pfSentinel — License Audit

> Last updated: 2026-04-30
> Project license: **Apache-2.0**

## Runtime Dependencies

| Package | Version Req | License | Apache-2.0-Compatible | Notes |
|---------|-------------|---------|----------------------|-------|
| typer | >=0.15.0 | MIT | Yes | CLI framework |
| click | (via typer) | BSD-3-Clause | Yes | Indirect dependency |
| rich | >=13.9.0 | MIT | Yes | Terminal formatting |
| pydantic | >=2.10.0 | MIT | Yes | Data validation |
| **paramiko** | **>=3.5.0** | **LGPL-2.1** | **Caution** | **See Paramiko section** |
| httpx | >=0.28.0 | BSD-3-Clause | Yes | HTTPS requests |
| requests | >=2.31.0 | Apache-2.0 | Yes | HTTP client |
| urllib3 | (via requests) | MIT | Yes | Indirect dependency |
| cryptography | >=44.0.0 | Apache-2.0 OR BSD-3-Clause | Yes | SSH key handling |
| loguru | >=0.7 | MIT | Yes | Logging |
| pyyaml | >=6.0.0 | MIT | Yes | YAML parsing |
| packaging | >=24.0 | Apache-2.0 / BSD-2-Clause | Yes | Version parsing |
| keyring | >=25.0 | MIT | Yes | OS credential storage |
| keyrings.alt | >=5.0 | MIT | Yes | File-based keyring fallback |
| lxml | >=5.0 | BSD-3-Clause | Yes | XML parsing (HTTPS backup) |
| schedule | >=1.2 | MIT | Yes | In-process job scheduler |

## Dev-Only (Not Shipped)

| Package | License | Notes |
|---------|---------|-------|
| ruff | MIT | Linting and formatting |
| pytest | MIT | Test framework |
| pytest-cov | MIT | Coverage reporting |
| responses | Apache-2.0 | HTTP response mocking |
| mypy | MIT | Static type checking |
| types-paramiko | MIT | Type stubs |
| types-pyyaml | MIT | Type stubs |
| PyInstaller | GPL + special exception | See PyInstaller section |

## Build Tools

| Tool | License | Notes |
|------|---------|-------|
| hatchling | MIT | Build backend |

---

## Paramiko — LGPL-2.1 Implications

Paramiko is the **only runtime dependency with a copyleft license** (LGPL-2.1+).

### What LGPL-2.1 requires

The LGPL allows linking/using the library in non-GPL software, but the end user must be able to **replace the LGPL library** with a modified version.

### Impact by distribution method

| Distribution | Compliant? | Action needed |
|-------------|------------|---------------|
| **pip install** (PyPI) | Yes, no issues | Users install paramiko separately and can freely upgrade/replace it |
| **PyInstaller --onedir** | Yes, with care | Paramiko `.pyc` files are in the output directory and can be swapped by the user |
| **PyInstaller --onefile** | Risky | All files are bundled inside a single `.exe`; users cannot easily replace paramiko |

### Current approach

pfSentinel uses `--onefile` builds for the best end-user experience (single executable download). Since pfSentinel is open-source (Apache-2.0), users can always build from source with a modified or replacement version of Paramiko, which satisfies the LGPL's requirement to allow library replacement. The `THIRD_PARTY_LICENSES.txt` file in the repository acknowledges Paramiko's LGPL-2.1 license.

---

## PyInstaller — GPL with Special Exception

PyInstaller is GPL-licensed but includes a **special exception** that explicitly permits building and distributing non-free executables. The generated binaries can use any license.

**Restrictions:** If you modify PyInstaller's own source code and distribute those modifications, they must be released under the GPL. Using PyInstaller as-is to build binaries has no license impact on your project.

---

## Summary

- All runtime dependencies except one are permissive (MIT, BSD, Apache-2.0)
- **Paramiko (LGPL-2.1)** is the only copyleft dependency — mitigated by open-source availability
- **PyInstaller GPL exception** covers binary distribution
- The project is safe to publish as Apache-2.0 on PyPI and as `--onefile` binaries
