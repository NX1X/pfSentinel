# Restore a backup

This guide shows how to open a backup and put a config back on pfSense. With encryption on, every backup file ends in `.enc`. pfSentinel decrypts it for you, so you do not need a separate tool.

The commands are for PowerShell on Windows. On Linux, use the same commands with Linux paths, for example `--target ~/Desktop`.

## 1. Find the backup

1. Run `pfs backup list`. Each row shows the run number, the type, the file name and the changes.
2. Pick the run you want. For a config, the `Changes` column helps: `minor` means only small changes since the previous backup.
3. Optional: check the file you want, with its name in quotes: `pfs backup verify "<file name>"`. It checks the SHA-256 checksum, then decrypts and reads the file. Without a file name, `pfs backup verify` checks only the backups whose Status is `?`.
4. Optional: compare two configs before you restore:

   ```powershell
   pfs backup diff "<older config file name>" "<newer config file name>"
   ```

Put each file name in quotes. A name holds `#`, and quotes keep the name in one piece.

## 2. Get a plain copy

### On the PC that made the backup

The key is in the keystore of that PC (Windows Credential Manager), so you need no password:

```powershell
pfs backup restore "firewall_2026-11-22_030004_#011_minor.xml.gz.enc" --target "$env:USERPROFILE\Desktop"
```

You get `firewall_2026-11-22_030004_#011_minor.xml` on the Desktop. It is the plain `config.xml` of pfSense.

This copy is not encrypted. It holds password hashes, private keys and VPN keys. Delete it when you are done.

### On another PC

1. Copy the `.enc` file to the other PC.
2. Install pfSentinel there (see [installation](installation.md)).
3. Run the command for your encryption mode. `pfs encryption status` on the old PC shows the mode.

   ```powershell
   pfs backup decrypt "<file>.xml.gz.enc" --recovery-key --decompress    # keystore mode
   pfs backup decrypt "<file>.xml.gz.enc" --passphrase --decompress      # passphrase mode
   pfs backup decrypt "<file>.xml.gz.enc" --identity-file key.txt --decompress   # age mode
   ```

In keystore mode you need the recovery key. pfSentinel shows it once at `pfs encryption setup`. To see it again on the old PC, run `pfs encryption recovery-key`. Keep it outside the PC, for example in a password manager. Without the recovery key, keystore backups cannot be opened on another PC.

Without pfSentinel, you can use the `age` tool (age mode) or the standalone Python script in [encryption](encryption.md#standalone-decryption).

## 3. Put the config back on pfSense

1. Sign in to the pfSense web interface.
2. Open **Diagnostics > Backup & Restore**.
3. Under **Restore Backup**, set **Restore area** to **All** for a full restore.
4. Select the plain `.xml` file from step 2.
5. Leave **Configuration file is encrypted** off. That option is for pfSense's own encryption, not for pfSentinel's.
6. Select **Restore Configuration**. After a full restore, pfSense restarts.
7. Delete the plain `.xml` file.

To restore one part only, for example the firewall rules, select that part in **Restore area**.

## Extras (rrd, pkg, dhcp, aliases, certs, logs)

An extra is a `.tar.gz` or `.txt.gz` file. `pfs backup restore` gives you the plain `.tar` or `.txt`:

```powershell
pfs backup restore "firewall_2026-11-22_030004_#011_rrd.tar.gz.enc" --target "$env:USERPROFILE\Desktop"
tar -xf "$env:USERPROFILE\Desktop\firewall_2026-11-22_030004_#011_rrd.tar"
```

pfSentinel does not copy extras back to the device. Take the files you need from the archive, and copy them to the device yourself, for example with `scp`. To see which paths on the device an extra came from, run `pfs backup info "<file name>"`.

## ZFS snapshots

A ZFS file is a `zfs send` stream. `pfs backup restore` gives you the plain stream. To restore an incremental stream, you need the full stream of its chain first, then every incremental after it, in order. See [extended backups](extended-backups.md#zfs-snapshot-backups).

`pfs backup restore` reads the whole file into memory. A ZFS stream can be several GB, so the PC needs that much free memory.

Restoring a ZFS stream on pfSense (`zfs receive`) was not tested with pfSentinel. Try it on a test system first, and receive into a new dataset name, never over the dataset that is running.
