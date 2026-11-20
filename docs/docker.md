# pfSentinel in a container

> **EXPERIMENTAL.** This image is new and has not been field-tested against a
> real pfSense. The image is not published to any registry yet. You build it
> yourself from the repository. Do not rely on it as your only backup path.
> Report problems at https://github.com/NX1X/pfSentinel/issues.

## What you get

- A multi-stage `Dockerfile` at the repository root.
- Base image: the official `python` image (Docker Official Images, Docker
  Inc., US), Debian 12 "bookworm" slim. It is pinned by tag and digest.
- Dependencies come from `requirements.lock`. pip checks every hash
  (`--require-hashes --only-binary :all:`). pfSentinel itself is installed
  from the source tree with `--no-deps`.
- The process runs as user `pfs` (UID and GID 10001). It works with a
  read-only root filesystem and with all Linux capabilities dropped.
- The entrypoint is `pfs`. The default command is `--help`.

## Build

```
docker build -t pfsentinel:local .
docker run --rm pfsentinel:local --version
```

## Volumes

| Path in container | Holds | Required |
| --- | --- | --- |
| `/data` | Backups | Yes |
| `/home/pfs/.pfsentinel` | `config.json`, encrypted secret store, trusted SSH host keys | Yes |

Both must be persistent. If you lose the config volume, you lose the device
list, the stored passwords and the encryption keys.

With bind mounts, the host folders must be writable by UID 10001:

```
mkdir -p pfs-data pfs-config
sudo chown 10001:10001 pfs-data pfs-config
chmod 0700 pfs-config
```

Named Docker volumes need no extra step.

## One-time setup

Run these commands once. Use `-it` because they ask questions.

```
RUN="docker run --rm -it -v pfs-data:/data -v pfs-config:/home/pfs/.pfsentinel pfsentinel:local"

$RUN config set backup-dir /data
$RUN device add
$RUN device trust-key <device-id>
$RUN encryption setup
```

1. `config set backup-dir /data` makes backups go to the volume. Without it,
   `backup run --non-interactive` asks where to save and stops.
2. `device add` stores the device and its password.
3. `device trust-key` shows the SSH host key. Compare the fingerprint with
   the firewall before you accept it.
4. `encryption setup` turns on encryption of backups at rest. Save the
   recovery key it prints in a place outside the container.

Check the result with `$RUN status`.

## Run a backup

```
docker run --rm \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -v pfs-data:/data -v pfs-config:/home/pfs/.pfsentinel \
  pfsentinel:local backup run --non-interactive
```

For a published image, use `ghcr.io/nx1x/pfsentinel:<version>@sha256:<digest>`
instead of `pfsentinel:local`.

## Where secrets are stored

A container has no OS keystore. pfSentinel then uses its encrypted file
store, in `/home/pfs/.pfsentinel/store`. It holds the device passwords, the
SSH key passphrase and the notification tokens.

The store seals secrets with AES-256-GCM. The key file `secrets.key` sits
next to the vault file `secrets.json`, in the same folder, with mode 0600.

This means:

- Anyone who can read the config volume can read the key and open the vault.
  That includes root on the host, and any other container that mounts the
  volume.
- The store keeps passwords out of backup archives, logs and `config.json`.
  It also stops other users on the host.
- It does **not** protect against an attacker who already runs code as this
  user, or who can copy the whole config volume. Unattended backups must
  decrypt without a person present, so this limit cannot be removed.

Treat the config volume as a secret. Do not put it in an image, in Git, or on
shared storage. Do not back it up to the same place as the backups.

## Scheduling

pfSentinel has **no daemon mode**. `pfs schedule enable --no-task-scheduler`
starts a scheduler thread inside the process that ran the command. The thread
stops when that command exits. In a container that command exits at once, so
the schedule does not survive. Do not use it here.

Run one container per backup, started by an outside scheduler.

### Option 1: Kubernetes CronJob

Files:

- `deploy/kubernetes/cronjob.yaml`: two PVCs and the CronJob.
- `deploy/kubernetes/setup-pod.yaml`: a pod for the one-time setup.

The CronJob uses `runAsNonRoot`, `readOnlyRootFilesystem`, dropped
capabilities, a `RuntimeDefault` seccomp profile, resource limits and
`concurrencyPolicy: Forbid`. It uses no Secret object. Credentials live in
the encrypted store on the config PVC.

Steps:

1. Build the image and push it to a registry you control.
2. Copy the digest: `docker buildx imagetools inspect <image>:<tag>`.
3. Put the image and digest in both YAML files, in place of
   `REPLACE_WITH_REAL_DIGEST`.
4. Apply `cronjob.yaml`, then follow the steps in `setup-pod.yaml`.
5. Test once: `kubectl create job --from=cronjob/pfsentinel-backup test-run`.

I did not test these manifests on a cluster. I checked only that the YAML
parses.

### Option 2: Docker Compose with host cron

`deploy/compose/compose.yaml` defines one service that runs one backup. It
does not stay running. Start it from the host scheduler:

```
0 2 * * *  cd /opt/pfsentinel && docker compose run --rm pfsentinel
```

A systemd timer works the same way.

## Limits of this experimental image

- Only `linux/amd64` was built and tested.
- No registry publishing, no signed image, no attestation yet.
- Do not use `pfs update` in the container. It was not tested there. Update
  by building or pulling a new image.
- Desktop notifications (Linux) were not tested and probably have no desktop
  to show on. Use Telegram or Slack.
