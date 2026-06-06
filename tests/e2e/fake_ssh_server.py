"""In-process paramiko SSH + SFTP server for e2e tests.

Serves a configurable set of virtual files (notably /cf/conf/config.xml)
and supports password authentication. Optionally responds to allowlisted
exec commands (zfs, tar, cat, ls, uname, sysctl) with canned output.

Usage:
    server = FakeSSHServer.start(
        username="admin",
        password="hunter2",
        files={"/cf/conf/config.xml": b"<pfsense>...</pfsense>"},
    )
    try:
        device_port = server.port
        # ... run client code against 127.0.0.1:server.port ...
    finally:
        server.stop()
"""

from __future__ import annotations

import io
import socket
import threading
from dataclasses import dataclass, field
from typing import Self

import paramiko

# Host key generated once per process. ECDSA P-256 is ~10x faster than RSA-2048.
_HOST_KEY: paramiko.PKey | None = None


def _host_key() -> paramiko.PKey:
    global _HOST_KEY
    if _HOST_KEY is None:
        _HOST_KEY = paramiko.ECDSAKey.generate()
    return _HOST_KEY


class _StaticSFTPHandle(paramiko.SFTPHandle):
    """Read-only SFTP handle backed by an in-memory bytes buffer."""

    def __init__(self, data: bytes, flags: int = 0) -> None:
        super().__init__(flags)
        self.readfile = io.BytesIO(data)
        self.writefile = None


def _make_sftp_interface(files: dict[str, bytes]):
    """Build an SFTPServerInterface subclass bound to a file map.

    Paramiko's set_subsystem_handler does not give us a clean way to pass
    arbitrary state into the SFTP interface, so we close over `files` via
    a fresh subclass per server instance.
    """

    class _SFTP(paramiko.SFTPServerInterface):
        def _resolve(self, path: str) -> str:
            return path if path.startswith("/") else "/" + path

        def list_folder(self, path: str):
            return paramiko.SFTP_OP_UNSUPPORTED

        def stat(self, path: str):
            key = self._resolve(path)
            if key in files:
                attrs = paramiko.SFTPAttributes()
                attrs.st_size = len(files[key])
                attrs.st_mode = 0o100644
                return attrs
            return paramiko.SFTP_NO_SUCH_FILE

        def lstat(self, path: str):
            return self.stat(path)

        def open(self, path: str, flags: int, attr):
            key = self._resolve(path)
            if key in files:
                return _StaticSFTPHandle(files[key], flags)
            return paramiko.SFTP_NO_SUCH_FILE

    return _SFTP


@dataclass
class ExecResponse:
    """Canned response for an exec_command invocation."""

    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int = 0


@dataclass
class FakeSSHServer:
    """A locally-running paramiko SSH+SFTP server bound to 127.0.0.1.

    Construct via FakeSSHServer.start(...). Always call .stop() (the
    pytest fixture handles that for you).
    """

    username: str
    password: str
    files: dict[str, bytes] = field(default_factory=dict)
    exec_responses: dict[str, ExecResponse] = field(default_factory=dict)

    _sock: socket.socket | None = None
    _accept_thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _transports: list[paramiko.Transport] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def port(self) -> int:
        assert self._sock is not None, "Server not started"
        return self._sock.getsockname()[1]

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @classmethod
    def start(
        cls,
        *,
        username: str = "admin",
        password: str = "hunter2",
        files: dict[str, bytes] | None = None,
        exec_responses: dict[str, ExecResponse] | None = None,
    ) -> Self:
        server = cls(
            username=username,
            password=password,
            files=files or {},
            exec_responses=exec_responses or {},
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        sock.settimeout(0.25)
        server._sock = sock
        server._accept_thread = threading.Thread(
            target=server._accept_loop,
            name=f"FakeSSH-{server.port}",
            daemon=True,
        )
        server._accept_thread.start()
        return server

    def set_file(self, path: str, content: bytes | str) -> None:
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.files[path if path.startswith("/") else "/" + path] = content

    def set_exec_response(
        self,
        command_prefix: str,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
        exit_code: int = 0,
    ) -> None:
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        self.exec_responses[command_prefix] = ExecResponse(
            stdout=stdout, stderr=stderr, exit_code=exit_code
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for t in self._transports:
                try:
                    t.close()
                except Exception:
                    pass
            self._transports.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client_sock, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle_client,
                args=(client_sock,),
                daemon=True,
            ).start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        transport = paramiko.Transport(client_sock)
        with self._lock:
            self._transports.append(transport)
        try:
            transport.add_server_key(_host_key())
            transport.set_subsystem_handler(
                "sftp",
                paramiko.SFTPServer,
                _make_sftp_interface(self.files),
            )
            server_iface = _ServerIface(
                username=self.username,
                password=self.password,
                exec_responses=self.exec_responses,
            )
            try:
                transport.start_server(server=server_iface)
            except paramiko.SSHException:
                return

            # Keep the transport alive until the client disconnects or we stop.
            while transport.is_active() and not self._stop.is_set():
                channel = transport.accept(timeout=0.5)
                if channel is None:
                    continue
                # Channels handled by ServerInterface callbacks (exec) or
                # by the SFTP subsystem handler. We just need to keep the
                # transport pumping.
        finally:
            try:
                transport.close()
            except Exception:
                pass


class _ServerIface(paramiko.ServerInterface):
    """Authenticates clients and dispatches exec_command requests."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        exec_responses: dict[str, ExecResponse],
    ) -> None:
        self._username = username
        self._password = password
        self._exec_responses = exec_responses

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self._username and password == self._password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_exec_request(self, channel, command: bytes) -> bool:
        cmd = command.decode("utf-8", errors="replace")
        response = self._lookup_exec(cmd)

        def _run() -> None:
            try:
                if response.stdout:
                    channel.sendall(response.stdout)
                if response.stderr:
                    channel.sendall_stderr(response.stderr)
                channel.send_exit_status(response.exit_code)
            finally:
                try:
                    channel.close()
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()
        return True

    def _lookup_exec(self, command: str) -> ExecResponse:
        for prefix, resp in self._exec_responses.items():
            if command.startswith(prefix):
                return resp
        # Default: behave like an unknown command (non-zero exit).
        return ExecResponse(stderr=f"unknown: {command}\n".encode(), exit_code=127)
