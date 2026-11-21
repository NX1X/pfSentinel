# Second copy of the encrypted backups

pfSentinel can keep a second copy of your backups on another target: a folder, an SFTP server, S3-compatible object storage, Azure Blob Storage, or a WebDAV folder. A second copy protects you when the PC that runs pfSentinel fails or is lost.

## Use encryption

Only encrypted backup files (`.enc`) are copied. Plain files never leave the PC, on every target kind. `pfs copy setup` refuses to start until encryption is on:

```bash
pfs encryption setup
pfs encryption encrypt-existing    # optional: encrypt the older plain backups
```

Store the recovery key (or the passphrase) in your password manager, away from this PC. Without it, the second copy cannot be decrypted. See [encryption.md](encryption.md).

If plain backups exist, `pfs copy now` tells you how many files were skipped. A backup run writes it to the log. It sends an alert only if encryption is off.

## How it works

1. After each `pfs backup run`, pfSentinel looks at the encrypted backups of the last `keep_days` days.
2. It copies each file that the target does not have yet, or that has a different size there.
3. It checks the size on the target after each upload.
4. It removes day folders on the target that are older than `keep_days`.

The layout on the target is the same as on the PC: `<device>/YYYY/MM/DD/<run>/<file>.enc`. Retention touches only folders with this layout. Other files on the target stay.

A failed copy never fails the backup. You get a "second copy failed" alert, and the next run copies what is missing.

```bash
pfs copy setup      # choose and test the target, then turn the second copy on
pfs copy now        # copy now, for every device (or -d DEVICE)
pfs copy status     # show the target
pfs copy disable    # stop copying; files on the target stay
```

## Target kinds

| Kind | Target | Status |
|------|--------|--------|
| `folder` | A local path: a network share or a Nextcloud-synced folder | Stable |
| `sftp` | An SSH server, with key login and a pinned host key | Stable |
| `s3` | Any S3-compatible object storage | EXPERIMENTAL |
| `azure` | Azure Blob Storage | EXPERIMENTAL |
| `webdav` | A WebDAV folder: Nextcloud, ownCloud, Synology, Apache | EXPERIMENTAL |

**EXPERIMENTAL** means: the `s3`, `azure` and `webdav` targets are tested against local fake servers that follow the published APIs. They are not tested against every provider in this document. After the first copy, look at the files on the target. Keep the local backups as your main copy. Please report problems on GitHub.

## folder

Enter a path that exists, for example a mounted share or a folder that the Nextcloud desktop client syncs. Each file is written under a temporary `.part` name and then renamed. A failed copy removes the `.part` file.

The folder must be outside the backup folder, and must not hold it. The copy's cleanup removes old day folders, so it would delete local backups. Setup and every copy refuse such a folder.

## sftp

Enter the SSH host, port, user, private key file and remote folder. Setup shows the server host key fingerprint. Compare it on the server (`ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub`) before you trust it. If the host key changes later, pfSentinel refuses to copy until you run `pfs copy setup` again. A key passphrase goes into the credential store.

## Rules for s3, azure and webdav

- **https only.** pfSentinel refuses an `http://` URL. The one exception is a WebDAV server on your LAN, and only after you allow it in setup.
- **TLS verification is always on.** For a server with a private or self-signed certificate (for example MinIO or a NAS), give the CA certificate file in setup. There is no option to turn verification off.
- **Secrets stay in the credential store** (Windows Credential Manager, SecretService, or the encrypted file store). They are never in `config.json`. Error messages and logs never show them.
- **Timeouts:** 15 seconds to connect, 120 seconds for each read from the server.
- **One retry:** after a connection error, a timeout or a 5xx reply, the request is sent once more.
- **Redirects are not followed.**
- **One request per file.** Large uploads are not split into parts. See the size limits below.

## s3: S3-compatible object storage

One client works with every S3-compatible service. It signs requests with AWS Signature Version 4 and needs no cloud SDK.

Setup asks for:

| Setting | Example | Notes |
|---------|---------|-------|
| Endpoint URL | `https://s3.eu-central-1.amazonaws.com` | `https://host[:port]` only, no path |
| Region | `eu-central-1` | `auto` for Cloudflare R2 and Google Cloud Storage |
| Bucket | `pfsentinel-backups` | Create it first |
| Folder inside the bucket | `firewalls` | Optional key prefix |
| Path-style addressing | yes or no | See the table below |
| Access key ID | `AKIA...` | Saved in `config.json` |
| Secret access key | | Saved in the credential store only |
| CA certificate file | | Only for a server with a private certificate |

The key needs these permissions on the bucket: put object, get object (for the size check), list bucket, and delete object. If the provider supports it, create a key that can reach this bucket only.

**Size limit:** at most 5 GiB per file. A larger file is refused with a clear error. Config backups are far smaller. Only a large ZFS snapshot or archive can come near this limit.

**Path-style or virtual-host:** with path-style, the URL is `https://endpoint/bucket/key`. With virtual-host, it is `https://bucket.endpoint/key`. A bucket name with a dot does not work with virtual-host over https, because the certificate does not match. Use path-style for such a bucket.

### Providers

These values come from each provider's documentation. Check that documentation for the current list of regions.

| Provider | Endpoint | Region | Path-style |
|----------|----------|--------|------------|
| AWS S3 | `https://s3.<region>.amazonaws.com` | The bucket region, for example `eu-central-1` | no |
| Backblaze B2 (S3 API) | `https://s3.<region>.backblazeb2.com` | The region in the endpoint, for example `us-west-004` | yes |
| Linode / Akamai Object Storage | `https://<region>.linodeobjects.com` | The region in the endpoint, for example `us-east-1` or `nl-ams-1` | yes |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` | `auto` | yes |
| Wasabi | `https://s3.<region>.wasabisys.com` | For example `eu-central-1` | yes |
| DigitalOcean Spaces | `https://<region>.digitaloceanspaces.com` | The region in the endpoint, for example `fra1` | yes |
| Hetzner Object Storage | `https://<location>.your-objectstorage.com` | The location, for example `fsn1`, `nbg1` or `hel1` | yes |
| Scaleway Object Storage | `https://s3.<region>.scw.cloud` | For example `fr-par`, `nl-ams` or `pl-waw` | yes |
| MinIO (self-hosted) | Your server, for example `https://minio.lan:9000` | `us-east-1`, unless you set another region in MinIO | yes |
| Google Cloud Storage (XML API) | `https://storage.googleapis.com` | `auto` | yes |

Notes on the keys:

- **Backblaze B2:** use an application key. The key ID is the access key ID, and the application key is the secret.
- **Cloudflare R2:** create an R2 API token. It shows an access key ID and a secret access key.
- **Google Cloud Storage:** use an HMAC key. Create it in the Cloud console under Cloud Storage, Settings, Interoperability, preferably for a service account with access to this bucket only.
- **MinIO:** create an access key for a user whose policy covers this bucket only.

## azure: Azure Blob Storage

Azure Blob Storage is not S3-compatible, so pfSentinel has a small client for its REST API (version header `2025-01-05`).

Setup asks for:

| Setting | Example | Notes |
|---------|---------|-------|
| Storage account name | `pfsbackups` | 3 to 24 lowercase letters and digits |
| Container | `pfsentinel` | Create it first |
| Folder inside the container | `firewalls` | Optional prefix |
| Sign in with | `sas` or `key` | `sas` is recommended |
| SAS token or account key | | Saved in the credential store only |
| CA certificate file | | Usually blank |

**SAS token (recommended):** create a SAS for the container only, with the permissions read, add, create, write, delete and list. It gives the least access. Give it an expiry date and note that date. When the token expires, copies fail with HTTP 403 and you get an alert. Then create a new token and run `pfs copy setup` again.

**Account key:** the storage account key gives full access to the whole account. Use it only if you cannot use a SAS token.

The endpoint is `https://<account>.blob.core.windows.net`. For another Azure cloud, set `azure_endpoint` in `config.json`, for example `https://<account>.blob.core.usgovcloudapi.net` for Azure Government.

**Size limit:** at most 5000 MiB per file (one Put Blob request). A larger file is refused with a clear error.

## webdav: WebDAV folder

Setup asks for the folder URL, the user name, the password and, if needed, a CA certificate file. Create the folder on the server first.

| Server | Folder URL |
|--------|------------|
| Nextcloud | `https://<host>/remote.php/dav/files/<user>/<folder>` |
| ownCloud | `https://<host>/remote.php/dav/files/<user>/<folder>` |
| Synology (WebDAV Server package) | `https://<nas>:5006/<shared-folder>/<folder>` |
| Apache `mod_dav` | The URL of the DAV location, for example `https://<host>/dav/<folder>` |

For Nextcloud and ownCloud, use an **app password**, not your login password. In Nextcloud, create it under Settings, Security, Devices and sessions.

Each file is uploaded under a temporary name (`.<file>.uploading`) and then renamed with a WebDAV MOVE. A failed upload removes the temporary file. So a half upload never looks like a complete backup. Missing folders are created with MKCOL. Old day folders are removed with one DELETE each.

**Plain http:** only for a server on your LAN. Setup asks you to allow it. With http, the password crosses the network unprotected. The files themselves stay encrypted.

## Settings in config.json

All settings are under `backup_policy.second_copy`. Secrets are never stored here.

| Key | Used by | Meaning |
|-----|---------|---------|
| `enabled` | all | Copy after each backup run |
| `kind` | all | `folder`, `sftp`, `s3`, `azure` or `webdav` |
| `keep_days` | all | Days to keep on the target (default 90) |
| `path` | folder, sftp | Local folder, or the remote folder on the SSH server |
| `host`, `port`, `key_path` | sftp | SSH server and private key file |
| `username` | sftp, webdav | Login name |
| `endpoint`, `region`, `bucket`, `path_style`, `access_key_id` | s3 | See the s3 section |
| `account`, `container`, `azure_auth`, `azure_endpoint` | azure | See the azure section |
| `prefix` | s3, azure | Folder inside the bucket or container |
| `url`, `allow_http` | webdav | Folder URL, and plain http for a LAN server |
| `ca_cert_path` | s3, azure, webdav | CA certificate file for a private certificate |

Configs from older versions load unchanged. The new keys get their defaults.
