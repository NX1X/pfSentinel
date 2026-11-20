# Encryption at rest

A pfSense `config.xml` holds password hashes, CA private keys, TOTP secrets and VPN keys. With encryption on, pfSentinel writes every backup file encrypted, as `<name>.enc`. The plain bytes never touch the disk: extras are read into memory and encrypted while they are written.

Encryption is off by default. Turn it on once per PC.

## Turn it on

```bash
pfs encryption setup
```

Choose a mode:

| Mode | The key | Scheduled runs | On another PC |
|------|---------|----------------|---------------|
| `keystore` (recommended) | 256 random bits in the OS keystore (Windows Credential Manager) | No input needed | Recovery key: `pfs backup decrypt FILE --recovery-key` |
| `passphrase` | Derived from your passphrase (scrypt, N = 2^17, r = 8, p = 1) | No input needed: the passphrase is also in the keystore | Passphrase: `pfs backup decrypt FILE --passphrase` |
| `age` | Standard [age v1](https://age-encryption.org/v1) files, to pfSentinel's own age identity and to your own age keys | No input needed: pfSentinel's identity is in the keystore | The `age` tool, or `pfs backup decrypt FILE --identity-file KEYFILE`. See [age mode](#age-mode). |

In `keystore` mode, setup shows a **recovery key** once, for example `PFSK-ABCD-EFGH-...`. Store it in your password manager. Without it, a dead PC or a lost keystore means the backups are lost. Setup asks you to type its last group, to confirm that you saved it. `pfs encryption recovery-key` shows it again while the key is still in the keystore.

In `passphrase` mode, use at least 12 characters, and store the passphrase in your password manager too.

## Existing backups

Setup encrypts new backups only. To encrypt the older ones:

```bash
pfs encryption encrypt-existing --wipe
```

Each plain file is encrypted, read back and compared, and only then deleted. `--wipe` overwrites the plain file before it is deleted. On an SSD, the drive may still keep old copies of the data, so a wipe is not a guarantee.

## Daily use

Nothing changes. `backup list`, `verify`, `diff` and `restore` decrypt on the fly. `restore` writes a plain copy where you tell it to; delete that copy when you are done.

```bash
pfs encryption status      # mode, key ID, and how many backups are encrypted or plain
pfs encryption disable     # new backups plain again; .enc files stay readable
```

A new `pfs encryption setup` creates a new key for new backups. Every earlier key stays in the keystore under its key ID, so older backups stay readable.

## Restore on another PC

Copy the `.enc` file, install pfSentinel, and run one of these:

```bash
pfs backup decrypt home-fw_..._minor.xml.gz.enc --recovery-key --decompress
pfs backup decrypt home-fw_..._minor.xml.gz.enc --passphrase --decompress
pfs backup decrypt home-fw_..._minor.xml.gz.enc --identity-file key.txt --decompress
```

`--decompress` also removes the gzip layer, so you get the `config.xml` to restore in pfSense (Diagnostics > Backup & Restore). In `age` mode, you can also use the `age` tool without pfSentinel (see [age mode](#age-mode)).

## age mode

In `age` mode, every backup file is a standard [age v1](https://age-encryption.org/v1) file. You can open it with the `age` tool and your own age key, without pfSentinel. The file name still ends in `.enc`.

### Set it up

1. Make an age key on any PC, if you do not have one yet: `age-keygen -o key.txt`. The command prints the public key (`age1...`).
2. Keep `key.txt` safe, for example in your password manager. It is the secret part.
3. On the pfSentinel PC, run:

   ```bash
   pfs encryption setup --mode age --recipient age1yourpublickey...
   ```

   Add `--recipient` once for each key that must open the backups. Only the public keys go to pfSentinel.

Setup also makes an age identity for pfSentinel itself and keeps it in the OS keystore. pfSentinel needs it to read its own backups for `verify`, `diff`, `restore` and change detection. Setup shows this identity once (`AGE-SECRET-KEY-1...`). With your own key added, saving it is optional: your key already opens every backup. Without your own key, it is the only way to open the backups on another PC. In that case, setup asks you to type its last 4 characters, to confirm that you saved it. `pfs encryption recovery-key` shows it again while it is still in the keystore.

Each file is encrypted to pfSentinel's identity and to each of your keys. `pfs encryption status` lists the public keys.

### Open a backup with the age tool

```bash
age -d -i key.txt -o home-fw_..._minor.xml.gz home-fw_..._minor.xml.gz.enc
gunzip home-fw_..._minor.xml.gz
```

To use pfSentinel's identity instead, save it in a file (one line, `AGE-SECRET-KEY-1...`) and pass that file to `-i`.

### Open a backup with pfSentinel on another PC

```bash
pfs backup decrypt home-fw_..._minor.xml.gz.enc --identity-file key.txt --decompress
pfs backup decrypt home-fw_..._minor.xml.gz.enc --identity --decompress
```

`--identity-file` reads an age identity file, as made by `age-keygen`. `--identity` asks for one identity on the keyboard.

### What pfSentinel stores in age mode

- In the OS keystore: pfSentinel's identity (`AGE-SECRET-KEY-1...`), under its key ID.
- In `config.json`: the key ID, pfSentinel's public key (`age_recipient`), your public keys (`age_recipients`) and the key IDs of all age identities made on this PC (`age_key_ids`). No secret.
- Not stored: your own age identity. pfSentinel never needs it.

A new `pfs encryption setup` makes a new identity. The old ones stay in the keystore, and `age_key_ids` keeps their IDs, so older backups stay readable.

### Limits of age mode

- Only X25519 keys (`age1...` and `AGE-SECRET-KEY-1...`, as made by `age-keygen`) are supported. Post-quantum (`age1pq1...`), plugin (YubiKey and others), SSH and passphrase recipients are not.
- pfSentinel writes and reads the binary format only, not the ASCII-armored (`-a`) format.
- pfSentinel implements the age format itself, with the `cryptography` package. It passes the 68 applicable C2SP CCTV test vectors (`tests/fixtures/age/`), and was checked by hand against `age` v1.3.2 in both directions.

## What is and is not encrypted

- Encrypted: every backup file (config, extras, ZFS streams, archives).
- Not encrypted: `backup_index.json`. It holds file names, dates, sizes, hashes, the pfSense version and hostname, and which config sections changed. It holds no secrets.
- The key protects the backups if the backup folder or disk is copied or stolen. It does not protect against malware that runs as your user, because scheduled backups must be able to use the key without you.

## File format (version 1)

This section is about the `keystore` and `passphrase` modes. `age` mode writes the [age v1 format](https://age-encryption.org/v1) instead. pfSentinel tells the two apart by the first bytes of the file: `PFSENC` or `age-encryption.org/`.

All integers are big-endian.

| Field | Bytes | Value |
|-------|-------|-------|
| magic | 8 | `PFSENC` `00` `01` |
| mode | 1 | 1 = keystore key, 2 = passphrase |
| key ID | 8 | first 8 bytes of HMAC-SHA256(key, `pfsentinel-key-id`) |
| salt | 16 | scrypt salt (zeros in keystore mode) |
| log2 N, r, p | 3 | scrypt parameters (zeros in keystore mode) |
| nonce prefix | 7 | random, new for every file |

After the 43-byte header come the chunks: AES-256-GCM of each 64 KiB of plaintext, each with a 16-byte tag. The nonce of chunk `i` is `prefix + i (4 bytes) + last (1 byte)`, where `last` is 1 only for the final chunk. The associated data of every chunk is the header. This is the STREAM construction, as used by age and Tink: a changed, reordered, dropped or added chunk, or a changed header, fails authentication.

The recovery key is `PFSK-` followed by the base32 of the 32-byte key plus 3 check bytes (the first 3 bytes of HMAC-SHA256(key, `pfsentinel-recovery-check`)), in groups of 4.

### Standalone decryption

This script needs only Python 3 and the `cryptography` package. It reads a file encrypted in `keystore` or `passphrase` mode, without pfSentinel. For `age` mode, use the `age` tool instead.

```python
"""Decrypt a pfSentinel .enc backup: python pfs_decrypt.py FILE OUT (keystore or passphrase)."""
import base64, getpass, hashlib, hmac, struct, sys
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

CHUNK, TAG, HEAD = 64 * 1024, 16, struct.Struct(">8sB8s16sBBB7s")

def decrypt(src, dst, secret):
    with open(src, "rb") as f:
        header = f.read(HEAD.size)
        magic, mode, kid, salt, log2_n, r, p, prefix = HEAD.unpack(header)
        if magic != b"PFSENC\x00\x01":
            sys.exit("not a pfSentinel v1 encrypted file")
        if mode == 1:  # recovery key: PFSK-XXXX-...
            raw = "".join(secret.split()).upper().replace("-", "").removeprefix("PFSK")
            raw = base64.b32decode(raw + "=" * (-len(raw) % 8))
            key = raw[:32]
            if hmac.new(key, b"pfsentinel-recovery-check", hashlib.sha256).digest()[:3] != raw[32:]:
                sys.exit("recovery key has a typo")
        else:  # passphrase
            key = Scrypt(salt=salt, length=32, n=2**log2_n, r=r, p=p).derive(secret.encode())
        if hmac.new(key, b"pfsentinel-key-id", hashlib.sha256).digest()[:8] != kid:
            sys.exit("wrong key or passphrase for this file")
        aead, counter, out = AESGCM(key), 0, open(dst, "wb")
        chunk = f.read(CHUNK + TAG)
        while True:
            following = f.read(CHUNK + TAG)
            last = not following
            nonce = prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")
            out.write(aead.decrypt(nonce, chunk, header))
            if last:
                break
            chunk, counter = following, counter + 1
        out.close()

if __name__ == "__main__":
    decrypt(sys.argv[1], sys.argv[2], getpass.getpass("Recovery key or passphrase: "))
```

The output is still gzip-compressed when the file name ends in `.gz.enc`; open it with `gunzip`, 7-Zip or `python -m gzip -d`.
