"""In-process SMTP server on 127.0.0.1 for the email alert tests.

Speaks enough ESMTP for smtplib: EHLO, STARTTLS, AUTH PLAIN and LOGIN,
MAIL, RCPT, DATA, RSET, NOOP and QUIT. It can also start with TLS (like
port 465). TLS uses the self-signed certificate of the fake HTTPS server
(SAN 127.0.0.1).

Usage:
    server = FakeSMTPServer.start(starttls=True, login=("me", "secret"))
    try:
        ...  # send to server.host:server.port
        server.messages[0].message["Subject"]
    finally:
        server.stop()
"""

from __future__ import annotations

import base64
import email
import socketserver
import ssl
import threading
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Self

from tests.e2e.fake_https_server import _get_cert_material


@dataclass
class ReceivedMail:
    mail_from: str
    rcpt_to: list[str]
    data: bytes

    @property
    def message(self) -> Message:
        return email.message_from_bytes(self.data)


def _server_tls_context() -> tuple[ssl.SSLContext, Path]:
    cert_pem, key_pem, cert_path = _get_cert_material()
    key_dir = Path(mkdtemp(prefix="pfsentinel-fake-smtp-"))
    key_path = key_dir / "key.pem"
    key_path.write_bytes(key_pem)
    chain = key_dir / "cert.pem"
    chain.write_bytes(cert_pem)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain, key_path)
    return context, cert_path


class _Handler(socketserver.StreamRequestHandler):
    server: _TCPServer

    def _send(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8") + b"\r\n")
        self.wfile.flush()

    def _readline(self) -> str | None:
        raw = self.rfile.readline(65536)
        if not raw:
            return None
        self.server.fake.transcript.append(raw)
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def _start_tls(self) -> None:
        self.connection = self.server.fake.tls_context.wrap_socket(
            self.connection, server_side=True
        )
        self.request = self.connection
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb")

    def handle(self) -> None:
        fake = self.server.fake
        tls = fake.implicit_tls
        if tls:
            self._start_tls()
        self._send("220 fake.smtp.test ESMTP ready")
        authed = False
        mail_from: str | None = None
        rcpt: list[str] = []
        while True:
            line = self._readline()
            if line is None:
                return
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            if verb in ("EHLO", "HELO"):
                extensions = ["fake.smtp.test", "SIZE 1000000", "8BITMIME"]
                if fake.starttls and not tls:
                    extensions.append("STARTTLS")
                if fake.login is not None and (tls or fake.auth_in_plain_text):
                    extensions.append("AUTH PLAIN LOGIN")
                for ext in extensions[:-1]:
                    self._send(f"250-{ext}")
                self._send(f"250 {extensions[-1]}")
            elif verb == "STARTTLS" and fake.starttls and not tls:
                self._send("220 2.0.0 Ready to start TLS")
                self._start_tls()
                tls = True
            elif verb == "AUTH" and fake.login is not None:
                authed = self._auth(arg)
            elif verb == "MAIL":
                if fake.login is not None and not authed:
                    self._send("530 5.7.0 Authentication required")
                    continue
                mail_from, rcpt = arg.partition("<")[2].partition(">")[0], []
                self._send("250 2.1.0 OK")
            elif verb == "RCPT":
                address = arg.partition("<")[2].partition(">")[0]
                if address in fake.reject:
                    self._send("550 5.1.1 No such user here")
                else:
                    rcpt.append(address)
                    self._send("250 2.1.5 OK")
            elif verb == "DATA":
                self._send("354 End data with <CR><LF>.<CR><LF>")
                body = bytearray()
                while True:
                    raw = self.rfile.readline(65536)
                    if not raw or raw == b".\r\n":
                        break
                    body += raw[1:] if raw.startswith(b"..") else raw
                fake.messages.append(ReceivedMail(mail_from or "", rcpt, bytes(body)))
                self._send("250 2.0.0 OK queued")
            elif verb == "RSET":
                mail_from, rcpt = None, []
                self._send("250 2.0.0 OK")
            elif verb == "NOOP":
                self._send("250 2.0.0 OK")
            elif verb == "QUIT":
                self._send("221 2.0.0 Bye")
                return
            else:
                self._send("502 5.5.2 Command not implemented")

    def _auth(self, arg: str) -> bool:
        fake = self.server.fake
        mechanism, _, initial = arg.partition(" ")
        if mechanism.upper() == "PLAIN":
            if not initial:
                self._send("334 ")
                initial = self._readline() or ""
            echo = f"AUTH PLAIN {initial}"
            _, user, password = base64.b64decode(initial).decode("utf-8").split("\0")
        elif mechanism.upper() == "LOGIN":
            self._send("334 VXNlcm5hbWU6")
            user_b64 = self._readline() or ""
            self._send("334 UGFzc3dvcmQ6")
            password_b64 = self._readline() or ""
            echo = f"{user_b64} {password_b64}"
            user = base64.b64decode(user_b64).decode("utf-8")
            password = base64.b64decode(password_b64).decode("utf-8")
        else:
            self._send("504 5.5.4 Unrecognized authentication type")
            return False
        fake.logins.append((user, password))
        if (user, password) == fake.login and not fake.refuse_login:
            self._send("235 2.7.0 Authentication successful")
            return True
        if fake.echo_auth:
            # Some servers quote the client's line back; the client must not leak it.
            self._send(f"535 5.7.8 Bad credentials: {echo} (password {password})")
        else:
            self._send("535 5.7.8 Authentication credentials invalid")
        return False


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    fake: FakeSMTPServer

    def handle_error(self, request: object, client_address: object) -> None:
        # A client that rejects the certificate drops the handshake: expected here.
        pass


@dataclass
class FakeSMTPServer:
    starttls: bool = False
    implicit_tls: bool = False
    login: tuple[str, str] | None = None
    auth_in_plain_text: bool = False
    refuse_login: bool = False
    echo_auth: bool = False
    reject: set[str] = field(default_factory=set)
    messages: list[ReceivedMail] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    transcript: list[bytes] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 0
    cert_path: Path | None = None
    tls_context: ssl.SSLContext = field(init=False)
    _server: _TCPServer | None = None
    _thread: threading.Thread | None = None

    @classmethod
    def start(cls, **options: Any) -> Self:
        fake = cls(**options)
        fake.tls_context, fake.cert_path = _server_tls_context()
        server = _TCPServer((fake.host, 0), _Handler)
        server.fake = fake
        fake.port = server.server_address[1]
        fake._server = server
        fake._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        fake._thread.start()
        return fake

    def commands(self) -> list[str]:
        """The command verbs received, in order (MAIL, RCPT, DATA, ...)."""
        return [ln.split(b" ", 1)[0].strip().upper().decode() for ln in self.transcript]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
