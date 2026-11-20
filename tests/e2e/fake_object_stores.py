"""In-process fake storage servers for the second copy: S3, Azure Blob, WebDAV.

Each runs a real ThreadingHTTPServer on 127.0.0.1, with TLS by default
(the self-signed certificate from fake_https_server; pass its path as
``ca_cert_path``). They keep objects in memory, check the request signature
or login on every request, and record what they saw.

Failure injection: append an HTTP status to ``fail_next`` and the next
request gets that status instead of being served.
"""

from __future__ import annotations

import base64
import hashlib
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from xml.sax.saxutils import escape

from pfsentinel.utils.request_signing import (
    azure_shared_key_authorization,
    sigv4_authorization,
)

from .fake_https_server import _get_cert_material, _write_key_alongside


@dataclass
class Seen:
    method: str
    path: str  # raw request target, as sent
    headers: dict[str, str]
    body: bytes


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class _FakeServer:
    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.fail_next: list[int] = []
        self.lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self.tls = True

    # -- lifecycle --

    def start(self, *, tls: bool = True) -> _FakeServer:
        self.tls = tls
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                return

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                seen = Seen(self.command, self.path, headers, body)
                with server.lock:
                    server.seen.append(seen)
                    injected = server.fail_next.pop(0) if server.fail_next else None
                    reply = Reply(injected) if injected else server.handle(seen)
                self.send_response(reply.status)
                payload = reply.body
                for k, v in reply.headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in reply.headers:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD" and payload:
                    self.wfile.write(payload)

            do_GET = do_HEAD = do_PUT = do_DELETE = _serve  # noqa: N815
            do_PROPFIND = do_MKCOL = do_MOVE = _serve  # noqa: N815

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            _, key_pem, cert_path = _get_cert_material()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # DevSkim: ignore DS440000 - test fake; a TLS floor, not a config choice.
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(cert_path), str(_write_key_alongside(cert_path, key_pem)))
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"

    @property
    def cert_path(self) -> Path:
        return _get_cert_material()[2]

    def handle(self, req: Seen) -> Reply:  # pragma: no cover - overridden
        raise NotImplementedError

    def wire_text(self) -> str:
        """Everything the client sent, as text, to look for leaked secrets."""
        with self.lock:
            return "\n".join(
                f"{r.method} {r.path} {r.headers} {r.body.decode('latin-1')}" for r in self.seen
            )

    def count(self, method: str) -> int:
        with self.lock:
            return sum(1 for r in self.seen if r.method == method)


def _xml_reply(status: int, xml: str) -> Reply:
    return Reply(status, xml.encode(), {"Content-Type": "application/xml"})


class FakeS3(_FakeServer):
    """Path-style S3: /<bucket>/<key>. Checks SigV4 and the payload hash."""

    def __init__(
        self, *, access_key_id: str, secret: str, bucket: str, region: str, page_size: int = 2
    ) -> None:
        super().__init__()
        self.access_key_id = access_key_id
        self.secret = secret
        self.bucket = bucket
        self.region = region
        self.page_size = page_size
        self.objects: dict[str, bytes] = {}

    def _error(self, status: int, code: str) -> Reply:
        return _xml_reply(status, f"<Error><Code>{code}</Code><Message>x</Message></Error>")

    def handle(self, req: Seen) -> Reply:
        path, _, raw_query = req.path.partition("?")
        query = parse_qsl(raw_query, keep_blank_values=True)
        auth = req.headers.get("authorization", "")
        try:
            signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
            headers = {h: req.headers[h] for h in signed}
            payload = req.headers["x-amz-content-sha256"]
            expected = sigv4_authorization(
                req.method,
                path,
                query,
                headers,
                payload,
                access_key_id=self.access_key_id,
                secret_access_key=self.secret,
                region=self.region,
                service="s3",
                amz_date=req.headers["x-amz-date"],
            )
        except (IndexError, KeyError):
            return self._error(403, "AccessDenied")
        if auth != expected or "host" not in signed:
            return self._error(403, "SignatureDoesNotMatch")
        if req.method == "PUT" and hashlib.sha256(req.body).hexdigest() != payload:
            return self._error(400, "XAmzContentSHA256Mismatch")
        bucket_path = f"/{self.bucket}"
        if path == bucket_path and req.method == "GET":
            return self._list(dict(query))
        if not path.startswith(bucket_path + "/"):
            return self._error(404, "NoSuchBucket")
        key = unquote(path[len(bucket_path) + 1 :])
        if req.method == "PUT":
            self.objects[key] = req.body
            return Reply(200, headers={"ETag": '"x"'})
        if req.method == "HEAD":
            if key not in self.objects:
                return Reply(404)
            return Reply(200, headers={"Content-Length": str(len(self.objects[key]))})
        if req.method == "DELETE":
            self.objects.pop(key, None)
            return Reply(204)
        return self._error(405, "MethodNotAllowed")

    def _list(self, query: dict[str, str]) -> Reply:
        if query.get("list-type") != "2":
            return self._error(400, "InvalidArgument")
        prefix = query.get("prefix", "")
        start = query.get("continuation-token", "")
        limit = min(int(query.get("max-keys", "1000")), self.page_size)
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > start)
        page, more = keys[:limit], len(keys) > limit
        contents = "".join(
            f"<Contents><Key>{escape(k)}</Key><Size>{len(self.objects[k])}</Size></Contents>"
            for k in page
        )
        token = f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>" if more else ""
        return _xml_reply(
            200,
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>{self.bucket}</Name><Prefix>{escape(prefix)}</Prefix>"
            f"<KeyCount>{len(page)}</KeyCount><IsTruncated>{str(more).lower()}</IsTruncated>"
            f"{contents}{token}</ListBucketResult>",
        )


class FakeAzure(_FakeServer):
    """Azure Blob service for one account and container: Shared Key or a SAS token."""

    def __init__(
        self,
        *,
        account: str,
        container: str,
        account_key: str | None = None,
        sas_sig: str | None = None,
        page_size: int = 2,
    ) -> None:
        super().__init__()
        self.account = account
        self.container = container
        self.account_key = account_key
        self.sas_sig = sas_sig
        self.page_size = page_size
        self.blobs: dict[str, bytes] = {}

    def _error(self, status: int, code: str) -> Reply:
        return Reply(status, b"", {"x-ms-error-code": code})

    def _authorized(self, req: Seen, path: str, query: list[tuple[str, str]]) -> bool:
        if "x-ms-version" not in req.headers or "x-ms-date" not in req.headers:
            return False
        if self.sas_sig is not None:
            return dict(query).get("sig") == self.sas_sig and "authorization" not in req.headers
        assert self.account_key is not None
        expected = azure_shared_key_authorization(
            self.account, self.account_key, req.method, path, query, req.headers
        )
        return req.headers.get("authorization") == expected

    def handle(self, req: Seen) -> Reply:
        path, _, raw_query = req.path.partition("?")
        query = parse_qsl(raw_query, keep_blank_values=True)
        if not self._authorized(req, path, query):
            return self._error(403, "AuthenticationFailed")
        params = dict(query)
        root = f"/{self.container}"
        if path == root and params.get("comp") == "list" and req.method == "GET":
            return self._list(params)
        if not path.startswith(root + "/"):
            return self._error(404, "ContainerNotFound")
        name = unquote(path[len(root) + 1 :])
        if req.method == "PUT":
            if req.headers.get("x-ms-blob-type") != "BlockBlob":
                return self._error(400, "MissingRequiredHeader")
            self.blobs[name] = req.body
            return Reply(201)
        if req.method == "HEAD":
            if name not in self.blobs:
                return self._error(404, "BlobNotFound")
            return Reply(200, headers={"Content-Length": str(len(self.blobs[name]))})
        if req.method == "DELETE":
            if self.blobs.pop(name, None) is None:
                return self._error(404, "BlobNotFound")
            return Reply(202)
        return self._error(405, "UnsupportedHttpVerb")

    def _list(self, params: dict[str, str]) -> Reply:
        prefix = params.get("prefix", "")
        marker = params.get("marker", "")
        limit = min(int(params.get("maxresults", "5000")), self.page_size)
        names = sorted(n for n in self.blobs if n.startswith(prefix) and n > marker)
        page, more = names[:limit], len(names) > limit
        blobs = "".join(
            f"<Blob><Name>{escape(n)}</Name><Properties>"
            f"<Content-Length>{len(self.blobs[n])}</Content-Length></Properties></Blob>"
            for n in page
        )
        next_marker = escape(page[-1]) if more else ""
        return _xml_reply(
            200,
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<EnumerationResults ServiceEndpoint="https://x/" ContainerName="{self.container}">'
            f"<Prefix>{escape(prefix)}</Prefix><Blobs>{blobs}</Blobs>"
            f"<NextMarker>{next_marker}</NextMarker></EnumerationResults>",
        )


class FakeWebDav(_FakeServer):
    """A WebDAV folder tree under ``base`` with HTTP Basic login."""

    def __init__(self, *, username: str, password: str, base: str) -> None:
        super().__init__()
        self.credentials = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.base = base.rstrip("/")
        self.folders: set[str] = set()
        parts = self.base.split("/")
        for depth in range(1, len(parts) + 1):
            self.folders.add("/".join(parts[:depth]) or "/")
        self.files: dict[str, bytes] = {}

    def _parent_ok(self, path: str) -> bool:
        return (path.rsplit("/", 1)[0] or "/") in self.folders

    def handle(self, req: Seen) -> Reply:
        if req.headers.get("authorization") != self.credentials:
            return Reply(401, headers={"WWW-Authenticate": 'Basic realm="dav"'})
        path = unquote(urlsplit(req.path).path).rstrip("/") or "/"
        if req.method == "PROPFIND":
            return self._propfind(path, req.headers.get("depth", "1"))
        if req.method == "MKCOL":
            if path in self.folders or path in self.files:
                return Reply(405)
            if not self._parent_ok(path):
                return Reply(409)
            self.folders.add(path)
            return Reply(201)
        if req.method == "PUT":
            if not self._parent_ok(path) or path in self.folders:
                return Reply(409)
            existed = path in self.files
            self.files[path] = req.body
            return Reply(204 if existed else 201)
        if req.method == "MOVE":
            dest = unquote(urlsplit(req.headers.get("destination", "")).path).rstrip("/")
            if path not in self.files:
                return Reply(404)
            if not self._parent_ok(dest):
                return Reply(409)
            existed = dest in self.files
            if existed and req.headers.get("overwrite", "T") != "T":
                return Reply(412)
            self.files[dest] = self.files.pop(path)
            return Reply(204 if existed else 201)
        if req.method == "DELETE":
            if path in self.files:
                del self.files[path]
                return Reply(204)
            if path in self.folders:
                self.folders = {
                    f for f in self.folders if f != path and not f.startswith(path + "/")
                }
                self.files = {k: v for k, v in self.files.items() if not k.startswith(path + "/")}
                return Reply(204)
            return Reply(404)
        return Reply(405)

    def _entry(self, path: str) -> str:
        if path in self.folders:
            href = quote(path.rstrip("/") + "/")
            props = "<d:resourcetype><d:collection/></d:resourcetype>"
            missing = "<d:propstat><d:prop><d:getcontentlength/></d:prop>"
            missing += "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        else:
            href = quote(path)
            props = (
                f"<d:resourcetype/><d:getcontentlength>{len(self.files[path])}</d:getcontentlength>"
            )
            missing = ""
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
            f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat>{missing}</d:response>"
        )

    def _propfind(self, path: str, depth: str) -> Reply:
        if path not in self.folders and path not in self.files:
            return Reply(404)
        entries = [self._entry(path)]
        if depth == "1" and path in self.folders:
            children = [
                p
                for p in sorted(self.folders | set(self.files))
                if p != path and p.rsplit("/", 1)[0] == path
            ]
            entries.extend(self._entry(c) for c in children)
        return _xml_reply(
            207,
            '<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:">'
            + "".join(entries)
            + "</d:multistatus>",
        )
