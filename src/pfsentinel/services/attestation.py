"""Verify the signed build attestation of a downloaded pfSentinel binary.

Releases since 0.2.5 carry SLSA build provenance, signed through the public
Sigstore service by the release workflow (actions/attest-build-provenance).
Before ``pfs update install`` replaces the binary, this module checks, with
``cryptography`` only (no new dependency):

1. GitHub's attestation API returns a SLSA provenance bundle whose subject
   names the asset with exactly the downloaded file's SHA-256.
2. The DSSE signature over that statement verifies with the bundle's
   certificate.
3. The certificate chains to the pinned Sigstore (Fulcio) intermediate and
   root, and is a code-signing certificate.
4. The certificate names this repository's release workflow on main, issued
   for GitHub Actions: the identity ``gh attestation verify`` checks too.
5. The transparency-log promise (Rekor signed entry timestamp) verifies with
   a pinned Rekor key, its entry names the same certificate, and the signing
   time lies inside the certificate's 10-minute validity.

Not checked: the Merkle inclusion proof of the log entry. A bundle without a
signed entry timestamp (a newer log format) is accepted only after steps 1 to
4, with a log line saying so.

The pinned keys come from Sigstore's trusted_root.json
(github.com/sigstore/root-signing, targets/trusted_root.json, read
2026-11-20); the CA certificates are valid until 2031-10-05. If Sigstore
rotates them, update these constants; until then a binary update fails
closed, and installing by hand (docs, "Standalone binary") still works.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID

REPO = "NX1X/pfSentinel"
WORKFLOW_IDENTITY = f"https://github.com/{REPO}/.github/workflows/ci.yml@refs/heads/main"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"
API_URL = f"https://api.github.com/repos/{REPO}/attestations/sha256:{{digest}}"
_OID_ISSUER_V1 = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.1")
_OID_ISSUER_V2 = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.8")

FULCIO_INTERMEDIATE_PEM = b"""-----BEGIN CERTIFICATE-----
MIICGjCCAaGgAwIBAgIUALnViVfnU0brJasmRkHrn/UnfaQwCgYIKoZIzj0EAwMw
KjEVMBMGA1UEChMMc2lnc3RvcmUuZGV2MREwDwYDVQQDEwhzaWdzdG9yZTAeFw0y
MjA0MTMyMDA2MTVaFw0zMTEwMDUxMzU2NThaMDcxFTATBgNVBAoTDHNpZ3N0b3Jl
LmRldjEeMBwGA1UEAxMVc2lnc3RvcmUtaW50ZXJtZWRpYXRlMHYwEAYHKoZIzj0C
AQYFK4EEACIDYgAE8RVS/ysH+NOvuDZyPIZtilgUF9NlarYpAd9HP1vBBH1U5CV7
7LSS7s0ZiH4nE7Hv7ptS6LvvR/STk798LVgMzLlJ4HeIfF3tHSaexLcYpSASr1kS
0N/RgBJz/9jWCiXno3sweTAOBgNVHQ8BAf8EBAMCAQYwEwYDVR0lBAwwCgYIKwYB
BQUHAwMwEgYDVR0TAQH/BAgwBgEB/wIBADAdBgNVHQ4EFgQU39Ppz1YkEZb5qNjp
KFWixi4YZD8wHwYDVR0jBBgwFoAUWMAeX5FFpWapesyQoZMi0CrFxfowCgYIKoZI
zj0EAwMDZwAwZAIwPCsQK4DYiZYDPIaDi5HFKnfxXx6ASSVmERfsynYBiX2X6SJR
nZU84/9DZdnFvvxmAjBOt6QpBlc4J/0DxvkTCqpclvziL6BCCPnjdlIB3Pu3BxsP
mygUY7Ii2zbdCdliiow=
-----END CERTIFICATE-----
"""

FULCIO_ROOT_PEM = b"""-----BEGIN CERTIFICATE-----
MIIB9zCCAXygAwIBAgIUALZNAPFdxHPwjeDloDwyYChAO/4wCgYIKoZIzj0EAwMw
KjEVMBMGA1UEChMMc2lnc3RvcmUuZGV2MREwDwYDVQQDEwhzaWdzdG9yZTAeFw0y
MTEwMDcxMzU2NTlaFw0zMTEwMDUxMzU2NThaMCoxFTATBgNVBAoTDHNpZ3N0b3Jl
LmRldjERMA8GA1UEAxMIc2lnc3RvcmUwdjAQBgcqhkjOPQIBBgUrgQQAIgNiAAT7
XeFT4rb3PQGwS4IajtLk3/OlnpgangaBclYpsYBr5i+4ynB07ceb3LP0OIOZdxex
X69c5iVuyJRQ+Hz05yi+UF3uBWAlHpiS5sh0+H2GHE7SXrk1EC5m1Tr19L9gg92j
YzBhMA4GA1UdDwEB/wQEAwIBBjAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQWBBRY
wB5fkUWlZql6zJChkyLQKsXF+jAfBgNVHSMEGDAWgBRYwB5fkUWlZql6zJChkyLQ
KsXF+jAKBggqhkjOPQQDAwNpADBmAjEAj1nHeXZp+13NWBNa+EDsDP8G1WWg1tCM
WP/WHPqpaVo0jhsweNFZgSs0eE7wYI4qAjEA2WB9ot98sIkoF3vZYdd3/VtWB5b9
TNMea7Ix/stJ5TfcLLeABLE4BNJOsQ4vnBHJ
-----END CERTIFICATE-----
"""

REKOR_KEYS = {
    "c0d23d6ad406973f9559f3ba2d1ca01f84147d8ffc5b8445c224f98b9591801d": (
        "PKIX_ECDSA_P256_SHA_256",
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE2G2Y+2tabdTV5BcGiBIx0a9fAFwrkBbmLSGtks4L3qX6yYY0zufBnhC8Ur/iy55GhWP/9A/bY2LhC30M9+RYtw==",
    ),  # https://rekor.sigstore.dev
    "cf1199155bddd051268d1f16ac5c0c75c009f6fb5a63f4177f8e18d7051e3fa0": (
        "PKIX_ED25519",
        "MCowBQYDK2VwAyEAt8rlp1knGwjfbcXAYPYAkn0XiLz1x8O4t0YkEhie244=",
    ),  # https://log2025-1.rekor.sigstore.dev
}


class AttestationError(Exception):
    """The download has no valid build attestation from this repository."""


def _pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE pre-authentication encoding."""
    t = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def _ext_text(cert: x509.Certificate, oid: x509.ObjectIdentifier) -> str | None:
    try:
        raw: bytes = cert.extensions.get_extension_for_oid(oid).value.value  # type: ignore[attr-defined]
    except x509.ExtensionNotFound:
        return None
    # v1 extensions hold raw text; v2 ones a DER UTF8String (tag 0x0c, length).
    if raw[:1] == b"\x0c" and len(raw) > 2:
        raw = raw[2:] if raw[1] < 0x80 else raw[2 + (raw[1] & 0x7F) :]
    return raw.decode("utf-8", "replace")


def _check_chain(leaf: x509.Certificate) -> None:
    inter = x509.load_pem_x509_certificate(FULCIO_INTERMEDIATE_PEM)
    root = x509.load_pem_x509_certificate(FULCIO_ROOT_PEM)
    try:
        for child, parent in ((leaf, inter), (inter, root)):
            if child.issuer != parent.subject:
                raise AttestationError("certificate is not issued by the pinned Sigstore CA")
            key = parent.public_key()
            if not isinstance(key, ec.EllipticCurvePublicKey) or not child.signature_hash_algorithm:
                raise AttestationError("unexpected certificate key type")
            key.verify(
                child.signature,
                child.tbs_certificate_bytes,
                ec.ECDSA(child.signature_hash_algorithm),
            )
    except InvalidSignature as e:
        raise AttestationError("certificate signature does not verify") from e
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if ExtendedKeyUsageOID.CODE_SIGNING not in eku:
        raise AttestationError("certificate is not a code-signing certificate")


def _check_identity(leaf: x509.Certificate) -> None:
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    if WORKFLOW_IDENTITY not in uris:
        raise AttestationError(f"signed by {uris}, not by {WORKFLOW_IDENTITY}")
    issuer = _ext_text(leaf, _OID_ISSUER_V2) or _ext_text(leaf, _OID_ISSUER_V1)
    if issuer != OIDC_ISSUER:
        raise AttestationError(f"certificate issuer {issuer!r} is not GitHub Actions")


def _check_log_entry(entry: dict[str, Any], leaf: x509.Certificate) -> bool:
    """Verify the signed entry timestamp. False when the entry has none."""
    promise = entry.get("inclusionPromise", {}).get("signedEntryTimestamp")
    if not promise:
        return False
    log_id = base64.b64decode(entry["logId"]["keyId"]).hex()
    if log_id not in REKOR_KEYS:
        raise AttestationError("transparency log is not a pinned Sigstore log")
    kind, raw_key = REKOR_KEYS[log_id]
    payload = json.dumps(
        {
            "body": entry["canonicalizedBody"],
            "integratedTime": int(entry["integratedTime"]),
            "logID": log_id,
            "logIndex": int(entry["logIndex"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    key = serialization.load_der_public_key(base64.b64decode(raw_key))
    signature = base64.b64decode(promise)
    try:
        if isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
        elif isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, payload)
        else:  # pragma: no cover - no other key type is pinned
            raise AttestationError(f"unsupported log key {kind}")
    except InvalidSignature as e:
        raise AttestationError("transparency log promise does not verify") from e

    # The log entry must be about this certificate, at a time it was valid.
    body = base64.b64decode(entry["canonicalizedBody"]).decode("utf-8", "replace")
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM)
    if base64.b64encode(leaf_pem).decode() not in body:
        raise AttestationError("transparency log entry is for another certificate")
    signed_at = int(entry["integratedTime"])
    if (
        not leaf.not_valid_before_utc.timestamp()
        <= signed_at
        <= leaf.not_valid_after_utc.timestamp()
    ):
        raise AttestationError("signed outside the certificate's validity")
    return True


def verify_bundle(bundle: dict[str, Any], asset_name: str, sha256_hex: str) -> None:
    """Raise AttestationError unless ``bundle`` attests ``asset_name`` with this digest."""
    envelope = bundle["dsseEnvelope"]
    payload = base64.b64decode(envelope["payload"])
    statement = json.loads(payload)
    if statement.get("predicateType") != SLSA_PREDICATE:
        raise AttestationError("not a SLSA build provenance attestation")
    if not any(
        s.get("name") == asset_name and s.get("digest", {}).get("sha256") == sha256_hex
        for s in statement.get("subject", [])
    ):
        raise AttestationError(f"the attestation does not cover {asset_name} with this hash")

    material = bundle["verificationMaterial"]
    leaf = x509.load_der_x509_certificate(base64.b64decode(material["certificate"]["rawBytes"]))
    _check_chain(leaf)
    _check_identity(leaf)
    key = leaf.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise AttestationError("unexpected certificate key type")
    try:
        key.verify(
            base64.b64decode(envelope["signatures"][0]["sig"]),
            _pae(envelope["payloadType"], payload),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature as e:
        raise AttestationError("the attestation signature does not verify") from e

    entries = material.get("tlogEntries", [])
    if not any(_check_log_entry(e, leaf) for e in entries):
        from pfsentinel.utils.logging import get_logger

        get_logger(__name__).info(
            "Attestation verified without a transparency-log timestamp (newer log format)"
        )


def verify_file(path: Path, asset_name: str, timeout: int = 15) -> str:
    """Verify ``path`` against the repository's attestations. Returns the SHA-256."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    sha = digest.hexdigest()
    try:
        resp = requests.get(
            API_URL.format(digest=sha),
            headers={"Accept": "application/vnd.github+json", "User-Agent": "pfSentinel-updater"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise AttestationError(f"cannot reach GitHub's attestation API: {type(e).__name__}") from e
    if resp.status_code == 404:
        raise AttestationError("GitHub has no attestation for this file")
    resp.raise_for_status()
    errors = []
    for item in resp.json().get("attestations", []):
        try:
            verify_bundle(item["bundle"], asset_name, sha)
            return sha
        except (AttestationError, KeyError, ValueError) as e:
            errors.append(str(e))
    raise AttestationError("no valid build attestation: " + "; ".join(errors or ["none found"]))
