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

## [1.0.0] - 2026-12-04

The first stable release. The code is the same as 1.0.0-rc.6. Before it came out, the release candidates ran 14 days in a row (2026-11-20 to 2026-12-04) on a homelab PC with Windows 11 and pfSense 2.8.1, without a failed backup.

**To update:** `pfs update install`. It works for the binary and for pip and pipx installs.

**What changes for you:**

- Nothing in your configuration or your scheduled tasks. You do not need to run `pfs schedule enable` again.

The changes since 0.3.0 are in the 1.0.0-rc.1 to 1.0.0-rc.6 entries below.

## [1.0.0-rc.6] - 2026-11-28

The sixth release candidate. It adds a setting that turns off the release check, and a list of every network connection pfSentinel makes. The backup path does not change. It is a GitHub pre-release. The 14-day stability gate for 1.0.0 keeps its dates (2026-11-20 to 2026-12-04).

**To try it:** `pfs update install --pre`.

**What changes for you:**

- `pfs config set update-check off` stops the check task from asking GitHub for new releases. It is on by default, as before.
- Nothing changes in the scheduled tasks. You do not need to run `pfs schedule enable` again.

### Added

- `pfs config set update-check off` turns off the release check. The check task then makes no request to GitHub. Before, the only way was to edit `notifications.update_alerts` in config.json. `pfs notify status` shows the setting.
- `docs/privacy.md` lists every network connection pfSentinel makes. pfSentinel has no telemetry.
- `scripts/mock_pfsense.py` starts a mock pfSense web UI on 127.0.0.1, to test a build without a real firewall.
- winget: the CI job `publish-winget` sends each stable release to `microsoft/winget-pkgs` (package `NX1X.pfSentinel`), after the maintainer approves it. Release candidates are not sent. The manifest uses schema 1.28.0.

### Fixed

- Tests no longer write a backup into the real `~/Documents/pfSentinel` folder.

## [1.0.0-rc.5] - 2026-11-28

The fifth release candidate. It adds a history of backup runs, makes `pfs schedule run-now` a full run, and gets pfSentinel ready for winget. It is a GitHub pre-release. The 14-day stability gate for 1.0.0 keeps its dates (2026-11-20 to 2026-12-04).

**To try it:** `pfs update install --pre`.

**What changes for you:**

- `pfs backup history` shows past runs with their result, failed runs included. The history starts with the first run after this update.
- `pfs schedule run-now` now saves the extras and makes the second copy too, like a scheduled run. Before, it saved only the config.
- `pfs backup list` no longer cuts long file names in a narrow window.
- Nothing changes in the scheduled tasks. You do not need to run `pfs schedule enable` again.

### Added

- `pfs backup history`: the runs of `pfs backup run` and `pfs schedule run-now`, scheduled or by hand, with their result. A run that failed made no file, so `pfs backup list` never showed it, and on Windows only the last task result remained. The results are `OK`, `Partial` (the config was saved, a part failed), `Failed` and `No end` (the run was stopped before its end, for example by a shutdown). Options: `-d` for one device, `-n` for the number of runs, `--problems`, `--json`. The history is `run_history.jsonl` next to `config.json`, and it starts with this version. It is best effort: a history file that cannot be written never stops a backup. See docs/usage.md (Run History).
- pfSentinel now knows when winget installed it (planned from 1.0.0). `pfs update install` then changes nothing and names `winget upgrade NX1X.pfSentinel`, and so does the update alert. The manifest template is in packaging/winget.

### Changed

- `pfs backup list` in a narrow window cut the file names with "...". Now a long file name is folded over more lines, and with one device the Device column is left out.

### Fixed

- `pfs schedule run-now` saved only the config: no extras, no second copy, no ZFS snapshot or archive. It also ended with exit code 0 when a backup failed. Its help said "same as backup run". Now it runs a full backup like `pfs backup run --non-interactive`, and a failed device gives exit code 1.
- docs/usage.md named a log file (`~/.pfsentinel/logs/pfsentinel.log`) that pfSentinel never wrote, and a `pfs --debug` option that does not exist. The log goes to the console, and the level is `log_level` in `config.json`. The Self-Update section named `pfs update` as the command; the commands are `pfs update check`, `install` and `revert`.

## [1.0.0-rc.4] - 2026-11-22

The fourth release candidate. It fixes the ZFS snapshot default for pfSense 2.8.1, makes restore and decrypt work on large files, and adds the lock alert and a richer "backup OK" message. It is a GitHub pre-release. The 14-day stability gate for 1.0.0 keeps its dates (2026-11-20 to 2026-12-04).

**To try it:** `pfs update install --pre`.

**What changes for you:**

- ZFS snapshots now use the dataset mounted at `/` (for example `pfSense/ROOT/default`). A saved `"zroot/ROOT"` is read as empty, so you do not need to change `config.json`.
- With success messages on, you get one "backup OK" message per run, with every file, its size and the config changes.
- A new alert, event ID 1009 "Backup folder cannot be locked", for network shares that cannot lock. It goes out at most once in 7 days per device.
- `pfs backup restore` and `pfs backup decrypt` copy in 1 MiB steps, so a ZFS stream of several GB no longer needs that much memory.
- New guides: docs/restore.md and docs/platforms.md.

### Added

- A new alert, event ID 1009 "Backup folder cannot be locked" (Warning). Some network shares (SMB, NFS) cannot lock the `.pfsentinel.lock` file. The run then goes on without the lock, as before, but two runs at once can lose records of the backup index. Before, only the log said so. The alert goes out at most once in 7 days per device, and a manual run also shows it on screen.
- docs/restore.md: how to open an encrypted backup and put a config back on pfSense, on the same PC or another one, with notes on extras and ZFS streams.
- docs/platforms.md: what runs when the PC is off, asleep or signed out, and a table of every command group on Windows and Linux. A new test fails when a command group is missing from the table or works on one system only.

### Changed

- The "backup OK" alert covers the whole run: one message with every file, its size and, for the config, its changes. It also shows the pfSense host name and version, the run number, the total size, whether the files are encrypted, and the pfSentinel version. Before, it named only the config file. See docs/usage.md (Alerts).

### Fixed

- `pfs backup restore` and `pfs backup decrypt` read the whole file into memory. A ZFS stream can be several GB, so both now copy in 1 MiB steps. A file that fails half way (wrong key, cut short) leaves no partial copy, and an existing file at the target stays as it was. The plain copy is created readable by you only (0600 on Linux and macOS). The symlink check on the restore target now runs before the path is resolved, so it works.
- docs/second-copy.md: the provider table was checked against each provider's documentation. The Linode endpoint is an endpoint ID, which is not always the region. DigitalOcean also accepts `us-east-1`. Scaleway has `it-mil`. The Nextcloud app password is under Personal settings, Security.
- ZFS snapshots used the dataset `zroot/ROOT` by default. On a pfSense 2.8.1 firewall the pool is `pfSense`, so that dataset does not exist. Also, `<pool>/ROOT` is only an empty container for the boot environments. Now an empty `dataset` (the new default) means the dataset mounted at `/`, for example `pfSense/ROOT/default`. A saved `"zroot/ROOT"` is read as empty, so you do not need to change `config.json`.

## [1.0.0-rc.3] - 2026-11-21

The third release candidate. It fixes the bugs found in the rc.2 field test and in a code review before 1.0.0: about 30 fixes, most of them in scheduled runs, the second copy, alerts and self-update. It is a GitHub pre-release. The 14-day stability gate for 1.0.0 keeps its dates (2026-11-20 to 2026-12-04).

**To try it:** `pfs update install --pre`. Then run `pfs schedule enable` once, so old Task Scheduler tasks are cleaned up.

**What changes for you:**

- Telegram alerts are plain text: the title is no longer bold.
- A new alert, event ID 1008 "Backup partly failed", reports a failed extra, ZFS snapshot or archive. Before, these were only logged.
- Each device folder gets a `.pfsentinel.lock` file. Leave it there: it keeps two runs of one device apart.
- The daily digest covers the time since the last digest, not a fixed 24 hours.

### Fixed

- A backup run with no extras turned on (the default) ended right after the config. It skipped the second copy (since 0.3.0) and the ZFS snapshot or filesystem archive, even when they were turned on. A `--config-only` run also skipped the second copy. Now every run makes the second copy; `--config-only` still skips extras, ZFS and archive.
- `pfs config validate` no longer reports "missing password" for a device that logs in with an SSH key. It shows `SSH key`, or an error if the key file is missing.
- `pfs backup verify` now saves its result in the backup index. Before, a passed check did not change the `?` status in `pfs backup list`, so files added by `pfs backup reindex` kept `?` forever. A failed check now sets `?`.
- ZFS: retention counted ZFS backups like any other type and deleted the oldest first. That was the full send, so every incremental after it could no longer be restored. Retention now deletes a ZFS chain (a full send and its incrementals) only as a whole, and a chain at the ZFS limit starts with a new full send.
- ZFS: the device ran `zfs send ... | gzip`, which exits with gzip's status. A failed send still gave a small valid file, recorded as a good backup. pfSentinel now streams `zfs send` and gzips it on this computer, so a failed or empty send is an error. The file format stays the same.
- ZFS: a failed transfer left its new snapshot on the device, where no cleanup removed it. It is now destroyed.
- ZFS: an incremental send was based on the newest snapshot on the device, even when its local file was gone. It now needs the newest local ZFS backup as its base, in the configured dataset and still on the device, or it makes a full send. Before, a destroyed base or a changed dataset made every later run fail until a manual `--full`.
- `pfs backup snapshot` now holds the device lock, like a backup run.
- On Windows, `pfs schedule enable` only created tasks and never removed old ones. Turning the weekly backup off left the weekly task running. Enabling once as administrator and once without left both daily tasks, so two backups started at 02:00. Enable now removes, after it has created the new tasks, every pfSentinel task it does not want. As administrator it keeps the no-admin `pfSentinel-*` tasks, which may belong to another user, and prints the command to remove them.
- `pfs schedule disable` said "Scheduling disabled" even when a task could not be removed (for example a task made as administrator, removed from a normal shell). It now says so, and exits 1. A Linux user who may not use cron no longer gets this error.
- `pfs update revert` after a failed `pfs update install` could copy an older binary over the working one, and named the wrong version. A binary update now saves the revert details only after it backs up the current binary.
- `pfs update install` showed a traceback for a download error, a folder it cannot write, or a GitHub rate limit on the attestation check. These are now clear errors, and they no longer quote the signed download URL. When the new binary fails its check and the old one cannot be put back, the error now says so and names the backup file.
- A root update of a shared `/usr/local/bin/pfs` made it readable by root only, so the timers of other users failed. The new binary now keeps the mode of the old one.
- A device that logs in with an SSH key, with no password, reported "No password stored ... HTTPS needs one" when SSH failed. The real SSH error was only in the log. A key-only device now tries SSH only, and reports its error.
- An SSH key path typed as a relative path (`id_ed25519`), or pasted with quotes from Explorer's "Copy as path", was saved as typed. `pfs device test` passed, but scheduled runs start in another folder and could not find the key. `pfs setup`, `pfs device add` and `pfs device edit` now save the full path, without quotes.
- `pfs device add --id Home-FW` saved the device as `home-fw` but its password under `Home-FW`. On Linux every backup then failed with "No password stored". Secrets now use the saved ID.
- On Windows, a user name with letters like `ü` or `É` could crash every command that saves the config, and made `pfs schedule status` say "Not registered". The output of `whoami`, `schtasks` and `icacls` is now read in the console code page.
- `config.json` saved with a BOM (Windows PowerShell 5 `Set-Content -Encoding UTF8`) was reported as invalid JSON. It now loads.
- On Linux, `pfs schedule status` showed the backup service's last result for the check timer. It now shows the check service's result.
- Two backup runs of one device at the same time could lose records from the backup index and give two runs the same number. This happens when the PC is off at 02:00 and 03:00: the daily and the weekly task then start together at sign-in. Every command that changes the index now locks the device (`.pfsentinel.lock` in its folder): a backup run, `pfs schedule run-now`, reindex, verify, delete, `encrypt-existing`, snapshot and archive. The second run waits for the first, at most 40 minutes, then fails with an alert. The second copy runs after the lock is released, so a long upload does not hold it. On a file system without lock support (some NFS and SMB mounts), a run warns and goes on without the lock.
- A failed extra, ZFS snapshot or filesystem archive was only logged. The run reported success, so a part that failed every night went unnoticed. A run now sends one alert, event ID 1008 "Backup partly failed", that lists the failed parts. The exit code stays 0, because the config was saved.
- A backup folder that could not be used (for example the F: drive unplugged) failed the run with no alert. It now sends a "backup failed" alert.
- `pfs backup run -o DIR` printed DIR, but saved the files in the configured backup folder. They now go to DIR.
- `pfs backup run --non-interactive` asked where to save backups when no backup folder was set. A scheduled run then stopped or waited until Task Scheduler ended it. It now uses the default folder.
- When retention could not delete a file (on Windows, for example while antivirus scans it), it removed the record anyway. The file stayed on disk, unlisted and never removed. The record now stays, and the next run tries again.
- On Windows, saving the backup index failed when another process had it open. The save now tries again for up to 2 seconds.
- A second copy folder that was the backup folder, inside it, or held it, let the copy's cleanup delete local backups, monthly keepers too. `pfs copy setup` and every copy now refuse such a folder, also when it is the same folder under another name (a case-insensitive disk, a bind mount).
- A failed `pfs copy setup` kept the old target in config.json but stored the new secret, so the next copy failed. Setup now puts back the old value of each secret it wrote when it fails.
- An SFTP server without SFTP for the account (no `Subsystem sftp`) crashed `pfs copy setup` and `pfs copy now` with a traceback. It now gives a clear error.
- With encryption on, older plain backups made every run send a "second copy failed: turn on encryption" alert. A run now only logs them, with the hint `pfs encryption encrypt-existing`. With encryption off, the alert stays.
- A failed upload to a folder, SFTP or WebDAV target left a partial temporary file. It is now removed.
- Telegram refused an alert with HTTP 400 when its text had an odd number of `_`, `*`, `` ` `` or `[`, for example a Windows path or a device ID with one `_`. The alert was lost, with only a warning in the log. Telegram alerts are now plain text (the title is no longer bold), cut to Telegram's limit of 4096 characters.
- The daily digest covered 24 hours back from the time it was sent. That time moves (sign-in, 12:00), so an alert between two digests could be in neither, or in both. A digest now covers the time since the last digest, at most 7 days. It lists at most 10 alerts per device, the newest, and says how many earlier ones it left out.
- `pfs backup diff` crashed with a MarkupError when the config held text like `[/lan]`, and dropped text like `[b]`. Messages, errors and `pfs backup info` had the same problem. They now print square brackets as text.
- `pfs backup list` showed `unknown` in the Changes column of every extra. Only config backups are compared, so extras now show `-`.

### Changed

- The pfs and pfs.exe binaries no longer bundle setuptools. Nothing used it at run time, but its start hook loaded it on every start. The binary is about 1 MB smaller and starts slightly faster (about 7% in a Linux test).

### Added

- `pfs config set extras rrd,pkg,dhcp,aliases,certs` (or `all`, or `none`) turns on the extras for scheduled backups. Before, scheduled backups saved the config only unless you edited `config.json` by hand, and the docs did not say so. `pfs schedule status` now shows what a scheduled backup saves.
- `pfs backup verify` without a filename checks every backup with status `?`. Add `-d DEVICE` for one device.

## [1.0.0-rc.2] - 2026-11-20

The second release candidate. It fixes a data-safety bug found in the rc.1 field test: after `backup_index.json` was deleted by hand, the next run started again at #001 and the existing backups vanished from `pfs backup list`. It is a GitHub pre-release; the 14-day stability gate for 1.0.0 starts again with it.

**To try it:** `pfs update install --pre` (from rc.1), then `pfs backup reindex --dry-run` to see whether any backups are missing from the index.

### Added

- The experimental container image is published to the GitHub Container Registry as `ghcr.io/nx1x/pfsentinel:<version>`, for `linux/amd64` and `linux/arm64`, with a signed build attestation. There is no `latest` tag: pin a version and its digest. It is not on Docker Hub. See [docs/docker.md](docs/docker.md).
- **`pfs backup reindex [-d DEVICE] [--dry-run]`** rebuilds a device's backup index from the backup files on disk. A readable index keeps its records and gets the files it is missing. An unreadable index is moved aside to `backup_index.json.broken-<time>`, never deleted. The checksum of each added file is computed again, and the next run number continues after the highest one found. Backup files are only read, and links out of the device folder are not followed. `--dry-run` shows what would change and writes nothing. See [Rebuild the Backup Index](docs/usage.md#rebuild-the-backup-index).

### Changed

- The Docker build stage installs only the wheel builder (hatchling and its four dependencies) from the dev lockfile, with hashes checked, not the whole dev toolchain.

### Fixed

- **A backup index that could not be read was replaced by an empty one.** If `backup_index.json` was locked, had no read permission, or was damaged, pfSentinel treated it as "no index". The next run then saved a new index with only that run, numbered #001 and marked "initial". The old records disappeared from `pfs backup list`, and `check-recent` sent a false "no recent backup" alert. The backup files stayed on disk. Now only a missing file means a new index. Any other problem stops every command that reads the index (`backup run`, `list`, `check`, `verify`, `check-recent`, retention, `copy now`, `encryption encrypt-existing` and others) with one error line and exit code 1. The file is not changed. A backup run also sends a "Backup failed" alert (1001). `check-recent` sends its alert (1003) with the title "Backup index of DEVICE cannot be read". To recover lost records, run `pfs backup reindex`.
- **A missing backup index restarted the run numbers.** If `backup_index.json` was deleted while backup files stayed in the device folder, the next run started again at #001 and the existing backups vanished from `pfs backup list`. Seen on Windows 11 on 2026-11-20 (0.3.0 and 1.0.0-rc.1). Now a run that finds an empty or missing index next to backup files first rebuilds the index from those files (as `pfs backup reindex` does), logs a warning, and continues the run numbers.
- `pfs schedule status` showed a non-zero result of the backup tasks as "informational". `pfs backup run` exits with 1 when the backup did not finish, so a failed scheduled backup looked harmless. It now shows `FAILED (exit code N)` for the backup tasks, and for the no-recent-backup check task: "a device had no recent backup; alert sent".
- README: the feature list names the 1.0 features (restore check, second copy, long-term retention, email, digest, Event Log and syslog, the attestation check).
- The backup index is now written atomically: to a temp file in the same folder, flushed to disk, then swapped in one step. The previous version is kept as `backup_index.json.bak`. On Linux and macOS, the index is owner-only (0600).

## [1.0.0-rc.1] - 2026-11-20

The release candidate for 1.0: everything planned for 0.4.0, 0.5.0 and 0.6.0 in one pre-release. It is published as a GitHub pre-release, so `pfs update install` keeps users on 0.3.0 unless they ask for it. 1.0.0 follows after it runs 14 days in a row without a failed backup.

**To try it:**

1. Run `pfs update install --pre` (or `pip install --pre --upgrade pfsentinel`).
2. Run `pfs --version`. Expected: `pfSentinel v1.0.0-rc.1`.
3. Optional: `pfs notify email setup`, `pfs notify digest enable --time 08:00`, `pfs copy setup`.

S3, Azure, WebDAV and the container image are EXPERIMENTAL in this release.

### Added

- **Email alerts through any SMTP server:** STARTTLS (587), implicit TLS (465), or no encryption for a local server such as Mailpit. The login is optional, and the password is kept in the OS keystore, never in config.json; a login over an unencrypted connection is refused. TLS certificates and host names are always checked; for a lab server with its own CA, `--ca-cert FILE` adds that CA. Set up with `pfs notify email setup`
- **Daily digest:** one message a day for every enabled device, with the last successful backup, the number of backups and the problems of the last 24 hours. While it is on, successful backups send no message of their own; failures and risky changes are still sent at once. `pfs notify digest enable --time 08:00`. New event ID 1007
- **Linux desktop notifications** through `notify-send`, on by default like the Windows toast; nothing is shown without a desktop session. `pfs notify desktop enable|disable|status`
- **`age` encryption mode:** `pfs encryption setup --mode age` writes backups as standard age v1 files (X25519), encrypted to pfSentinel's own age identity (kept in the OS keystore, shown once) and to your own `age1...` keys (`--recipient`, repeatable). The `age` tool opens them without pfSentinel. Built with `cryptography` only and tested against the 68 applicable C2SP CCTV age test vectors; checked by hand against the `age` 1.3.2 tool. `pfs backup decrypt --identity` and `--identity-file` open them on another PC
- **Second copy to S3-compatible storage** (`kind: s3`, EXPERIMENTAL): AWS S3, Backblaze B2, Linode/Akamai, Cloudflare R2, Wasabi, DigitalOcean Spaces, Hetzner, Scaleway, MinIO and Google Cloud Storage (XML API, HMAC keys). Signature Version 4 without an SDK, checked against AWS's published test values; the secret key stays in the credential store
- **Second copy to Azure Blob Storage** (`kind: azure`, EXPERIMENTAL), with a container SAS token (recommended) or the account key
- **Second copy to a WebDAV folder** (`kind: webdav`, EXPERIMENTAL), for example Nextcloud, ownCloud or Synology: each file is uploaded under a temporary name and then renamed; https only unless you allow http for a LAN server. Only encrypted files are copied, for every target. Guide: `docs/second-copy.md`
- **Container image (EXPERIMENTAL):** a `Dockerfile` (Python 3.13 slim, pinned by digest, hash-verified dependencies, non-root UID 10001, read-only root filesystem), a Kubernetes CronJob and a Compose example in `deploy/`, and `docs/docker.md`. Not published to a registry yet. A CI workflow builds and smoke-tests the image

### Fixed

- **Windows: piped or redirected output crashed** with `UnicodeEncodeError` on the first symbol (for example `pfs backup list | Select-String ...` or output to a log file): Windows uses the legacy code page for a pipe. pfs now writes UTF-8 when its output is not a terminal. Found by the new binary smoke test
- **A corrupt `config.json` was treated as empty**, so `pfs config validate` called it valid and the next command that saved the config wrote empty defaults over your devices. pfSentinel now stops with one clear message (what is wrong and where), exit code 1, and leaves the file as it is. Found by the new CLI tests

From the 0.3.0 field test (2026-11-20, Windows 11, PowerShell 7):

- The progress bar no longer runs into one long line: Rich's `print()` removed the carriage return that brings the bar back to the line start. The bar is now written straight to the terminal, and not drawn at all when the output is not a terminal
- The `pkg` target on a non-root user gives one info line again. pfSense's bsdtar adds two harmless lines and quotes the file name with a backtick, so the 0.3.0 check fell back to a raw warning
- "logs need root" is shown once, not twice

### Security

- **`pfs update install` checks the signed build attestation** before it installs a binary: the SLSA provenance that the release workflow signs through Sigstore must name the downloaded file's SHA-256, chain to the pinned Sigstore CA, name this repository's `ci.yml` on main as the signer, and carry a valid transparency-log promise. Done with `cryptography`, no new dependency. A failed check installs nothing
- Downloads are accepted only over https from GitHub hosts, checked again after every redirect, and capped at 200 MB
- The new binary is staged next to the installed one (owner-only, same filesystem) instead of in the config folder, and a failed update leaves no staged file
- `pfs update revert` uses a backup path it derives itself, never one read from the state file
- On Windows, the config folder (config, pinned host keys, secret store) and each device's backup folder are made owner-only (`icacls`: only you, SYSTEM and Administrators), like 0600/0700 on Linux. Existing files inherit the new access list
- As root, `pfs update install` and `revert` refuse a binary in a folder that is not root-owned or that others can write

### Removed

- The unfinished terminal UI (`src/pfsentinel/tui`). It could not start (its library was never a dependency) and nothing used it. Also removed the unused `cli/list.py`, which imported a module that does not exist

### Changed

- Test coverage is 92% (was 71% in 0.3.0), mostly new CLI tests; CI fails below 80%
- CI builds the PyInstaller binary on Linux and Windows for every PR that touches the code, and runs it against a fake SSH server (`scripts/binary_smoke.py`: host key pinning, encryption, two runs, second copy, verify, restore check, decrypt). The release jobs run the same test before they upload a binary, and both systems now use one build definition (`scripts/build_exe.py`)
- mypy finds no errors, and CI now runs it in the required lint job, so a type error blocks a merge

### Added

- `pfs schedule enable` works without administrator rights on Windows: the tasks are then created for your user in the Task Scheduler root (`pfSentinel-DailyBackup`, `pfSentinel-WeeklyBackup`, `pfSentinel-BackupCheck`). From an Administrator PowerShell they still go into the `\pfSentinel` folder and the Event Log source is registered. `schedule status` and `disable` handle both. Not yet tested on a standard (non-admin) Windows account
- `--passphrase-stdin` for `pfs encryption setup --mode passphrase` and `pfs backup decrypt`: reads the passphrase from piped input, for scripts and containers (a hidden prompt on Windows reads the console directly and cannot be piped)
- `--json` output for `pfs device list` and `pfs schedule status` (for scripts). `pfs backup list --json` now prints `[]` instead of a text line when there are no backups
- **Count-drop alert** (a risky change, event 1002): firewall rules, port forwards, users, certificates, CAs or interfaces fell by 20% or more (and at least two), or to zero, since the previous config
- **Update-available alert** (event 1006, Information): the daily check task tells you once per new release. Turn it off with `notifications.update_alerts`
- `pfs update check --pre` and `pfs update install --pre` include pre-releases (release candidates). A version with a suffix (for example `1.0.0-rc.1`) is published as a GitHub pre-release, so the normal update path stays on the last stable release

### Documentation

- `CODE_OF_CONDUCT.md`: the Contributor Covenant 2.1. Reports go through the contact form at nx1xlab.dev/contact
- The docs said a backup is saved only when the config changed, and suggested `pfs backup run --force`, which does not exist. Every run saves a config backup (labelled `minor` when nothing changed); only `pfs backup watch` waits for a change

## [0.3.0] - 2026-11-20

Encryption at rest, one folder per backup run, alerts (risky changes, restore check, no recent backup, Windows Event Log), long-term retention and a second copy. Most items come from the homelab field test of 2026-11-20.

**After updating** (`pfs update install`):

1. In an administrator PowerShell, run `pfs schedule disable`, then `pfs schedule enable --daily-time 02:00` (your time). This adds the no-recent-backup check task and registers the Event Log source.
2. Run `pfs encryption setup`, store the recovery key in your password manager, then run `pfs encryption encrypt-existing --wipe` for the older backups.
3. Optional: `pfs notify eventlog enable` and `pfs copy setup`.

### Added

- **Second copy** (`pfs copy setup`): after each run, every encrypted backup the target does not have yet is copied to a folder (a share, or a Nextcloud-synced folder) or an SFTP server (key login, pinned host key). Plain files are never copied, so setup requires encryption. A failed copy does not fail the backup: it is an alert (event 1005), and the next run catches up. The target keeps its own retention (`keep_days`, default 90). `pfs copy now`, `status`, `disable`
- **Long-term retention** (`backup_policy.long_term`): besides the last 30, the newest config backup of each week is kept for 12 weeks and of each month for 12 months. These do not count toward the per-type limit and do not expire with `keep_days`. With daily backups this keeps about 48 configs. On by default, for `config` only
- **Alerts** on every enabled channel, each kind with a fixed Windows Event Log ID:
  - **Risky change** (1002): after each config backup, one alert per security-relevant change since the previous config: admin users and groups, the GUI authentication server, WAN pass rules and port forwards, SSH password login, the anti-lockout rule, and the web GUI protocol, port and certificate. Alerts name settings only, never secrets. The changes are stored with the backup
  - **Restore check** (1004): after each config backup, config.xml must parse and have users, an admin user, interfaces and a filter section; CAs and certificates are counted. `pfs backup check [FILE]` runs it on any backup
  - **No recent backup** (1003): `pfs backup check-recent` alerts when a device has had no successful backup for `stale_after_days` days (default 3). `pfs schedule enable` now also registers it at sign-in and daily: `pfSentinel\BackupCheck` on Windows, `pfsentinel-check.timer` (or cron) on Linux
  - **Windows Event Log** channel (`pfs notify eventlog enable`): Application log, source `pfSentinel`, IDs 1000 backup OK, 1001 failed, 1002 risky change, 1003 no recent backup, 1004 restore check failed, 1005 second copy failed. On Linux the same alerts go to syslog/journald. `pfs schedule enable` registers the event source
- **Encryption at rest** (`pfs encryption setup`). Every backup file (config, extras, ZFS, archive) is written as AES-256-GCM `<name>.enc` in 64 KiB authenticated chunks, so a changed, cut or reordered file fails to decrypt. Two modes: `keystore` (a random key in the OS keystore, plus a recovery key shown once) and `passphrase` (scrypt). Both work for scheduled runs without input. `list`, `verify`, `diff` and `restore` decrypt on the fly. New commands: `pfs encryption status`, `recovery-key`, `disable`, `encrypt-existing [--wipe]` (encrypts older plain backups, checks them, then deletes the plain files), and `pfs backup decrypt FILE --recovery-key | --passphrase` for another PC. `docs/encryption.md` documents the format and has a standalone decryption script; a test keeps it working. Encryption is off by default
- Extras (RRD, aliases, certificates, logs, DHCP) are read into memory instead of a temporary folder, so certificate keys and the rest never sit in a plain temporary file, with or without encryption
- **One folder per backup run:** `<device>/YYYY/MM/DD/<HHMMSS>_#NNN/` holds the config and every extra of that run. Before, the config went to `<device>/YYYY/MM/DD/` and each extra to `<device>/<type>/YYYY/MM/DD/`. `NNN` is now a run number that counts the device's runs and never resets, instead of a daily sequence. `backup list` shows it in a "Run" column, which replaces the row number. Backups made before 0.3.0 stay where they are and keep working with list, verify, diff, restore and retention, and new run numbers continue after them. Retention removes run folders that it empties. The backup index moves to schema 3 on first use

### Security

- Bandit and zizmor in `security.yml` are installed with `--require-hashes --only-binary :all:` from a new lockfile, `ci/security-tools.lock`, instead of a version range. Wheels only means no package's setup code runs at install time. A changed or compromised release can no longer run in CI. This fixes the two open OpenSSF Scorecard "Pinned-Dependencies" findings. OSV-Scanner and Renovate cover the new lockfile
- soupsieve 4.0.1 (dev only, GHSA-mmhr-3c5m-f64f, high) and OpenTelemetry 1.45.2 in the Dagger CI module (GHSA-mf5v-w3w3-f6cg, medium)

### Changed

- Dependencies updated within the 14-day release cooldown: pydantic 2.14.3, typer 0.27.3, paramiko 5.1.0, cryptography 50.0.2, lxml 6.1.3, keyring 25.7.0, packaging 26.3, certifi 2026.10.23; dev tools ruff 0.17.4, pytest 9.2.0, mypy 2.4.1, hypothesis 6.170.14, hatchling 1.33.0, types-paramiko 5.1.0. The floors in `pyproject.toml` match
- CI: github/codeql-action 4.39.3, Dagger engine 0.21.11, and uv_build 0.13.7 for the Dagger module
- `renovate.json` says why the dashboard always shows "pip-compile: dependency not found in lock file": it is `requires-python`, which no lockfile contains

### Fixed

- Change detection compared a new config with the newest backup of any type, which can be an extra (a tar file) of the same run; it now always compares with the newest config backup

Found in the homelab field test of 2026-11-20 (v0.2.6, Windows 11, pfSense CE 2.8.1, non-root backup user):

- `pfs device add` checks that the host resolves before it saves the device, and asks again if it does not. The prompt now says "IP address or DNS name". Before, a device ID typed as the host was saved, and every method then failed with `getaddrinfo failed`
- `pfs device add` asks for the SSH key passphrase only when the key is encrypted, and says so when it is not
- `pfs device test` shows HTTPS and HTTP as skipped, not failed, on a device that logs in with an SSH key and has no password
- `pfs device test` shows a clear warning, with the fix, when a device's SSH host key is not pinned (devices added before 0.2.0). paramiko's raw `UserWarning` with the key bytes is no longer printed
- A non-root SSH user no longer gets an ERROR for root-only files. The `logs` target is skipped with one "logs need root" line, and root-only files in `pkg` (such as `sasldb2.db`) are listed in one info line
- `aliases` no longer warns about alias folders that do not exist on the pfSense version in use (on 2.8.1, only `/var/db/aliastables` exists)
- `pfs schedule status` shows "Last run: never" and "not run yet" for a task that has not run, instead of `30/11/1999` and result `267011`. A running or disabled task is named as such
- Log lines no longer start in the middle of the progress bar during `pfs backup run`. A message after an unfinished bar also starts on a new line
- The trust-key hint names the host key file that matches the key type, and says to run it on the firewall. `SECURITY.md` no longer shows a bash `for` loop, which fails in the pfSense shell (tcsh)

### Documentation

- `docs/extended-backups.md` has a table of which extras work for a non-root SSH user on pfSense 2.8.1

## [0.2.6] - 2026-11-08

Fixes SSH for users with an SSH host CA in `~/.ssh/known_hosts`.

### Changed

- CI runs on main are no longer cancelled by a later merge. A release run waits for its PyPI approval, and a merge during that wait cancelled the v0.2.5 publish. Only pull-request runs cancel superseded runs now
- **Renovate now updates the lockfiles.** Two header options in the lockfiles (`-o` and `--extra dev`) made Renovate's pip-compile manager reject both files, so for months its PRs raised floors in `pyproject.toml` but never touched `requirements.lock` or `requirements-dev.lock`. The headers now use `--output-file=` and `--extra=dev`, and a test keeps them that way
- Renovate rules matched the wrong manager: they named `pep621`, but the root `pyproject.toml` was read by the `poetry` manager. Grouping and automerge never applied, so every package got its own PR. The rules now target `pip-compile`, which owns the root project and its lockfiles. The `poetry` manager is off, and `pep621` handles only the Dagger module. A dry run shows 6 update branches where the dashboard listed about 25
- Python versions (requires-python, CI `python-version`, Dagger module) are no longer bumped by Renovate. They change by hand once lxml, cryptography and paramiko have wheels for the new version. This removes the errored `python 3.15.1` update
- Lockfile maintenance runs monthly and no longer automerges. It re-resolves every pin at once, so the 14-day release cooldown cannot apply to it
- `CONTRIBUTING.md` documents the exact lockfile commands, with the cooldown passed as `UV_EXCLUDE_NEWER`

### Fixed

- An `@cert-authority` line in `~/.ssh/known_hosts` (an SSH host CA) no longer breaks SSH connections. paramiko's loader stopped at that line with "Incorrect padding", so every device without a pinned key failed. pfSentinel now reads that file itself: it skips `@cert-authority` lines and never loads a key that a `@revoked` line lists. paramiko cannot check host certificates, so pin a CA-signed host with `pfs device trust-key`
- The SSH troubleshooting hint no longer tells Windows users to give a Linux key path


## [0.2.5] - 2026-11-07

Fixes from a security review of the code shipped since 0.2.0.

### Security

- **Notification credentials no longer reach logs or the console.** A Telegram bot token travels in the request URL, and a Slack webhook URL is itself the credential. When a send failed, `requests` quoted that URL in its error, and pfSentinel printed the error to the console and to the scheduled-run log. Errors now show only the HTTP status or the error type, and every log line passes through a filter that redacts bot tokens and webhook paths, tracebacks included
- **The Slack webhook host is enforced.** A webhook URL that is not on `hooks.slack.com` was only warned about and still received backup details. pfSentinel now refuses to send there, and `pfs notify slack setup` refuses to save it. Notification requests no longer follow redirects
- **A key pinned with `pfs device trust-key` now always wins.** paramiko checks the system and `~/.ssh/known_hosts` keys before the client's own, so a stale OpenSSH entry for the same host overrode the pin, and re-trusting could not fix the mismatch. For a trusted device, only `~/.pfsentinel/known_hosts` is used now
- **Re-trusting replaces every old key for the host.** paramiko removes only the first matching entry, so a second key type could stay pinned. The pinned-keys file is now written atomically and is owner-only from the moment it exists
- An untrusted host key now raises pfSentinel's own error directly, instead of being recognised from paramiko's English message text. The rule that a host key problem never falls back to HTTPS no longer depends on that wording
- Scheduled jobs: a line break or NUL in the command path is rejected instead of writing extra lines into the systemd unit or crontab. A `$` in the path is written as `$$`, because systemd expands variables even inside quotes. The cron log is created owner-only (0600, folder 0700)
- Windows: times read from `config.json` are validated before they reach the Task Scheduler XML, as on Linux already
- Release files, including the `pfs` and `pfs.exe` binaries, now carry signed build provenance. Check a download with `gh attestation verify pfs.exe --repo NX1X/pfSentinel`
- OSV-Scanner now checks all three lockfiles (runtime, development, Dagger CI), not only the runtime one

### Fixed

- Windows: a schedule time with a one-digit hour, such as `2:05`, produced an invalid Task Scheduler start time (`T2:05:00`). Times are now zero-padded
- A schedule time with a non-ASCII digit, such as `²:00`, crashed with a traceback. It is now rejected with a clear message
- `child_env()` now restores only PyInstaller's library-path variables, and only in a frozen build. It used to rewrite any `*_ORIG` variable, including ones that belong to the user. It also drops a library path that points into the bundle when PyInstaller saved no original

### Changed

- The `publish-pypi` job no longer requests `attestations: write`; trusted publishing needs only `id-token: write`
- `SECURITY.md` describes the current host key behavior. It still described the pre-0.2.0 permissive default and a `--strict-host-keys` option that does not exist


## [0.2.4] - 2026-11-07

### Security

- Update `certifi` to 2026.10.23 (GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320, critical). `certifi` reaches pfSentinel through `requests`. It is now a declared dependency with a floor of `>=2026.10.18`, so a lockfile update cannot fall back below the fix
- Update `urllib3` to 2.8.0 (PYSEC-2026-4175, PYSEC-2026-4176, PYSEC-2026-4177). The floor is now `>=2.8.0`
- Update the PyPI publish action, `pypa/gh-action-pypi-publish`, from 1.14.2 to 1.14.3 (GHSA-5xjx-3w67-w9x9, critical). Version 1.14.2 trusted a revoked certificate authority in the chain that Sigstore uses for Trusted Publishing and attestations
- Update the Dagger CI module lockfile: OpenTelemetry 1.44.0 to 1.45.1 (GHSA-xcq7-p3pf-p88h), `certifi` to 2026.10.23 and `multidict` to 6.9.1 (PYSEC-2026-4244). These packages run only in CI, not in pfSentinel
- The Security workflow (OSV-Scanner, Bandit, zizmor) now runs every day instead of once a week. The weekly scan let the critical `certifi` advisory go unreported for days

### Changed

- Runtime dependencies: `cryptography` 50.0.2, `paramiko` 5.1.0, `pydantic` 2.14.1, `typer` 0.27.3
- Development tools: `ruff` 0.17 (the allowed range moves from `<0.17` to `>=0.17,<0.18`), `mypy` 2.4, `hypothesis` 6.170, `pyinstaller` 6.22.3
- GitHub Actions, each pinned by commit SHA: `setup-python` 7.1.0, `upload-artifact` 7.0.2, `download-artifact` 8.0.2, `codecov-action` 7.1.1, `codeql-action` 4.39.1, `osv-scanner-action` 2.7.0
- Renovate no longer proposes changes to `requires-python`. It suggested `>=3.15.0` for the Dagger module, which no Python in CI satisfied, so every Dagger CI run on that branch failed


## [0.2.3] - 2026-09-21

### Fixed

- **Self-update of a binary install failed at the last step with "New binary failed version check".** After verifying the download, the updater runs the new executable once to confirm it starts, but it inherited the running build's PyInstaller variables (`_MEIPASS2`, `_PYI_*`, and rewritten loader paths such as `LD_LIBRARY_PATH`). A one-file binary that sees those believes it is already unpacked and refuses to start, so a perfectly good download was rejected and rolled back; the same file ran fine from a shell. Every program pfSentinel spawns now gets a cleaned environment, with the caller's original loader variables restored from PyInstaller's `<NAME>_ORIG` copies
- When the new binary really does fail to start, the error now names the exit code and the program's own message instead of only "failed version check", and the check allows 120 seconds instead of 10, since a 30 MB one-file binary can take that long to unpack and clear an antivirus scan on first run


## [0.2.2] - 2026-09-21

### Fixed

- **Self-update from a binary install failed with "Checksum for 'pfs.exe' not found in release checksums file".** The release job generated the checksums with `sha256sum ./*`, so every name carried a `./` prefix, while the updater matched the asset name exactly. Releases now write bare filenames, and the updater normalises what it reads (`./name`, `*name` from binary mode, or a directory prefix), so a release built either way verifies. v0.2.1 itself cannot be repaired (GitHub releases are immutable), so 0.2.0 and 0.2.1 binary installs update to 0.2.2 instead


## [0.2.1] - 2026-09-21

### Fixed

- `pfs schedule status` now explains the Windows Task Scheduler result codes it can diagnose: `0x80070002` (the task points at a pfs executable that no longer exists, typically one registered by an older version), `0x80070005` and `0x80070057`, each with the command that fixes it. Previously only `0x80070057` was explained and the rest showed as a bare hex code
- The default backup directory is now defined in one place. `BackupPolicy.resolved_root` hardcoded `~/Documents/pfSentinel` while `utils.platform.default_backup_dir()` returned `~/pfSentinel` on Linux and was never called, so the documented Linux path was wrong. Behavior is unchanged (`~/Documents/pfSentinel` on both systems); the docs now match it

## [0.2.0] - 2026-09-19

0.1.4 and 0.1.5 were prepared but never published: the release jobs need a green Windows test run, and Windows CI was red from 2026-07-11 until this release. Their changes ship here. 0.1.4 also has a GitHub release for the record; neither version is on PyPI, so install 0.2.0.

### Fixed

- **Scheduled backups no longer stop at the backup-type menu.** `pfs backup run` without flags always showed the interactive "What would you like to back up?" menu, and a scheduled run has no one to answer it, so every scheduled backup on every platform aborted there. Scheduled tasks now run `pfs backup run --non-interactive`, which uses the backup policy from `config.json`, and a run without a terminal on stdin never prompts either
- **Scheduled backups on Linux and macOS now actually run.** `pfs schedule enable` used to start a background thread inside the CLI process, which ended as soon as the command returned, so no scheduled backup ever fired. It now installs systemd user timers (`pfsentinel-daily.timer`, `pfsentinel-weekly.timer`, with `Persistent=true` so a run missed while the machine was off happens at next boot), and falls back to cron where there is no systemd user session (WSL without systemd, containers, macOS). pfSentinel's crontab lines are tagged and replaced in place; the rest of the crontab is left alone. `pfs schedule disable` removes both. `pfs schedule status` shows the timer state, next run and last result, and warns when the config says enabled but nothing is installed
- **Scheduled backups on Windows can now read the device password.** Tasks were registered with `LogonType=S4U`, which has no access to the user's DPAPI-protected data, and Windows Credential Manager (where pfSentinel keeps passwords) is DPAPI-protected. Tasks now use `LogonType=InteractiveToken`: they run while you are signed in (a locked screen counts), and a run missed while signed out starts at the next sign-in (`StartWhenAvailable`)
- **Windows toast notifications work.** They depended on `winotify`, which was only in the dev extras, so released builds silently skipped every toast while reporting it as sent. Toasts now go through the WinRT API using the built-in Windows PowerShell, with no extra package. Notification text is passed through environment variables and XML-escaped, never interpolated into the script, and PowerShell is started by absolute path
- `pfs schedule enable` validates `--daily-time`, `--weekly-time` and `--weekly-day` before writing anything. Previously an invalid time was written into the Task Scheduler XML as-is
- Windows CI had been failing since July, which held back the 0.1.4 and 0.1.5 releases (the release jobs require a green test run). The cause was a test, not the parser: the XXE test built `file://C:\...`, which is not a valid URI, so libxml2 rejected it before the DOCTYPE check. The test now uses a proper file URI
- Pressing Ctrl+C at an interactive prompt no longer produces an unhandled traceback. typer 0.26 vendored click, which means `typer.Abort` is no longer the same class as the external `click.Abort` - so all eight abort handlers in `pfs notify` and `pfs device add` had silently become dead code. They now catch `typer.Abort`
- Align the runtime and dev lockfiles. `requirements.lock` resolved typer 0.25.1 while `requirements-dev.lock` resolved 0.27.0, so the test suite exercised a different (click-vendoring) typer than users installed - which is exactly why the abort bug went unnoticed. The `typer` floor is now `>=0.26` so the vendored-click assumption holds for every supported version

### Security

- **Strict SSH host key checking is now the default** (closes #3). New devices refuse an SSH server whose key has not been confirmed once, which blocks man-in-the-middle attacks on the connection that carries the pfSense admin password. `pfs device add` shows the key's SHA256 fingerprint and asks you to trust it; `pfs device trust-key <id>` does the same later (for example after reinstalling pfSense). Trusted keys live in `~/.pfsentinel/known_hosts`, separate from `~/.ssh`. A changed key is refused with a clear possible-MITM error, and neither a changed nor an untrusted key falls back to HTTPS. Devices saved by earlier versions keep their explicit `strict_host_keys: false` until you run `trust-key`, so upgrading breaks no existing backups
- Bump `cryptography` to `>=50.0.1,<51` (GHSA-g6cj-pr64-35w5 / CVE-2026-69247, a Bleichenbacher padding oracle in PKCS#7 decryption; also covers GHSA-537c-gmf6-5ccf, the vulnerable OpenSSL bundled in wheels before 48.0.1). pfSentinel only uses AES-GCM, so the vulnerable path was not reachable, but the floor keeps it that way on future lock regenerations
- Remove the unused `httpx` dependency, which pulled in `anyio` 4.13.0 (CVE-2026-63374, CVE-2026-64847). Nothing in pfSentinel imported `httpx`; the HTTPS connector uses `requests`
- Add `TestLockfileConsistency`, which fails if `requirements.lock` and `requirements-dev.lock` pin a shared package to different versions, or if the dev lock is missing a runtime package. Both locks are now regenerated together
- Replace the unmaintained `defusedxml` dependency with a hardened `lxml` parser for `config.xml`. `defusedxml` has had no release since 0.7.1 (March 2021) and no upstream commit since October 2023, which is not a safe position for the component that guards pfSentinel's only untrusted input. `lxml` was already a dependency, so this removes a package rather than swapping one. The replacement disables entity resolution, network access, DTD loading and huge trees, and additionally rejects any config carrying a DOCTYPE so entity attacks fail loudly instead of parsing with unresolved references
- Add `TestXxeHardening` covering classic XXE file disclosure, entity-expansion ("billion laughs"), external DTD, network entity, and blind-XXE parameter entities. The previous `defusedxml` protection had no test coverage at all, so this is the first time the XML security boundary is actually verified
- Add property-based fuzzing for the XML parser (`hypothesis`). `validate_xml` is pfSentinel's only untrusted-input boundary, so the suite asserts one invariant across arbitrary text, arbitrary bytes, generated XML documents, deep nesting and XML-metacharacter soup: parsing either returns a well-formed pfSense root or raises `PfSenseXMLError`, and nothing else escapes. Verified to catch a real regression - re-enabling entity resolution makes the suite fail with actual file contents in the assertion output
- Remove an ineffective SSH hardening setting. `SSHConnector` passed `disabled_algorithms={"pubkeys": ["rsa-sha1", "ssh-rsa"]}`, but paramiko only filters against algorithms present in its preference tuple, and paramiko 5.0.0 offers only Ed25519, ECDSA and RSA-SHA2 - so the setting filtered nothing while reading as protection. Replaced with `TestParamikoPubkeyAlgorithms`, which asserts no SHA-1 public-key algorithm is offered and fails loudly if a future paramiko reintroduces one
- Add `tests/unit/test_dependency_contracts.py` pinning both dependency assumptions above, so neither can regress silently
- Replace the `keyrings.alt` fallback with an own AES-256-GCM encrypted file store (`services/secret_store.py`), built on `cryptography` which was already a dependency. This fixes a real functional bug as well as removing a package: the previous fallback used `keyrings.alt`'s `EncryptedKeyring`, which **prompts for a master password on every access** - so on WSL, headless Linux and containers, scheduled backups could not retrieve credentials unattended. The new store never prompts. Secrets are sealed as a single ciphertext (so the file does not leak which keys exist), the key is 32 random bytes at 0600 in a 0700 directory, writes are atomic, and tampering is caught by the GCM authentication tag. Its threat model is documented in the module docstring: it defends against casual disclosure (backup archives, screenshares, accidental commits) but not against an attacker already executing as the same user - which is unavoidable while unattended backups must decrypt without a human
- Isolate the encrypted store in tests. Constructing a `CredentialService` without a keyring previously wrote a real vault into the developer's `~/.pfsentinel/store` during the test run; an autouse fixture now redirects it to a tmp directory
- Replace the abandoned `schedule` package with stdlib `threading` + `datetime` for the in-process scheduler. Besides removing a dependency with no upstream commit in two years, this fixes a latent flaw: the old loop polled every 30 seconds, so `pfs schedule disable` (and process shutdown) could block for up to that long. The new loop waits on a `threading.Event` until the next run is actually due, so stopping is immediate. The schedule arithmetic is now pure functions (`next_daily_run`, `next_weekly_run`), covered by 15 new tests - previously untestable because `schedule` owned the clock internally
- Replace `loguru` with stdlib `logging` (`utils/logging.py`). This also makes `log_level` in `config.json` do something: it was declared with a default of `INFO` but never applied, because loguru used its own default regardless. `configure_logging()` now runs at CLI startup with the configured level, falling back to `INFO` if the config cannot be read
- The TUI log screen's loguru sink is now a `logging.Handler`. Console and TUI levels are tracked separately, so opening the log screen (which forces DEBUG so the widget sees everything) no longer floods stderr with debug output

### Added

- CI pipeline defined as a [Dagger](https://dagger.io) module (`.dagger/`, `dagger.json`): `dagger call check --source=.` runs lint, tests, pip-audit, Bandit and the package build in a digest-pinned container, identically on a laptop and in the new `Dagger CI` workflow. See `docs/ci-dagger.md`
- `pfs device trust-key <id>` (`--yes` to skip the prompt) to trust or re-trust a device's SSH host key
- `pfs docs` opens the online manual at https://pfs.nx1xlab.dev/docs, and `pfs --help` links to it. The manual has every command, recipes that combine them, and a plain-text version (`/llms-full.txt`) you can paste into an AI assistant

### Changed

- Runtime dependency majors: `rich` 15, `packaging` 26
- Dev tooling majors: `mypy` 2, `pytest-cov` 7, `twine` 7, `types-paramiko` 5, `ruff` 0.16. The abandoned `lxml-stubs` is replaced by `types-lxml`
- GitHub Actions majors, each pinned by commit SHA: `checkout` 7, `setup-python` 7, `upload-artifact` 7, `download-artifact` 8 (downloads now fail on a digest mismatch instead of warning), `attest-build-provenance` 4, `dependency-review-action` 5, `codecov-action` 7, `codeql-action` 4, `osv-scanner-action` 2.5.1, and `gh-action-pypi-publish` pinned to v1.14.2 instead of the moving `release/v1` branch
- Lockfiles are resolved with the same 14-day release-age cooldown Renovate applies, so a freshly published (possibly compromised) version cannot enter through a lock regeneration
- `requirements.lock` is now compiled with `uv pip compile --universal`, so the runtime lock is valid on Windows as well as Linux (it gained the Windows-only `colorama` and `pywin32-ctypes` entries with platform markers)
- CI re-emits each failing test, plus the first assertion detail, as a GitHub `::error::` annotation. Annotations are readable through the check-runs API, which works from networks that block the raw log download
- Renovate config fixes: the top-level `abandonmentThreshold` was silently overridden by the `abandonments:recommended` preset and never applied, so it is removed; the CI Python rule no longer tries to combine `allowedVersions` with `matchUpdateTypes` (Renovate rejects that), patch rewrites of the floating `3.14` pin are disabled, and pre-releases are excluded; a dead ruff regex manager left over from an old CI step is removed

### Removed

- The README no longer lists Windows Event Log notifications; the setting exists but was never implemented (tracked on the roadmap)
- `defusedxml` is no longer a runtime dependency
- `httpx` and `pyyaml` are no longer dependencies (neither was imported anywhere), along with `types-pyyaml` from the dev extras
- `loguru` is no longer a dependency
- `schedule` is no longer a dependency
- `keyrings.alt` is no longer a dependency (see the encrypted file store above). `keyring` itself stays - it is actively maintained and still provides the preferred OS-backed path on Windows and Linux desktops
- `click` is no longer a direct dependency. It was declared only because the CLI imported it for `click.Abort`; now that the handlers correctly catch `typer.Abort`, typer's vendored copy is the only one needed. The PyInstaller `--hidden-import click` workaround added in 0.1.5 is removed with it
- Remove the OpenSSF Scorecard CI job. Most of its findings were not actionable for a single-maintainer repo: `Branch-Protection` was a false negative (Scorecard reads the legacy branch-protection API and cannot see the repository rulesets that are actually enforcing 11 required checks, no force-push and no deletion), and `Code-Review` scores the absence of a second reviewer. Bandit, CodeQL, zizmor, OSV-Scanner, dependency-review and pip-audit all still run - Scorecard was scoring posture, not finding vulnerabilities. It may be re-added later
- `winotify` is no longer a dependency (see the Windows toast fix above)

## [0.1.5] - 2026-07-12

Never published to PyPI (see 0.2.0). Depends on `cryptography<49` (GHSA-g6cj-pr64-35w5), and its scheduled backups do not run on Linux or read credentials on Windows.

### Fixed

- **Release binaries now start correctly.** `click` is now declared as an explicit dependency. `typer` 0.26 stopped pulling `click` in transitively, but the CLI imports `click` directly (`notify`/`device` commands), so the standalone PyInstaller binaries crashed on launch with `ModuleNotFoundError: No module named 'click'`. This broke the Linux release build (and would have shipped a broken Windows `.exe`, which had no smoke test). `click` is now also an explicit PyInstaller hidden-import and the Windows build has a smoke test.

### Security

- Bump `paramiko` 4.0.0 → 5.0.0 (fixes CVE-2026-44405 / GHSA-r374-rxx8-8654: SHA-1 signature verification weakness). This removes the temporary `pip-audit` / OSV-Scanner ignore for that advisory that was in place while no fixed paramiko release existed
- Bump `pytest` 8 → `>=9.1.1,<10` (dev/test dependency; CVE-2025-71176 / GHSA-6w46-j5rx-g56g: predictable `/tmp/pytest-of-{user}` paths on UNIX allow a local user to cause a denial of service or possibly escalate privileges). Fixed upstream in 9.0.3; the floor is pinned so a lock regeneration cannot drift back onto a vulnerable 9.0.x
- Bump `requests` → `>=2.34.2,<3` (precautionary security update)

### Changed

- Add a smoke test to the Windows binary build so a non-runnable `.exe` can no longer pass CI
- Relocate the Renovate config to `.github/renovate.json` and migrate the deprecated `fileMatch` fields to `managerFilePatterns` (fixes the Renovate "pip-compile: dependency not found in lock file" repository warning)

## [0.1.4] - 2026-07-11

Never published to PyPI (see 0.2.0). Crashes on launch with current `typer` (missing `click`), and depends on `paramiko<5` (CVE-2026-44405) and `cryptography<49` (GHSA-g6cj-pr64-35w5).

### Fixed

- Scheduled Windows tasks failed silently every run with `ERROR_INVALID_PARAMETER` (`0x80070057`) due to a double-quoted command line in the task registration. As a result, daily and weekly backups created via `pfs schedule enable` did not execute on Windows.
- `pfs schedule status` now reports the live Task Scheduler state - last run time and last run result - for **both** the daily and weekly tasks (previously only the daily task was shown, and a task failing every run with `0x80070057` was still displayed as healthy). A failed last result is now flagged with remediation guidance instead of appearing as "Created".
- An invalid `--weekly-day` value (typo or unexpected input) no longer produces a malformed Task Scheduler XML element name; unrecognized days now fall back to Sunday so weekly task registration cannot fail on bad input.

### Security

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
