"""Request signing for object storage: AWS Signature Version 4 and Azure Shared Key.

Both are implemented with ``hmac`` and ``hashlib`` only, so pfSentinel needs
no cloud SDK. Paths passed in are already URI-encoded (see ``uri_encode``),
because the signature must cover the exact bytes that go on the wire.

References:
- AWS: "Create a signed AWS API request" and "Authenticating Requests:
  Using the Authorization Header (AWS Signature Version 4)" (Amazon S3 API).
- Azure: "Authorize with Shared Key" (Azure Storage REST API).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from urllib.parse import quote

SIGV4_ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def uri_encode(value: str, *, keep_slash: bool) -> str:
    """Percent-encode every byte except ``A-Z a-z 0-9 - _ . ~`` (and ``/`` if kept)."""
    return quote(value, safe="/" if keep_slash else "")


def encode_query(query: Sequence[tuple[str, str]]) -> str:
    """Return the sorted, encoded query string used both on the wire and in SigV4."""
    pairs = sorted(
        (uri_encode(k, keep_slash=False), uri_encode(v, keep_slash=False)) for k, v in query
    )
    return "&".join(f"{k}={v}" for k, v in pairs)


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_signing_key(secret_access_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(f"AWS4{secret_access_key}".encode(), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def sigv4_canonical_request(
    method: str,
    encoded_path: str,
    query: Sequence[tuple[str, str]],
    headers: Mapping[str, str],
    payload_sha256: str,
) -> tuple[str, str]:
    """Return ``(canonical_request, signed_headers)``. Every header given is signed."""
    canonical: dict[str, str] = {}
    for name, value in headers.items():
        canonical[name.strip().lower()] = " ".join(value.strip().split())
    names = sorted(canonical)
    canonical_headers = "".join(f"{n}:{canonical[n]}\n" for n in names)
    signed_headers = ";".join(names)
    request = "\n".join(
        [
            method.upper(),
            encoded_path or "/",
            encode_query(query),
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    return request, signed_headers


def sigv4_string_to_sign(amz_date: str, scope: str, canonical_request: str) -> str:
    digest = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    return f"{SIGV4_ALGORITHM}\n{amz_date}\n{scope}\n{digest}"


def sigv4_authorization(
    method: str,
    encoded_path: str,
    query: Sequence[tuple[str, str]],
    headers: Mapping[str, str],
    payload_sha256: str,
    *,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str,
    amz_date: str,
) -> str:
    """Return the ``Authorization`` header value for a SigV4-signed request.

    ``headers`` must hold every header to sign, including ``host`` and
    ``x-amz-date`` (equal to ``amz_date``, format ``YYYYMMDDTHHMMSSZ``).
    """
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    canonical, signed_headers = sigv4_canonical_request(
        method, encoded_path, query, headers, payload_sha256
    )
    key = sigv4_signing_key(secret_access_key, date_stamp, region, service)
    signature = hmac.new(
        key, sigv4_string_to_sign(amz_date, scope, canonical).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return (
        f"{SIGV4_ALGORITHM} Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


# Standard headers in the order the Shared Key string-to-sign lists them.
_AZURE_STANDARD_HEADERS = (
    "content-encoding",
    "content-language",
    "content-length",
    "content-md5",
    "content-type",
    "date",
    "if-modified-since",
    "if-match",
    "if-none-match",
    "if-unmodified-since",
    "range",
)


def azure_canonicalized_resource(
    account: str, encoded_path: str, query: Sequence[tuple[str, str]]
) -> str:
    """``/<account><path>`` then one ``\\nname:v1,v2`` line per query parameter."""
    params: dict[str, list[str]] = {}
    for name, value in query:
        params.setdefault(name.lower(), []).append(value)
    lines = [f"/{account}{encoded_path or '/'}"]
    for name in sorted(params):
        lines.append(f"{name}:{','.join(sorted(params[name]))}")
    return "\n".join(lines)


def azure_string_to_sign(
    method: str, headers: Mapping[str, str], canonicalized_resource: str
) -> str:
    lower = {k.lower(): v for k, v in headers.items()}
    standard = []
    for name in _AZURE_STANDARD_HEADERS:
        value = lower.get(name, "")
        if name == "content-length" and value == "0":
            value = ""  # since version 2015-02-21, zero length is signed as empty
        standard.append(value)
    ms_headers = sorted(
        (k, " ".join(v.strip().split())) for k, v in lower.items() if k.startswith("x-ms-")
    )
    canonicalized_headers = "".join(f"{k}:{v}\n" for k, v in ms_headers)
    return (
        method.upper()
        + "\n"
        + "\n".join(standard)
        + "\n"
        + canonicalized_headers
        + (canonicalized_resource)
    )


def azure_shared_key_signature(account_key_b64: str, string_to_sign: str) -> str:
    key = base64.b64decode(account_key_b64, validate=True)
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def azure_shared_key_authorization(
    account: str,
    account_key_b64: str,
    method: str,
    encoded_path: str,
    query: Sequence[tuple[str, str]],
    headers: Mapping[str, str],
) -> str:
    """Return the ``Authorization`` header value (``SharedKey account:signature``)."""
    resource = azure_canonicalized_resource(account, encoded_path, query)
    signature = azure_shared_key_signature(
        account_key_b64, azure_string_to_sign(method, headers, resource)
    )
    return f"SharedKey {account}:{signature}"
