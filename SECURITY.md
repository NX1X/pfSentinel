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

**Behavior (since 0.2.0):** strict host key checking is the default. pfSentinel connects only to a pfSense device whose SSH host key you confirmed once with `pfs device trust-key <id>` (or at the end of `pfs device add`). A key that changes later is refused with a "possible man-in-the-middle" error, and pfSentinel does not fall back to HTTPS on any host key error.

**Where keys live:** pinned keys are in `~/.pfsentinel/known_hosts` (owner-only, written atomically). For a pinned device, only that file counts: an entry in `~/.ssh/known_hosts` cannot override it. Without a pin, pfSentinel falls back to your OpenSSH `~/.ssh/known_hosts`.

**Verify the fingerprint:** `trust-key` shows the key's SHA256 fingerprint. Compare it on the pfSense console (option 8, Shell):

```
for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done
```

**After a pfSense reinstall:** the host key changes, so backups stop. Check the new fingerprint on the console, then run `pfs device trust-key <id>` again.

**Older configs:** devices saved before 0.2.0 carry `strict_host_keys: false` and still accept unknown keys with a warning, so upgrading does not break existing backups. Run `pfs device trust-key <id>` once to pin the key and switch the device to strict mode.

Also recommended:
1. Use SSH key authentication instead of passwords.
2. Prefer SSH over HTTPS as the primary connection method.

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

1. **`EncryptedKeyring`** - encrypts credentials with a master password at
   `~/.local/share/python_keyring/crypted_pass.cfg`. You will be prompted for
   the master password on first use.
2. **`PlaintextKeyring`** - stores credentials unencrypted at
   `~/.local/share/python_keyring/keyring_pass.cfg`. File permissions are
   automatically set to `0o600` (owner-only). A warning is logged.
3. **In-memory** - credentials are lost when the process exits.

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

## Incident Response

This is a solo-maintained community project. The following is a best-effort process for handling security incidents.

1. **Triage** - When a vulnerability report is received (via private reporting or contact form), I will acknowledge receipt and assess severity.
2. **Fix** - A patch is developed in a private fork or branch. For critical/high-severity issues, this takes priority over other work.
3. **Release** - A new patch release is published with the fix. The security changelog below is updated.
4. **Disclosure** - A GitHub Security Advisory is published with details, affected versions, and upgrade instructions. If the vulnerability was reported by a researcher, they are credited (unless they prefer anonymity).
5. **Notification** - Release notes and the advisory will describe the fix. Users on older versions should upgrade promptly.

For critical vulnerabilities with evidence of active exploitation, a fix will be released as quickly as possible and the advisory will note the urgency.

---

## Security Changelog

A living record of security findings, fixes, and hardening measures.
For the full initial audit report, see [docs/security-audit.md](docs/security-audit.md).

### 2026-04-30 - v0.1.0 (first public release)

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
