# Windows and Linux

pfSentinel works the same way on Windows and Linux. CI runs every test on both: Ubuntu and Windows, with Python 3.13 and 3.14. macOS is best effort: CI does not test it.

## When the PC is off, asleep or signed out

A scheduled backup runs on the PC where pfSentinel is installed, so the state of that PC decides when the backup runs. The table is for the daily and weekly backup tasks.

| State of the PC at backup time | Windows (Task Scheduler) | Linux (systemd user timer) |
|---|---|---|
| On, you are signed in (a locked screen counts) | Runs on time. | Runs on time. |
| Asleep | The task asks Windows to wake the PC (`WakeToRun`). This works only if the power plan allows wake timers. If the PC does not wake, the run starts when it wakes up. | The run starts when the PC wakes up (`Persistent=true`). systemd does not wake the PC. |
| On, but you are signed out | Does not run. The missed run starts at your next sign-in (`StartWhenAvailable`). | Runs on time only with lingering on (`loginctl enable-linger $USER`). Without it, the run starts at your next login. |
| Off | The missed run starts at your next sign-in. | The missed run starts after the next boot, with lingering on. Without lingering, it starts at your next login. |

More things to know:

- A missed daily run and a missed weekly run can start together. A lock file (`.pfsentinel.lock` in each device folder) makes the second run wait until the first is done. It waits at most 40 minutes. Some network shares (SMB, NFS) cannot lock. The run then goes on without the lock, and pfSentinel sends alert 1009 at most once in 7 days per device. A manual run also shows it on screen.
- The no-recent-backup check runs daily at 12:00. On Windows it also runs 5 minutes after each sign-in. On Linux, `pfsentinel-check.timer` also runs it 5 minutes after your systemd user session starts (at login, or at boot with lingering on). If no backup succeeded for `notifications.stale_after_days` days (default 3), it sends an alert. This catches a PC that stayed off for days.
- On Windows, the tasks run as you, with your normal rights. They do not need an administrator. Each task has a time limit of 1 hour.
- On a Linux desktop, the desktop keyring stays locked until you log in, so a run before login cannot read the passwords. On a headless system pfSentinel uses its encrypted file store, which works without a login.
- `pfs backup history` lists every run with its result. A run that the PC stopped half way (shutdown, the 1 hour task limit) shows as `No end`. A run that never started, because the PC was off, is not in the history.
- Cron (when systemd is not there, for example in WSL without systemd or in a container) has no catch-up. A run missed while the machine was off does not start later.

## Features on each system

Each row is one command group. A CI test (`tests/unit/test_platform_parity.py`) reads this table. It fails when a command group is missing from the table, or when a feature is not marked as working on both systems. So a feature for one system only cannot slip in without notice.

| Commands | Windows | Linux | How it differs |
|---|---|---|---|
| `pfs backup` | Yes | Yes | Same on both. |
| `pfs config` | Yes | Yes | The config is `%USERPROFILE%\.pfsentinel\config.json` on Windows and `~/.pfsentinel/config.json` on Linux. `pfs config path` shows it. |
| `pfs copy` | Yes | Yes | Same on both. |
| `pfs device` | Yes | Yes | Same on both. |
| `pfs docs` | Yes | Yes | Same on both. |
| `pfs encryption` | Yes | Yes | The key is in Windows Credential Manager on Windows. On Linux it is in the desktop keyring (Secret Service), or in the encrypted file store on a headless system. |
| `pfs list` | Yes | Yes | Same on both. |
| `pfs notify` | Yes | Yes | Same on both. |
| `pfs notify desktop` | Yes | Yes | A Windows toast on Windows. On Linux, a notification through `notify-send`. It shows only when a desktop session is running. |
| `pfs notify digest` | Yes | Yes | Same on both. |
| `pfs notify email` | Yes | Yes | Same on both. |
| `pfs notify eventlog` | Yes | Yes | The Windows Event Log (Application log, source `pfSentinel`) on Windows. To register the source, run `pfs notify eventlog enable` once in an Administrator PowerShell. Syslog or journald (tag `pfsentinel`) on Linux. |
| `pfs notify slack` | Yes | Yes | Same on both. |
| `pfs notify telegram` | Yes | Yes | Same on both. |
| `pfs schedule` | Yes | Yes | Task Scheduler on Windows. systemd user timers on Linux, or cron when systemd is not there. |
| `pfs setup` | Yes | Yes | Same on both. |
| `pfs status` | Yes | Yes | Same on both. |
| `pfs update` | Yes | Yes | Replaces `pfs.exe` on Windows and the `pfs` binary on Linux. A pip or pipx install is updated with pip or pipx. |
