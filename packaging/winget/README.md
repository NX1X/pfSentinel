# winget package (NX1X.pfSentinel)

These three files are the template of the winget manifest. winget installs `pfs.exe` as a portable program: it downloads the file from the GitHub release, checks its SHA-256 and puts `pfs` on the PATH.

pfSentinel is on winget from 1.0.0 on. Release candidates are not published there: winget has no pre-release channel.

Status: not submitted yet. The first submission is made by hand when 1.0.0 is out.

## Fill in the template

Each release needs three values:

| Placeholder | Value | Where it comes from |
|---|---|---|
| `{{VERSION}}` | for example `1.0.0` | the release tag without `v` |
| `{{RELEASE_DATE}}` | for example `2026-12-04` | the date of the GitHub release |
| `{{SHA256}}` | 64 hex characters, upper case | the `pfs.exe` line in `checksums-sha256.txt` of the release |

Copy the three files to a new folder, and replace the placeholders. Do not change the file names.

## Test a manifest on a Windows PC

1. Open PowerShell as administrator, and run `winget settings --enable LocalManifestFiles`. This is needed once.
2. Run `winget validate --manifest <folder>`. Expected: `Manifest validation succeeded.`
3. Run `winget install --manifest <folder>`.
4. Open a new PowerShell window, and run `pfs --version`.
5. Run `pfs update check`. If there is a newer release, the output names `winget upgrade NX1X.pfSentinel`.
6. Run `pfs schedule enable`, then `pfs schedule status`. Expected: the tasks are registered.
7. To remove the test install, run `pfs schedule disable`, then `winget uninstall NX1X.pfSentinel`.

## Submit

1. Fork `microsoft/winget-pkgs`.
2. Put the three filled-in files in `manifests/n/NX1X/pfSentinel/<version>/`.
3. Open a pull request. On the first one, sign the contributor agreement that the bot asks for.
4. Wait for the validation pipeline. It installs the package and scans `pfs.exe` with antivirus engines.

After the first version is accepted, a later version can be submitted with Microsoft's `wingetcreate`:

```powershell
wingetcreate update NX1X.pfSentinel --version <version> --urls https://github.com/NX1X/pfSentinel/releases/download/v<version>/pfs.exe --submit --token <token>
```

## What pfSentinel does for a winget install

- `pfs update install` changes nothing and names `winget upgrade NX1X.pfSentinel`. winget records the version it installed, so pfSentinel does not replace that file itself.
- The update alert (event 1006) names the same command.
- Before `winget uninstall`, run `pfs schedule disable`. If you do not, the scheduled tasks stay and point at a file that is gone.
