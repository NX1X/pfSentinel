"""In-process HTTPS server that mimics the pfSense web UI backup flow.

Emulates just enough of pfSense's /index.php login page and
/diag_backup.php form to exercise the real HTTPSConnector end-to-end
against a local TLS socket. A self-signed ECDSA certificate is minted
in-process (via `cryptography`) and cached at module scope so the ECDSA
key generation only happens once per session.

Usage:
    server = FakeHTTPSServer.start(
        username="admin",
        password="hunter2",
        config_xml="<pfsense>...</pfsense>",
    )
    try:
        session_url = f"https://{server.host}:{server.port}/index.php"
        # ... drive the connector against 127.0.0.1:server.port,
        # passing server.cert_pem_path as ca_cert_path ...
    finally:
        server.stop()
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import re
import ssl
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import mkdtemp
from typing import Self
from urllib.parse import parse_qs

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# Session-wide cert cache: cert generation is the slowest thing we do,
# so we mint one ECDSA P-256 cert per process and reuse it across every
# server instance.
_CERT_LOCK = threading.Lock()
_CACHED_CERT_PEM: bytes | None = None
_CACHED_KEY_PEM: bytes | None = None
_CACHED_CERT_PATH: Path | None = None


def _get_cert_material() -> tuple[bytes, bytes, Path]:
    """Return (cert_pem, key_pem, cert_pem_path), generating once per process."""
    global _CACHED_CERT_PEM, _CACHED_KEY_PEM, _CACHED_CERT_PATH
    with _CERT_LOCK:
        if _CACHED_CERT_PEM is not None:
            assert _CACHED_KEY_PEM is not None
            assert _CACHED_CERT_PATH is not None
            return _CACHED_CERT_PEM, _CACHED_KEY_PEM, _CACHED_CERT_PATH

        key = ec.generate_private_key(ec.SECP256R1())
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = _dt.datetime.now(_dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                        x509.DNSName("localhost"),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # Persist to a tmp file so requests can pass verify=<path>.
        cert_dir = Path(mkdtemp(prefix="pfsentinel-fake-https-"))
        cert_path = cert_dir / "server.pem"
        cert_path.write_bytes(cert_pem)

        _CACHED_CERT_PEM = cert_pem
        _CACHED_KEY_PEM = key_pem
        _CACHED_CERT_PATH = cert_path
        return cert_pem, key_pem, cert_path


def _login_page_html(csrf_token: str, include_csrf: bool = True) -> str:
    csrf_input = (
        f'<input type="hidden" name="__csrf_magic" value="{csrf_token}">' if include_csrf else ""
    )
    return f"""<!DOCTYPE html>
<html><head><title>pfSense Login</title></head>
<body>
<form method="post" action="/index.php">
  {csrf_input}
  <label>Username</label><input name="usernamefld" type="text">
  <label>Password</label><input name="passwordfld" type="password">
  <input type="submit" name="login" value="Sign In">
</form>
</body></html>
"""


def _backup_page_html(csrf_token: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>pfSense: Diagnostics: Backup and Restore</title></head>
<body>
<h1>Backup configuration</h1>
<form method="post" action="/diag_backup.php">
  <input type="hidden" name="__csrf_magic" value="{csrf_token}">
  <select name="backuparea">
    <option value="">ALL</option>
    <option value="filter">filter</option>
    <option value="system">system</option>
  </select>
  <input type="checkbox" name="nopackages" value="on">
  <input type="checkbox" name="donotbackuprrd" value="on">
  <input type="checkbox" name="backupdata" value="on">
  <input type="submit" name="download" value="Download configuration as XML">
</form>
</body></html>
"""


def _failed_login_page_html() -> str:
    # Text must contain the "username / password / sign in" trio so the
    # real HTTPSConnector detects it as an auth failure.
    return """<!DOCTYPE html>
<html><body>
<p>Username or password incorrect. Please Sign In again.</p>
</body></html>
"""


@dataclass
class FakeHTTPSServer:
    """A locally-running HTTPS server bound to 127.0.0.1.

    Construct via FakeHTTPSServer.start(...). Always call .stop() (the
    pytest fixture handles that for you).
    """

    username: str
    password: str
    config_xml: bytes
    include_login_csrf: bool = True

    _httpd: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _cert_path: Path | None = None
    _sessions: set[str] = field(default_factory=set)
    _login_tokens: set[str] = field(default_factory=set)
    _backup_tokens: set[str] = field(default_factory=set)
    _requests: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        assert self._httpd is not None, "Server not started"
        return self._httpd.server_address[1]

    @property
    def cert_pem_path(self) -> Path:
        assert self._cert_path is not None, "Server not started"
        return self._cert_path

    @classmethod
    def start(
        cls,
        *,
        username: str = "admin",
        password: str = "hunter2",
        config_xml: str | bytes = "<pfsense></pfsense>",
        include_login_csrf: bool = True,
    ) -> Self:
        if isinstance(config_xml, str):
            config_xml = config_xml.encode("utf-8")

        server = cls(
            username=username,
            password=password,
            config_xml=config_xml,
            include_login_csrf=include_login_csrf,
        )

        cert_pem, key_pem, cert_path = _get_cert_material()
        server._cert_path = cert_path

        handler_cls = _build_handler(server)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Refuse legacy TLS on the fake server (CodeQL py/insecure-protocol).
        # Real pfSentinel does not negotiate <TLS 1.2 either.
        # DevSkim: ignore DS440000 — test fake; TLS floor is the fix, not config.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # Load cert/key from PEM bytes via a temp keypair file the SSL
        # context can consume. keyfile+certfile is simplest.
        ctx.load_cert_chain(
            certfile=str(cert_path),
            keyfile=str(_write_key_alongside(cert_path, key_pem)),
        )
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

        server._httpd = httpd
        server._thread = threading.Thread(
            target=httpd.serve_forever,
            name=f"FakeHTTPS-{server.port}",
            daemon=True,
        )
        server._thread.start()
        return server

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def set_config_xml(self, xml: str | bytes) -> None:
        if isinstance(xml, str):
            xml = xml.encode("utf-8")
        with self._lock:
            self.config_xml = xml

    def set_include_login_csrf(self, include: bool) -> None:
        with self._lock:
            self.include_login_csrf = include

    def last_request(self) -> dict:
        with self._lock:
            if not self._requests:
                return {}
            return dict(self._requests[-1])

    def all_requests(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._requests]

    # --- Internal state helpers used by the handler ---

    def _issue_login_token(self) -> str:
        tok = f"TOK-LOGIN-{uuid.uuid4().hex}"
        with self._lock:
            self._login_tokens.add(tok)
        return tok

    def _issue_backup_token(self) -> str:
        tok = f"TOK-BACKUP-{uuid.uuid4().hex}"
        with self._lock:
            self._backup_tokens.add(tok)
        return tok

    def _consume_login_token(self, tok: str) -> bool:
        with self._lock:
            if tok in self._login_tokens:
                self._login_tokens.discard(tok)
                return True
            return False

    def _consume_backup_token(self, tok: str) -> bool:
        with self._lock:
            if tok in self._backup_tokens:
                self._backup_tokens.discard(tok)
                return True
            return False

    def _issue_session(self) -> str:
        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions.add(sid)
        return sid

    def _has_session(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            return sid in self._sessions

    def _record(self, entry: dict) -> None:
        with self._lock:
            self._requests.append(entry)


def _write_key_alongside(cert_path: Path, key_pem: bytes) -> Path:
    """Write private key next to cert (in the same tmp dir)."""
    key_path = cert_path.parent / "server.key"
    if not key_path.exists():
        key_path.write_bytes(key_pem)
    return key_path


_SESSION_COOKIE = "PHPSESSID"


def _build_handler(server: FakeHTTPSServer) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass bound to this server."""

    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr access log; we already record requests.
        def log_message(self, fmt: str, *args) -> None:  # noqa: N802
            return

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _parse_cookies(self) -> dict[str, str]:
            raw = self.headers.get("Cookie", "")
            out: dict[str, str] = {}
            for part in raw.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
            return out

        def _session_id(self) -> str | None:
            return self._parse_cookies().get(_SESSION_COOKIE)

        def _send_html(
            self, status: int, body: str, extra_headers: dict[str, str] | None = None
        ) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def _send_xml(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Disposition", "attachment; filename=config.xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_status(self, status: int, message: str = "") -> None:
            payload = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # --- GET ---
        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            server._record({"method": "GET", "path": self.path, "headers": dict(self.headers)})

            if path == "/index.php":
                # When the client follows the post-login redirect, return a
                # dashboard page that does NOT contain the "username /
                # password / sign in" trio HTTPSConnector uses to detect
                # auth failure. Only serve the login page for unauth GETs.
                if "ok=1" in query and server._has_session(self._session_id()):
                    self._send_html(
                        200,
                        "<html><body><h1>Dashboard</h1><p>Welcome.</p></body></html>",
                    )
                    return
                tok = server._issue_login_token()
                html = _login_page_html(tok, include_csrf=server.include_login_csrf)
                self._send_html(200, html)
                return

            if path == "/diag_backup.php":
                if not server._has_session(self._session_id()):
                    self._send_status(403, "Not logged in")
                    return
                tok = server._issue_backup_token()
                html = _backup_page_html(tok)
                self._send_html(200, html)
                return

            self._send_status(404, "not found")

        # --- POST ---
        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            raw = self._read_body()
            form_multi = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
            form = {k: v[0] if v else "" for k, v in form_multi.items()}
            server._record(
                {
                    "method": "POST",
                    "path": self.path,
                    "form": form,
                    "headers": dict(self.headers),
                }
            )

            if path == "/index.php":
                self._handle_login_post(form)
                return

            if path == "/diag_backup.php":
                self._handle_backup_post(form)
                return

            self._send_status(404, "not found")

        def _handle_login_post(self, form: dict[str, str]) -> None:
            csrf = form.get("__csrf_magic", "")
            if not server._consume_login_token(csrf):
                self._send_status(403, "invalid csrf")
                return

            user = form.get("usernamefld", "")
            pw = form.get("passwordfld", "")
            if user != server.username or pw != server.password:
                # Return the fake login page with the failure trio so
                # HTTPSConnector detects the auth failure text.
                self._send_html(200, _failed_login_page_html())
                return

            sid = server._issue_session()
            headers = {
                "Set-Cookie": f"{_SESSION_COOKIE}={sid}; Path=/; HttpOnly",
                "Location": "/index.php?ok=1",
            }
            self._send_html(302, "redirecting", extra_headers=headers)

        def _handle_backup_post(self, form: dict[str, str]) -> None:
            if not server._has_session(self._session_id()):
                self._send_status(403, "Not logged in")
                return
            csrf = form.get("__csrf_magic", "")
            if not server._consume_backup_token(csrf):
                self._send_status(403, "invalid csrf")
                return

            # Serve the configured XML back. Test can assert form fields
            # via server.last_request().
            with server._lock:
                xml = server.config_xml
            self._send_xml(xml)

    return Handler


# Small guard so callers that mis-use the module get a sensible error
# rather than a socket exception.
def _validate_endpoint(path: str) -> bool:
    return bool(re.match(r"^/(index|diag_backup)\.php(\?.*)?$", path))
