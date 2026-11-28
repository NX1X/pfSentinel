# winget package (NX1X.pfSentinel)

These three files are the template of the winget manifest. winget installs `pfs.exe` as a portable program: it downloads the file from the GitHub release, checks its SHA-256 and puts `pfs` on the PATH.

pfSentinel is on winget from 1.0.0 on. Release candidates are not published there: winget has no pre-release channel.

Status: not submitted yet. The CI job `publish-winget` sends 1.0.0 as the first version.

## How a release gets to winget

The job `publish-winget` in `.github/workflows/ci.yml` runs after the GitHub release, for stable versions only (`X.Y.Z`, no `-rc.N`). It:

1. Waits for approval in the `winget` environment (reviewer: the maintainer, branch: main).
2. Downloads `pfs.exe` and `checksums-sha256.txt` from the release, and checks the hash and the build attestation.
3. Fills in this template with `scripts/winget_manifest.py` (version, release date, SHA-256 of `pfs.exe`).
4. Downloads Microsoft's `wingetcreate.exe` (pinned version and SHA-256), with telemetry off.
5. Runs `wingetcreate submit`. It validates the manifest, forks `microsoft/winget-pkgs` into the token owner's account once, and opens the pull request.

The token is the repository secret `WINGET_TOKEN`: a classic GitHub token with the `public_repo` scope. Microsoft's README asks for the `repo` scope. `public_repo` should be enough, because the job only writes to public repositories. This is not verified yet. If the submit step fails with a permission error, give the token the `repo` scope.

On the first pull request, the bot asks for the Microsoft contributor agreement (CLA). Sign it once in the pull request. Then wait for the validation pipeline: it installs the package and scans `pfs.exe` with antivirus engines. A moderator merges the first version by hand.

If the job fails after the release, fix the cause and re-run the job in GitHub Actions. The release stays as it is.

To bump `wingetcreate`, change the version and the SHA-256 in the job together. Take the SHA-256 from the release page of `microsoft/winget-create`.

## Fill in the template by hand

```bash
python scripts/winget_manifest.py --version 1.0.0 --date 2026-12-04 --checksums checksums-sha256.txt --out build/winget
```

It writes the three files to `build/winget/manifests/n/NX1X/pfSentinel/1.0.0/` and prints that folder. It stops for a release candidate, a missing `pfs.exe` checksum or a placeholder left in a file.

| Placeholder | Value | Where it comes from |
|---|---|---|
| `{{VERSION}}` | for example `1.0.0` | the release tag without `v` |
| `{{RELEASE_DATE}}` | for example `2026-12-04` | the date of the GitHub release |
| `{{SHA256}}` | 64 hex characters, upper case | the `pfs.exe` line in `checksums-sha256.txt` of the release |

## Test a manifest on a Windows PC

Do not test on a PC whose scheduled backups you depend on: step 6 moves the tasks to the winget copy of `pfs.exe`.

1. Open PowerShell as administrator, and run `winget settings --enable LocalManifestFiles`. This is needed once.
2. Run `winget validate --manifest <folder>`. Expected: `Manifest validation succeeded.`
3. Run `winget install --manifest <folder>`.
4. Open a new PowerShell window, and run `pfs --version`.
5. Run `pfs update check`. If there is a newer release, the output names `winget upgrade NX1X.pfSentinel`.
6. Run `pfs schedule enable`, then `pfs schedule status`. Expected: the tasks are registered.
7. To remove the test install, run `pfs schedule disable`, then `winget uninstall NX1X.pfSentinel`.

## What pfSentinel does for a winget install

- `pfs update install` changes nothing and names `winget upgrade NX1X.pfSentinel`. winget records the version it installed, so pfSentinel does not replace that file itself.
- The update alert (event 1006) names the same command.
- Before `winget uninstall`, run `pfs schedule disable`. If you do not, the scheduled tasks stay and point at a file that is gone.
