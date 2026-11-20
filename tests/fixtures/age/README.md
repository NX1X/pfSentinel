# age test vectors

These files are public test vectors for the age v1 file format. They come from the C2SP CCTV project:

- Source: https://github.com/C2SP/CCTV/tree/main/age/testdata
- Commit: `4448f2097b2daa812c91a26141f9f36c2096b9ca` (2026-08-29)
- Downloaded: 2026-11-20
- License: 0BSD, CC0 1.0 or Unlicense, at your choice (see the CCTV age README)

The identities (`AGE-SECRET-KEY-1...`) in these files are published test keys. They are not secrets.

## Which vectors are here

pfSentinel implements only the X25519 recipient type, without ASCII armor. This folder holds the 68 vectors that apply to that subset:

- `x25519*`: the X25519 recipient stanza.
- `stanza_*`, `hmac_*`, `header_crlf`, `empty`, `version_unsupported`: header parsing and the header MAC.
- `stream_*`: the STREAM payload, including truncation, extra data and the final-chunk rules.
- `scrypt_and_x25519`: an scrypt stanza next to another stanza must fail.

Not copied: `armor_*` (ASCII armor), `hybrid_*` (post-quantum recipients) and the other `scrypt_*` vectors (passphrase recipients).

## File format

Each file has a text header, an empty line, and then the age file (zlib-compressed when the header says `compressed: zlib`). The header keys are `expect`, `payload` (SHA-256 of the plaintext released before any error), `identity`, `file key` and `comment`. `tests/unit/test_age_format.py` runs every vector.

Keep the files byte-exact: `.gitattributes` marks them binary, because `header_crlf` has CRLF line ends on purpose.
