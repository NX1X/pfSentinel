"""Second copy targets over HTTP (EXPERIMENTAL): S3-compatible, Azure Blob, WebDAV.

Rules shared by all three:

- https only. WebDAV may use plain http for a LAN server, but only when
  ``allow_http`` is set. TLS verification is always on; a server with a
  private certificate is trusted through ``ca_cert_path``.
- Every request has a timeout and is tried once more after a connection
  error, a timeout or a 5xx reply. Redirects are not followed.
- Secrets come from the credential store only. Error and log messages are
  scrubbed of the target's secrets and passed through ``redact_secrets``.
- Only ``.enc`` files are uploaded.
- One request per upload, so a file is limited to 5 GiB on S3 and to
  5000 MiB on Azure. A half upload never looks complete: S3 and Azure show
  an object only when its request completes, and WebDAV uploads to a
  temporary name and then renames it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import posixpath
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import formatdate
from pathlib import Path
from typing import BinaryIO
from urllib.parse import SplitResult, parse_qsl, quote, unquote, urlsplit

import requests
from lxml import etree

from pfsentinel.models.config import SecondCopyPolicy
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.second_copy import CopyTarget, SecondCopyError, is_safe_rel
from pfsentinel.utils.logging import get_logger, redact_secrets
from pfsentinel.utils.request_signing import (
    EMPTY_SHA256,
    azure_shared_key_authorization,
    encode_query,
    sigv4_authorization,
    uri_encode,
)
from pfsentinel.utils.xml_parser import PfSenseXMLError, parse_untrusted_xml

logger = get_logger(__name__)

S3_SECRET_NAME = "__second_copy_s3_secret__"  # noqa: S105 - store entry name, not a secret
AZURE_SAS_NAME = "__second_copy_azure_sas__"
AZURE_KEY_NAME = "__second_copy_azure_key__"
WEBDAV_PASSWORD_NAME = "__second_copy_webdav_password__"  # noqa: S105 - entry name

EXPERIMENTAL_KINDS = frozenset({"s3", "azure", "webdav"})

S3_MAX_UPLOAD = 5 * 1024**3
AZURE_MAX_UPLOAD = 5000 * 1024**2
AZURE_API_VERSION = "2025-01-05"
# (connect, read) seconds. The read timeout is per socket operation, so a
# large upload on a slow line is fine as long as data keeps moving.
TIMEOUT = (15, 120)

_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
_REGION_RE = re.compile(r"[a-z0-9-]{1,40}")
_AZURE_ACCOUNT_RE = re.compile(r"[a-z0-9]{3,24}")
_AZURE_CONTAINER_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_DIGITS = re.compile(r"[0-9]+")
_DAY_PARTS = (re.compile(r"[0-9]{4}"), re.compile(r"[0-9]{2}"), re.compile(r"[0-9]{2}"))


def _require_encrypted(local: Path) -> None:
    if not local.name.endswith(".enc"):
        raise SecondCopyError(f"Refusing to upload a plain file: {local.name}")


def _check_url(
    url: str, what: str, *, allow_http: bool = False, http_hint: str = ""
) -> SplitResult:
    parts = urlsplit(url.strip())
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme.lower() not in schemes:
        raise SecondCopyError(f"{what} must start with https://{http_hint}")
    if not parts.hostname:
        raise SecondCopyError(f"{what} has no host name")
    if "@" in parts.netloc:
        raise SecondCopyError(f"{what} must not contain a user name or password")
    if parts.query or parts.fragment:
        raise SecondCopyError(f"{what} must not contain ? or #")
    return parts


def _host_only(url: str, what: str) -> SplitResult:
    parts = _check_url(url, what)
    if parts.path not in ("", "/"):
        raise SecondCopyError(f"{what} must be https://host[:port] without a path")
    return parts


def _verify(policy: SecondCopyPolicy) -> bool | str:
    if not policy.ca_cert_path:
        return True
    path = Path(policy.ca_cert_path).expanduser()
    if not path.is_file():
        raise SecondCopyError(f"CA certificate file not found: {path}")
    return str(path)


def _normalize_prefix(prefix: str | None) -> str:
    cleaned = (prefix or "").strip().strip("/")
    if not cleaned:
        return ""
    if not is_safe_rel(cleaned):
        raise SecondCopyError(f"Unsafe prefix: {prefix}")
    return cleaned + "/"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gib(size: int) -> str:
    return f"{size / 1024**3:.2f} GiB"


def day_folders_from_paths(device_id: str, rel_paths: Iterable[str]) -> list[str]:
    """``<device>/YYYY/MM/DD`` for every file path below such a day folder."""
    days: set[str] = set()
    for rel in rel_paths:
        parts = rel.split("/")
        if (
            len(parts) >= 5
            and parts[0] == device_id
            and all(p.fullmatch(v) for p, v in zip(_DAY_PARTS, parts[1:4], strict=True))
        ):
            days.add("/".join(parts[:4]))
    return sorted(days)


def _local_name(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _children(element: etree._Element, name: str) -> list[etree._Element]:
    return [c for c in element if _local_name(c.tag) == name]


def _child_text(element: etree._Element, name: str) -> str | None:
    for child in _children(element, name):
        return child.text or ""
    return None


class _Http:
    """requests session with timeouts, one retry, and scrubbed error messages."""

    def __init__(self, label: str, verify: bool | str, secrets: Iterable[str]) -> None:
        self.label = label
        self._verify = verify
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._session = requests.Session()

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return redact_secrets(text)

    def error(self, text: str) -> SecondCopyError:
        return SecondCopyError(self.scrub(f"{self.label}: {text}"))

    def status_error(
        self, what: str, resp: requests.Response, code: str | None = None
    ) -> SecondCopyError:
        detail = f" {code}" if code else ""
        hint = ""
        if resp.status_code in (401, 403):
            hint = " (check the credentials and their permissions)"
        return self.error(f"{what} failed: HTTP {resp.status_code}{detail}{hint}")

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Callable[[], BinaryIO] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> requests.Response:
        problem = ""
        for attempt in (1, 2):
            data = body() if body is not None else None
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    auth=auth,
                    timeout=TIMEOUT,
                    verify=self._verify,
                    allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                problem = str(e)
                logger.debug(self.scrub(f"{self.label} {method} try {attempt} failed: {e}"))
                continue
            except requests.RequestException as e:
                # "from None": the original exception text may quote the URL.
                raise self.error(f"{method} failed: {e}") from None
            finally:
                if data is not None:
                    data.close()
            if resp.status_code >= 500 and attempt == 1:
                logger.debug(f"{self.label} {method} got HTTP {resp.status_code}, trying again")
                resp.close()
                continue
            return resp
        raise self.error(f"cannot reach the server: {problem}")

    def xml(self, resp: requests.Response) -> etree._Element:
        try:
            return parse_untrusted_xml(resp.content)
        except PfSenseXMLError as e:
            raise self.error(f"unreadable reply from the server: {e}") from None

    def close(self) -> None:
        self._session.close()


def _content_length(http: _Http, resp: requests.Response) -> int:
    value = resp.headers.get("Content-Length", "")
    if not value.isdigit():
        raise http.error("the server did not report the file size")
    return int(value)


class S3Target:
    """Any S3-compatible service, signed with AWS Signature Version 4."""

    def __init__(self, policy: SecondCopyPolicy, creds: CredentialService) -> None:
        if not (policy.endpoint and policy.region and policy.bucket and policy.access_key_id):
            raise SecondCopyError(
                "S3 target needs endpoint, region, bucket and access key ID. Run: pfs copy setup"
            )
        endpoint = _host_only(policy.endpoint, "S3 endpoint")
        if not _BUCKET_RE.fullmatch(policy.bucket):
            raise SecondCopyError(f"Not a valid bucket name: {policy.bucket}")
        if not _REGION_RE.fullmatch(policy.region):
            raise SecondCopyError(f"Not a valid region: {policy.region}")
        secret = creds.get_secret(S3_SECRET_NAME)
        if not secret:
            raise SecondCopyError(
                "The S3 secret key is not in the credential store. Run: pfs copy setup"
            )
        self._secret = secret
        self._key_id = policy.access_key_id
        self._region = policy.region
        self._prefix = _normalize_prefix(policy.prefix)
        netloc = endpoint.netloc.lower()
        if policy.path_style:
            self._host, self._root = netloc, "/" + uri_encode(policy.bucket, keep_slash=False)
        else:
            self._host, self._root = f"{policy.bucket}.{netloc}", ""
        self._http = _Http("S3", _verify(policy), [secret])
        try:
            self._list_page(self._prefix, None, max_keys=1)
        except SecondCopyError:
            self._http.close()
            raise

    def _request(
        self,
        method: str,
        key: str | None,
        query: list[tuple[str, str]] | None = None,
        *,
        payload_sha256: str = EMPTY_SHA256,
        extra: dict[str, str] | None = None,
        body: Callable[[], BinaryIO] | None = None,
    ) -> requests.Response:
        query = query or []
        if key is None:
            path = self._root or "/"
        else:
            path = f"{self._root}/{uri_encode(key, keep_slash=True)}"
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "host": self._host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_sha256,
            **(extra or {}),
        }
        headers["authorization"] = sigv4_authorization(
            method,
            path,
            query,
            headers,
            payload_sha256,
            access_key_id=self._key_id,
            secret_access_key=self._secret,
            region=self._region,
            service="s3",
            amz_date=amz_date,
        )
        url = f"https://{self._host}{path}"
        if query:
            url += "?" + encode_query(query)
        return self._http.send(method, url, headers=headers, body=body)

    def _fail(self, what: str, resp: requests.Response) -> SecondCopyError:
        code = None
        if resp.content:
            with contextlib.suppress(PfSenseXMLError):
                code = _child_text(parse_untrusted_xml(resp.content), "Code")
        return self._http.status_error(what, resp, code)

    def _key(self, rel: str) -> str:
        if not is_safe_rel(rel):
            raise SecondCopyError(f"Unsafe path: {rel}")
        return self._prefix + rel

    def size(self, rel: str) -> int | None:
        resp = self._request("HEAD", self._key(rel))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise self._fail("HEAD", resp)
        return _content_length(self._http, resp)

    def put(self, local: Path, rel: str) -> None:
        _require_encrypted(local)
        size = local.stat().st_size
        if size > S3_MAX_UPLOAD:
            raise SecondCopyError(
                f"{local.name} is {_gib(size)}: the S3 target uploads each file in one "
                "request, which allows at most 5 GiB"
            )
        resp = self._request(
            "PUT",
            self._key(rel),
            payload_sha256=_sha256_file(local),
            extra={"content-type": "application/octet-stream"},
            body=lambda: local.open("rb"),
        )
        if resp.status_code != 200:
            raise self._fail("PUT", resp)

    def _list_page(
        self, prefix: str, token: str | None, max_keys: int | None = None
    ) -> tuple[list[str], str | None]:
        query = [("list-type", "2"), ("prefix", prefix)]
        if token:
            query.append(("continuation-token", token))
        if max_keys:
            query.append(("max-keys", str(max_keys)))
        resp = self._request("GET", None, query)
        if resp.status_code != 200:
            raise self._fail("LIST", resp)
        root = self._http.xml(resp)
        keys = [_child_text(c, "Key") or "" for c in _children(root, "Contents")]
        truncated = (_child_text(root, "IsTruncated") or "").strip().lower() == "true"
        next_token = _child_text(root, "NextContinuationToken") if truncated else None
        return [k for k in keys if k], next_token or None

    def _keys(self, prefix: str) -> Iterator[str]:
        token: str | None = None
        while True:
            keys, next_token = self._list_page(prefix, token)
            yield from keys
            if not next_token:
                return
            if next_token == token:
                raise self._http.error("the listing does not advance")
            token = next_token

    def day_folders(self, device_id: str) -> list[str]:
        start = len(self._prefix)
        keys = self._keys(self._key(device_id) + "/")
        return day_folders_from_paths(device_id, (k[start:] for k in keys))

    def remove_tree(self, rel: str) -> None:
        for key in list(self._keys(self._key(rel) + "/")):
            resp = self._request("DELETE", key)
            if resp.status_code not in (200, 204, 404):
                raise self._fail("DELETE", resp)

    def close(self) -> None:
        self._http.close()


class AzureBlobTarget:
    """Azure Blob Storage with a container SAS token or the account Shared Key."""

    def __init__(self, policy: SecondCopyPolicy, creds: CredentialService) -> None:
        if not (policy.account and policy.container):
            raise SecondCopyError(
                "Azure target needs a storage account and a container. Run: pfs copy setup"
            )
        if not _AZURE_ACCOUNT_RE.fullmatch(policy.account):
            raise SecondCopyError(f"Not a valid storage account name: {policy.account}")
        if not _AZURE_CONTAINER_RE.fullmatch(policy.container) or "--" in policy.container:
            raise SecondCopyError(f"Not a valid container name: {policy.container}")
        endpoint = _host_only(
            policy.azure_endpoint or f"https://{policy.account}.blob.core.windows.net",
            "Azure endpoint",
        )
        self._base = f"https://{endpoint.netloc}"
        self._account = policy.account
        self._container = policy.container
        self._prefix = _normalize_prefix(policy.prefix)
        self._sas: list[tuple[str, str]] = []
        self._account_key: str | None = None
        secrets: list[str] = []
        if policy.azure_auth == "sas":
            token = creds.get_secret(AZURE_SAS_NAME)
            if not token:
                raise SecondCopyError(
                    "The Azure SAS token is not in the credential store. Run: pfs copy setup"
                )
            self._sas = parse_qsl(token.strip().lstrip("?"), keep_blank_values=True)
            signatures = [v for k, v in self._sas if k == "sig"]
            if not signatures or not all(signatures):
                raise SecondCopyError(
                    "The stored Azure SAS token has no signature (sig=). Run: pfs copy setup"
                )
            secrets = [token, *signatures, *(uri_encode(s, keep_slash=False) for s in signatures)]
        else:
            key = creds.get_secret(AZURE_KEY_NAME)
            if not key:
                raise SecondCopyError(
                    "The Azure account key is not in the credential store. Run: pfs copy setup"
                )
            try:
                base64.b64decode(key, validate=True)
            except ValueError:
                raise SecondCopyError(
                    "The stored Azure account key is not valid base64. Run: pfs copy setup"
                ) from None
            self._account_key = key
            secrets = [key]
        self._http = _Http("Azure", _verify(policy), secrets)
        try:
            self._list_page(self._prefix, None, max_results=1)
        except SecondCopyError:
            self._http.close()
            raise

    def _request(
        self,
        method: str,
        blob: str | None,
        query: list[tuple[str, str]] | None = None,
        *,
        extra: dict[str, str] | None = None,
        body: Callable[[], BinaryIO] | None = None,
        length: int = 0,
    ) -> requests.Response:
        query = list(query or [])
        path = f"/{self._container}"
        if blob is not None:
            path += "/" + uri_encode(blob, keep_slash=True)
        headers = {
            "x-ms-date": formatdate(usegmt=True),
            "x-ms-version": AZURE_API_VERSION,
            **(extra or {}),
        }
        if self._account_key is not None:
            signed = {**headers, "Content-Length": str(length) if length else ""}
            headers["Authorization"] = azure_shared_key_authorization(
                self._account, self._account_key, method, path, query, signed
            )
            sent = query
        else:
            sent = query + self._sas
        url = self._base + path + ("?" + encode_query(sent) if sent else "")
        return self._http.send(method, url, headers=headers, body=body)

    def _fail(self, what: str, resp: requests.Response) -> SecondCopyError:
        return self._http.status_error(what, resp, resp.headers.get("x-ms-error-code"))

    def _blob(self, rel: str) -> str:
        if not is_safe_rel(rel):
            raise SecondCopyError(f"Unsafe path: {rel}")
        return self._prefix + rel

    def size(self, rel: str) -> int | None:
        resp = self._request("HEAD", self._blob(rel))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise self._fail("Get Blob Properties", resp)
        return _content_length(self._http, resp)

    def put(self, local: Path, rel: str) -> None:
        _require_encrypted(local)
        size = local.stat().st_size
        if size > AZURE_MAX_UPLOAD:
            raise SecondCopyError(
                f"{local.name} is {_gib(size)}: the Azure target uploads each file in one "
                "request, which allows at most 5000 MiB"
            )
        resp = self._request(
            "PUT",
            self._blob(rel),
            extra={"x-ms-blob-type": "BlockBlob", "Content-Type": "application/octet-stream"},
            body=lambda: local.open("rb"),
            length=size,
        )
        if resp.status_code != 201:
            raise self._fail("Put Blob", resp)

    def _list_page(
        self, prefix: str, marker: str | None, max_results: int | None = None
    ) -> tuple[list[str], str | None]:
        query = [("restype", "container"), ("comp", "list"), ("prefix", prefix)]
        if marker:
            query.append(("marker", marker))
        if max_results:
            query.append(("maxresults", str(max_results)))
        resp = self._request("GET", None, query)
        if resp.status_code != 200:
            raise self._fail("List Blobs", resp)
        root = self._http.xml(resp)
        names: list[str] = []
        for blobs in _children(root, "Blobs"):
            names.extend(_child_text(b, "Name") or "" for b in _children(blobs, "Blob"))
        return [n for n in names if n], (_child_text(root, "NextMarker") or None)

    def _names(self, prefix: str) -> Iterator[str]:
        marker: str | None = None
        while True:
            names, next_marker = self._list_page(prefix, marker)
            yield from names
            if not next_marker:
                return
            if next_marker == marker:
                raise self._http.error("the listing does not advance")
            marker = next_marker

    def day_folders(self, device_id: str) -> list[str]:
        start = len(self._prefix)
        names = self._names(self._blob(device_id) + "/")
        return day_folders_from_paths(device_id, (n[start:] for n in names))

    def remove_tree(self, rel: str) -> None:
        for name in list(self._names(self._blob(rel) + "/")):
            resp = self._request("DELETE", name)
            if resp.status_code not in (200, 202, 404):
                raise self._fail("Delete Blob", resp)

    def close(self) -> None:
        self._http.close()


_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/></d:prop>'
    b"</d:propfind>"
)


@dataclass(frozen=True)
class _DavEntry:
    path: str  # decoded URL path, no trailing slash
    is_dir: bool
    size: int | None


class WebDavTarget:
    """A WebDAV folder: Nextcloud, ownCloud, Synology, Apache mod_dav and others."""

    def __init__(self, policy: SecondCopyPolicy, creds: CredentialService) -> None:
        if not (policy.url and policy.username):
            raise SecondCopyError("WebDAV target needs a URL and a user name. Run: pfs copy setup")
        parts = _check_url(
            policy.url,
            "WebDAV URL",
            allow_http=policy.allow_http,
            http_hint=". Plain http is only for a LAN server: allow it in pfs copy setup",
        )
        password = creds.get_secret(WEBDAV_PASSWORD_NAME)
        if not password:
            raise SecondCopyError(
                "The WebDAV password is not in the credential store. Run: pfs copy setup"
            )
        # Accept a path typed with spaces or with %20: decode, then encode once.
        path = quote(unquote(parts.path), safe="/").rstrip("/")
        self._base = f"{parts.scheme.lower()}://{parts.netloc}{path}"
        self._auth = (policy.username, password)
        self._made: set[str] = set()
        self._http = _Http("WebDAV", _verify(policy), [password])
        try:
            if self._propfind(self._base + "/", "0") is None:
                raise SecondCopyError(f"WebDAV folder does not exist: {self._base}/")
        except SecondCopyError:
            self._http.close()
            raise

    def _url(self, rel: str, *, collection: bool = False) -> str:
        if not is_safe_rel(rel):
            raise SecondCopyError(f"Unsafe path: {rel}")
        return f"{self._base}/{uri_encode(rel, keep_slash=True)}" + ("/" if collection else "")

    def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: Callable[[], BinaryIO] | None = None,
    ) -> requests.Response:
        return self._http.send(method, url, headers=headers, body=body, auth=self._auth)

    def _propfind(self, url: str, depth: str) -> list[_DavEntry] | None:
        resp = self._send(
            "PROPFIND",
            url,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            body=lambda: io.BytesIO(_PROPFIND_BODY),
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 207:
            raise self._http.status_error("PROPFIND", resp)
        entries = []
        for response in self._http.xml(resp).iter("{DAV:}response"):
            href = response.findtext("{DAV:}href") or ""
            is_dir, size = False, None
            for propstat in response.iter("{DAV:}propstat"):
                if " 200 " not in f"{propstat.findtext('{DAV:}status') or ''} ":
                    continue
                prop = propstat.find("{DAV:}prop")
                if prop is None:
                    continue
                if prop.find("{DAV:}resourcetype/{DAV:}collection") is not None:
                    is_dir = True
                length = (prop.findtext("{DAV:}getcontentlength") or "").strip()
                if length.isdigit():
                    size = int(length)
            entries.append(_DavEntry(unquote(urlsplit(href).path).rstrip("/"), is_dir, size))
        return entries

    def size(self, rel: str) -> int | None:
        entries = self._propfind(self._url(rel), "0")
        if not entries or entries[0].is_dir:
            return None
        return entries[0].size

    def _mkdirs(self, rel_dir: str) -> None:
        parts = rel_dir.split("/")
        for depth in range(1, len(parts) + 1):
            sub = "/".join(parts[:depth])
            if sub in self._made:
                continue
            resp = self._send("MKCOL", self._url(sub, collection=True))
            # 405: the folder already exists.
            if resp.status_code not in (201, 405):
                raise self._http.status_error("MKCOL", resp)
            self._made.add(sub)

    def put(self, local: Path, rel: str) -> None:
        _require_encrypted(local)
        if not is_safe_rel(rel):
            raise SecondCopyError(f"Unsafe path: {rel}")
        folder, _, name = rel.rpartition("/")
        if folder:
            self._mkdirs(folder)
        # Not ".part": Nextcloud refuses uploads with that ending.
        temp = f"{folder}/.{name}.uploading" if folder else f".{name}.uploading"
        resp = self._send(
            "PUT",
            self._url(temp),
            headers={"Content-Type": "application/octet-stream"},
            body=lambda: local.open("rb"),
        )
        if resp.status_code not in (200, 201, 204):
            raise self._http.status_error("PUT", resp)
        resp = self._send(
            "MOVE", self._url(temp), headers={"Destination": self._url(rel), "Overwrite": "T"}
        )
        if resp.status_code not in (201, 204):
            with contextlib.suppress(SecondCopyError):
                self._send("DELETE", self._url(temp))
            raise self._http.status_error("MOVE", resp)

    def _subfolders(self, rel: str) -> list[str]:
        url = self._url(rel, collection=True)
        own = unquote(urlsplit(url).path).rstrip("/")
        names = []
        for entry in self._propfind(url, "1") or []:
            name = posixpath.basename(entry.path)
            if entry.is_dir and entry.path != own and _DIGITS.fullmatch(name):
                names.append(name)
        return names

    def day_folders(self, device_id: str) -> list[str]:
        days = []
        for year in self._subfolders(device_id):
            for month in self._subfolders(f"{device_id}/{year}"):
                for day in self._subfolders(f"{device_id}/{year}/{month}"):
                    days.append(f"{device_id}/{year}/{month}/{day}")
        return days

    def remove_tree(self, rel: str) -> None:
        resp = self._send("DELETE", self._url(rel, collection=True))
        if resp.status_code not in (200, 204, 404):
            raise self._http.status_error("DELETE", resp)

    def close(self) -> None:
        self._http.close()


def open_cloud_target(policy: SecondCopyPolicy, creds: CredentialService) -> CopyTarget:
    if policy.kind == "s3":
        return S3Target(policy, creds)
    if policy.kind == "azure":
        return AzureBlobTarget(policy, creds)
    if policy.kind == "webdav":
        return WebDavTarget(policy, creds)
    raise SecondCopyError(f"Not an HTTP target kind: {policy.kind}")
