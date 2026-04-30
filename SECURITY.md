# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in pfSentinel, **please do not open a public GitHub issue.**

Instead, report it through one of these channels:

- **Contact form:** [nx1xlab.dev/contact](https://nx1xlab.dev/contact)
- **GitHub private reporting:** [privately report a vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) on this repository

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept (if applicable)
- The version of pfSentinel where you found the issue

I will do my best to acknowledge your report promptly and work toward a fix. This is a community-driven project, so response times may vary. Contributions to security fixes are welcome.

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x (current) | Yes |

---

## Known Security Design Decisions

The following behaviors are intentional design decisions for homelab use cases. They are documented here for transparency.

### SSH Host Key Verification

**Behavior:** pfSentinel uses `paramiko.WarningPolicy` for SSH connections. Unknown host keys are logged as warnings but the connection proceeds.

**Reason:** Home firewall host keys frequently change (firmware updates, full reinstalls). Strict rejection would break backups silently after common maintenance events in a homelab environment.

**Risk:** An attacker on your local network capable of ARP spoofing could perform a man-in-the-middle attack on SSH connections. This is unlikely in a typical homelab but is a real threat on untrusted networks.

**Mitigation:** Set `strict_host_keys: true` in your device config (or use `--strict-host-keys` when adding a device). This uses `paramiko.RejectPolicy` — the host must already be in `~/.ssh/known_hosts` or the connection is refused.

Additional steps:
1. Pre-populate `~/.ssh/known_hosts` for your pfSense device: `ssh-keyscan -p 22 <host> >> ~/.ssh/known_hosts`
2. Use SSH key authentication instead of passwords.c
3. Prefer SSH over HTTPS as the primary connection method.

---

### SSL Certificate Verification

**Behavior:** `verify_ssl` defaults to `True`. It can be set to `False` per-device (via `--no-verify-ssl` at device-add time, or in the config file).

**Reason:** pfSense ships with a self-signed certificate. Many homelab users do not have a trusted CA certificate installed. Disabling verification allows HTTPS backup without certificate management overhead.

**Risk:** When `verify_ssl=False`, HTTPS connections are vulnerable to man-in-the-middle attacks. An attacker could intercept the session and steal the pfSense admin password.

**Recommendation:** Use SSH (the default and recommended connection method) rather than HTTPS. SSH does not depend on TLS certificates.

---

### HTTP Connection Method

**Behavior:** pfSentinel supports HTTP (unencrypted) as an explicit fallback connection method.

**Risk:** HTTP transmits the pfSense admin username and password in cleartext. Any device on the same network can capture these credentials with a packet sniffer.

**When it might be used:** Some very old pfSense installations with HTTP-only access. Use SSH or HTTPS whenever possible.

---

### Credential Storage

Passwords and tokens are stored in the OS-native keyring:

| Platform | Backend |
|----------|---------|
| Windows | Windows Credential Manager (encrypted at rest) |
| Linux desktop | SecretService / GNOME Keyring / KWallet |
| Linux headless / WSL | `keyrings.alt` file backend (`~/.local/share/python_keyring/`) |

**Note on headless/WSL fallback:** When no native keyring is available, pfSentinel
tries backends in this order:

1. **`EncryptedKeyring`** — encrypts credentials with a master password at
   `~/.local/share/python_keyring/crypted_pass.cfg`. You will be prompted for
   the master password on first use.
2. **`PlaintextKeyring`** — stores credentials unencrypted at
   `~/.local/share/python_keyring/keyring_pass.cfg`. File permissions are
   automatically set to `0o600` (owner-only). A warning is logged.
3. **In-memory** — credentials are lost when the process exits.

If you are on `PlaintextKeyring`, consider installing a system keyring backend
or verify permissions manually:
```bash
chmod 600 ~/.local/share/python_keyring/keyring_pass.cfg
```

---

### Logging

pfSentinel does **not** log passwords, tokens, or credential values. Log files contain only hostnames, operation status, and error messages. Verify with `--debug` if you are unsure what is being recorded.

---

## Dependency Security

To audit dependencies locally:

```bash
pip install pip-audit
pip-audit
```

For a full license and dependency audit, see [docs/LICENSE_AUDIT.md](docs/LICENSE_AUDIT.md).

---

## Permissions the Application Requests

| Permission | Why |
|-----------|-----|
| SSH access to pfSense (port 22) | Download config via SFTP |
| HTTPS/HTTP access to pfSense web UI | Alternative download method |
| OS keyring read/write | Store and retrieve device passwords |
| File system write to backup directory | Store backup files |
| Windows Task Scheduler (Windows only) | Create/delete scheduled backup tasks |
| Telegram/Slack API (optional) | Send backup notifications |

pfSentinel does **not** require or request administrator/root privileges for normal operation.

---

## Security Changelog

A living record of security findings, fixes, and hardening measures.
For the full initial audit report, see [docs/security-audit.md](docs/security-audit.md).

### 2026-04-30 — v0.1.0 (first public release)

**Fixes applied from pre-release audit (19 findings, all fixed):**

| # | Severity | Finding | Fix |
|---|----------|---------|-----|
| 1 | Critical | Command injection in ZFS commands | `shlex.quote()` on all interpolated values |
| 2 | Critical | Command injection in tar commands | `shlex.quote()` on all path components |
| 3 | Critical | SSH host key verification allows MITM | Load known_hosts; added `strict_host_keys` config option |
| 4 | Critical | Telegram bot token exposed in URL logging | Suppress urllib3 debug logging during API calls |
| 5 | High | SSL verification fully disabled | Added `ca_cert_path` option; scoped warning suppression |
| 6 | High | PlaintextKeyring stores credentials unencrypted | EncryptedKeyring preferred; `0o600` permissions enforced |
| 7 | High | Path traversal in backup restore target | `resolve()` + symlink rejection |
| 8 | High | Unvalidated `--dirs` in archive CLI | Absolute path + no `..` validation |
| 9 | High | Slack webhook URL not validated (SSRF) | HTTPS enforced; hostname checked |
| 10 | Medium | Config file permissions race condition | `os.fchmod()` before write |
| 11 | Medium | Backup files created with default permissions | `umask(0o077)` on Unix |
| 12 | Medium | Incomplete symlink protection | Checks on restore source + retention paths |
| 13 | Medium | No socket timeout on stream reads | `settimeout()` on SSH channel |
| 14 | Medium | Weak CSRF token regex | Dual regex patterns for attribute ordering |
| 15 | Medium | Information disclosure in error messages | Sanitized user-facing errors |
| 16 | Medium | Duplicated device ID regex | Shared `DEVICE_ID_PATTERN` constant |
| 17 | Low | SSH key path not validated | `field_validator` rejects directories |
| 18 | Low | Slack error messages could leak webhook URL | Specific exception handling |
| 19 | Low | Weak `relative_path` validator | Rejects `.` and `%2e`/`%2f` sequences |

**Additional hardening measures:**

| # | Type | Measure |
|---|------|---------|
| 20 | Supply chain | Binary update checksum verification (SHA-256 against release asset) |
| 21 | Defense in depth | Remote command execution allowlist on SSH connector |
| 22 | Least privilege | CI/CD permissions scoped to specific jobs |
| 23 | Dependencies | Renovate: SHA-pinned Actions, vulnerability alerts, priority security updates |
