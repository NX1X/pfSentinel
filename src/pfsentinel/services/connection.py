"""SSH and HTTP/HTTPS connectors for pfSense."""

from __future__ import annotations

import fnmatch
import io
import re
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from pfsentinel.models.device import ConnectionMethod, DeviceConfig, DeviceStatus
from pfsentinel.services.credentials import CredentialService

ProgressCallback = Callable[[str, int], None]

# pfSense config file path on the device
PFSENSE_CONFIG_PATH = "/cf/conf/config.xml"


class ConnectionError(Exception):
    """Raised when a connection cannot be established."""


class AuthenticationError(ConnectionError):
    """Raised when credentials are rejected."""


class SSHConnector:
    """Connect to pfSense via SSH using Paramiko (password or key-based auth)."""

    # Allowlist of command prefixes permitted for remote execution.
    # Any command not starting with one of these will be rejected.
    ALLOWED_COMMAND_PREFIXES = (
        "zfs ",  # ZFS snapshot/send/destroy operations
        "tar ",  # Archive creation for extra/archive backups
        "cat ",  # File reading
        "ls ",  # Directory listing
        "uname ",  # System info
        "sysctl ",  # System parameters
    )

    def __init__(
        self,
        device: DeviceConfig,
        password: str | None,
        ssh_key_passphrase: str | None = None,
    ) -> None:
        self.device = device
        self._password = password
        self._ssh_key_passphrase = ssh_key_passphrase
        self._client: object | None = None

    def _validate_command(self, command: str) -> None:
        """Validate that a command is in the allowlist.

        Raises ConnectionError if the command prefix is not permitted.
        """
        cmd = command.strip()
        if not any(cmd.startswith(prefix) for prefix in self.ALLOWED_COMMAND_PREFIXES):
            raise ConnectionError(
                f"Command not in allowlist: '{cmd.split()[0]}'. "
                f"Allowed prefixes: {', '.join(p.strip() for p in self.ALLOWED_COMMAND_PREFIXES)}"
            )

    def _get_client(self):
        import paramiko

        return paramiko.SSHClient()

    def connect(self) -> None:
        import paramiko

        client = self._get_client()
        # Load system host keys and ~/.ssh/known_hosts so that hosts with
        # a known key ARE verified strictly.
        client.load_system_host_keys()
        known_hosts = Path.home() / ".ssh" / "known_hosts"
        if known_hosts.is_file():
            client.load_host_keys(str(known_hosts))

        if self.device.strict_host_keys:
            # Strict mode: reject any host not already in known_hosts.
            client.set_missing_host_key_policy(paramiko.RejectPolicy())  # type: ignore[attr-defined]
        else:
            # Permissive mode: log unknown host keys via loguru but allow connection.
            # Pragmatic default for homelab use where host keys change on firmware
            # updates / reinstalls. Do NOT use AutoAddPolicy — it silently accepts
            # any key without logging.
            client.set_missing_host_key_policy(paramiko.WarningPolicy())  # type: ignore[attr-defined]
            logger.warning(
                f"strict_host_keys is disabled for '{self.device.id}'. "
                "Unknown SSH host keys will be accepted with a warning. "
                "Set strict_host_keys=true for MITM protection."
            )

        connect_kwargs: dict = dict(
            hostname=self.device.host,
            port=self.device.ssh_port,
            username=self.device.username,
            timeout=self.device.timeout,
        )

        if self.device.ssh_key_path:
            # Key-based authentication
            connect_kwargs.update(
                key_filename=str(self.device.ssh_key_path),
                passphrase=self._ssh_key_passphrase,
                look_for_keys=False,
                allow_agent=False,
            )
        else:
            # Password authentication
            connect_kwargs.update(
                password=self._password,
                look_for_keys=False,
                allow_agent=False,
            )

        try:
            client.connect(**connect_kwargs)
            self._client = client
            auth_mode = "key" if self.device.ssh_key_path else "password"
            logger.debug(
                f"SSH connected to {self.device.host}:{self.device.ssh_port} ({auth_mode})"
            )
        except paramiko.AuthenticationException as e:
            raise AuthenticationError(f"SSH authentication failed: {e}") from e
        except (paramiko.SSHException, TimeoutError, OSError) as e:
            raise ConnectionError(f"SSH connection failed: {e}") from e

    def disconnect(self) -> None:
        if self._client:
            self._client.close()  # type: ignore[attr-defined]
            self._client = None

    def test(self) -> bool:
        try:
            self.connect()
            self.disconnect()
            return True
        except (ConnectionError, AuthenticationError):
            return False

    def download_config(self) -> str:
        """Download /cf/conf/config.xml via SFTP."""
        if not self._client:
            self.connect()

        try:
            sftp = self._client.open_sftp()  # type: ignore[union-attr]
            buf = io.BytesIO()
            sftp.getfo(PFSENSE_CONFIG_PATH, buf)
            sftp.close()
            return buf.getvalue().decode("utf-8")
        except Exception as e:
            raise ConnectionError(f"Failed to download config via SSH: {e}") from e

    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        """Execute a remote command via SSH.

        Only commands matching ALLOWED_COMMAND_PREFIXES are permitted.

        Returns:
            Tuple of (stdout, stderr, exit_code).
        """
        self._validate_command(command)
        if not self._client:
            self.connect()

        try:
            _, stdout, stderr = self._client.exec_command(  # type: ignore[union-attr]
                command, timeout=timeout
            )
            exit_code = stdout.channel.recv_exit_status()
            return (
                stdout.read().decode("utf-8", errors="replace"),
                stderr.read().decode("utf-8", errors="replace"),
                exit_code,
            )
        except Exception as e:
            raise ConnectionError(f"Failed to execute remote command: {e}") from e

    def download_file(self, remote_path: str, local_path: Path) -> None:
        """Download a single file via SFTP."""
        if not self._client:
            self.connect()

        try:
            sftp = self._client.open_sftp()  # type: ignore[union-attr]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_path, str(local_path))
            sftp.close()
        except Exception as e:
            raise ConnectionError(f"Failed to download {remote_path}: {e}") from e

    def download_files(self, remote_paths: list[str], local_dir: Path) -> list[Path]:
        """Download multiple files via SFTP. Skips files that don't exist.

        Returns list of successfully downloaded local paths.
        """
        if not self._client:
            self.connect()

        local_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []

        try:
            sftp = self._client.open_sftp()  # type: ignore[union-attr]
            for rpath in remote_paths:
                fname = rpath.rsplit("/", 1)[-1]
                local = local_dir / fname
                try:
                    sftp.get(rpath, str(local))
                    downloaded.append(local)
                except FileNotFoundError:
                    logger.warning(f"Remote file not found: {rpath}")
                except Exception as e:
                    logger.warning(f"Failed to download {rpath}: {e}")
            sftp.close()
        except Exception as e:
            raise ConnectionError(f"SFTP download failed: {e}") from e

        return downloaded

    def list_remote_files(self, remote_dir: str, pattern: str = "*") -> list[str]:
        """List files in a remote directory matching a glob pattern."""
        if not self._client:
            self.connect()

        try:
            sftp = self._client.open_sftp()  # type: ignore[union-attr]
            entries = sftp.listdir(remote_dir)
            sftp.close()
            return [f"{remote_dir}/{e}" for e in entries if fnmatch.fnmatch(e, pattern)]
        except FileNotFoundError:
            logger.warning(f"Remote directory not found: {remote_dir}")
            return []
        except Exception as e:
            raise ConnectionError(f"Failed to list {remote_dir}: {e}") from e

    def stream_command_to_file(
        self,
        command: str,
        local_path: Path,
        timeout: int = 600,
        warn_exit_codes: set[int] | None = None,
    ) -> int:
        """Execute command and stream stdout directly to a local file.

        Used for ZFS send and tar piped output.
        Only commands matching ALLOWED_COMMAND_PREFIXES are permitted.
        Returns number of bytes written.

        Args:
            warn_exit_codes: Exit codes to treat as warnings (log but don't raise).
                Useful for tar which exits 1 on non-fatal issues like permission denied.
        """
        self._validate_command(command)
        if not self._client:
            self.connect()

        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            _, stdout, stderr = self._client.exec_command(  # type: ignore[union-attr]
                command, timeout=timeout
            )
            # Set socket-level read timeout to prevent indefinite blocking
            stdout.channel.settimeout(float(timeout))
            bytes_written = 0
            with open(local_path, "wb") as f:
                while True:
                    chunk = stdout.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_written += len(chunk)

            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode("utf-8", errors="replace")
                if warn_exit_codes and exit_code in warn_exit_codes:
                    logger.warning(
                        f"Command exited with code {exit_code} (non-fatal): {err.strip()}"
                    )
                else:
                    raise ConnectionError(f"Remote command exited with code {exit_code}: {err}")
            return bytes_written
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(f"Stream command failed: {e}") from e

    def __enter__(self) -> SSHConnector:
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()


class HTTPSConnector:
    """Connect to pfSense via HTTP or HTTPS web interface."""

    def __init__(self, device: DeviceConfig, password: str, use_https: bool = True) -> None:
        self.device = device
        self._password = password
        self._use_https = use_https

    @property
    def _base_url(self) -> str:
        if self._use_https:
            return f"https://{self.device.host}:{self.device.https_port}"
        return f"http://{self.device.host}:{self.device.http_port}"

    def _make_session(self):
        import warnings

        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.exceptions import InsecureRequestWarning
        from urllib3.util.retry import Retry

        session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        # Prefer CA cert file for self-signed certificates over disabling verification
        if self.device.ca_cert_path and self.device.ca_cert_path.is_file():
            session.verify = str(self.device.ca_cert_path)
        else:
            session.verify = self.device.verify_ssl
        session.headers.update({"User-Agent": "pfSentinel/3.0"})

        # Suppress SSL warnings per-session (not globally) for self-signed certs
        if not self.device.verify_ssl and not self.device.ca_cert_path:
            # Use warnings filter scoped to this session's lifetime instead of
            # urllib3.disable_warnings() which affects the entire process.
            warnings.filterwarnings("ignore", category=InsecureRequestWarning)
            logger.warning(
                f"SSL verification disabled for device '{self.device.id}'. "
                "Consider setting ca_cert_path to a CA certificate instead."
            )

        return session

    def _extract_csrf_token(self, html: str) -> str | None:
        """Extract CSRF token from pfSense HTML page."""
        # Try lxml first, fall back to regex
        try:
            from lxml import etree

            parser = etree.HTMLParser()
            tree = etree.fromstring(html.encode(), parser)
            for el in tree.xpath('//input[@name="__csrf_magic"]'):
                return el.get("value")
        except Exception:
            pass

        # Fallback: regex (handles single/double quotes and attributes between name/value)
        for pattern in (
            r"""name=["']__csrf_magic["'][^>]*value=["']([^"']+)["']""",
            r"""value=["']([^"']+)["'][^>]*name=["']__csrf_magic["']""",
        ):
            m = re.search(pattern, html)
            if m:
                return m.group(1)
        logger.debug("CSRF extraction failed. Page snippet: %s", html[:500])
        return None

    def _login(self, session) -> None:
        login_url = f"{self._base_url}/index.php"
        try:
            resp = session.get(login_url, timeout=self.device.timeout)
            resp.raise_for_status()
        except Exception as e:
            logger.debug(f"Web UI connection error: {e}")
            raise ConnectionError(f"Cannot reach pfSense web UI at {self.device.host}") from e

        csrf = self._extract_csrf_token(resp.text)
        if not csrf:
            raise ConnectionError("Could not extract CSRF token from login page")

        payload = {
            "__csrf_magic": csrf,
            "usernamefld": self.device.username,
            "passwordfld": self._password,
            "login": "Sign In",
        }

        try:
            resp = session.post(login_url, data=payload, timeout=self.device.timeout)
            resp.raise_for_status()
        except Exception as e:
            logger.debug(f"Login POST error: {e}")
            raise AuthenticationError(f"Login failed for {self.device.host}") from e

        if (
            "username" in resp.text.lower()
            and "password" in resp.text.lower()
            and "sign in" in resp.text.lower()
        ):
            raise AuthenticationError("Login failed: invalid credentials")

    def test(self) -> bool:
        try:
            session = self._make_session()
            self._login(session)
            return True
        except (ConnectionError, AuthenticationError):
            return False

    def download_config(self, area: str = "", no_packages: bool = False) -> str:
        """Download config via pfSense backup page.

        Args:
            area: Specific config section to back up (empty = full config).
                  Valid values: aliases, captiveportal, cert, dhcpd, filter,
                  interfaces, ipsec, nat, openvpn, routes, services, shaper,
                  syslog, system, users, wol
            no_packages: Exclude package config from backup.
        """
        session = self._make_session()
        self._login(session)

        backup_url = f"{self._base_url}/diag_backup.php"

        # Get backup page for CSRF token
        resp = session.get(backup_url, timeout=self.device.timeout)
        resp.raise_for_status()

        csrf = self._extract_csrf_token(resp.text)
        if not csrf:
            raise ConnectionError("Could not extract CSRF token from backup page")

        # POST to download config
        payload = {
            "__csrf_magic": csrf,
            "download": "Download configuration as XML",
            "donotbackuprrd": "on",
            "backuparea": area,
            "nopackages": "on" if no_packages else "",
            "backupdata": "",
        }

        resp = session.post(backup_url, data=payload, timeout=self.device.timeout)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "xml" not in content_type and "application/octet-stream" not in content_type:
            raise ConnectionError(
                f"Unexpected content type from backup: {content_type}. "
                "The download may have failed or redirected to an error page."
            )

        return resp.text


class ConnectionManager:
    """Orchestrates connectors with primary/fallback method."""

    def __init__(self, device: DeviceConfig, credential_service: CredentialService) -> None:
        self.device = device
        self._creds = credential_service
        self._last_successful_method: ConnectionMethod | None = None

    def _get_password(self) -> str | None:
        """Get device password.

        Returns None if the device uses SSH key auth (no password needed for SSH).
        Raises ConnectionError if no password and no key auth configured.
        """
        pw = self._creds.get(self.device.id)
        if pw is None and not self.device.ssh_key_path:
            raise ConnectionError(
                f"No password stored for device '{self.device.id}'. Run: pfs device add"
            )
        return pw  # may be None when key auth is configured

    def _make_connector(self, method: ConnectionMethod, password: str | None):
        if method == ConnectionMethod.SSH:
            passphrase = self._creds.get_ssh_key_passphrase(self.device.id)
            return SSHConnector(self.device, password, ssh_key_passphrase=passphrase)
        elif method == ConnectionMethod.HTTPS:
            if password is None:
                raise ConnectionError(
                    f"No password stored for '{self.device.id}' — required for HTTPS. "
                    "SSH key auth only works for SSH method."
                )
            return HTTPSConnector(self.device, password, use_https=True)
        else:
            if password is None:
                raise ConnectionError(
                    f"No password stored for '{self.device.id}' — required for HTTP."
                )
            return HTTPSConnector(self.device, password, use_https=False)

    def download_config(
        self,
        progress: ProgressCallback | None = None,
        area: str = "",
        no_packages: bool = False,
    ) -> tuple[str, str]:
        """Download config, trying primary then fallback method.

        Args:
            area: Specific config section (empty = full backup). HTTPS only.
            no_packages: Exclude package config. HTTPS only.

        Returns: (xml_content, method_used)
        """
        password = self._get_password()
        methods_to_try = [self.device.primary_method]
        if (
            self.device.fallback_method
            and self.device.fallback_method != self.device.primary_method
        ):
            methods_to_try.append(self.device.fallback_method)

        last_error: Exception | None = None

        for method in methods_to_try:
            if progress:
                progress(f"Connecting via {method.value.upper()}...", 10)
            try:
                connector = self._make_connector(method, password)
                if isinstance(connector, SSHConnector):
                    with connector:
                        xml = connector.download_config()
                else:
                    xml = connector.download_config(area=area, no_packages=no_packages)

                self._last_successful_method = method
                logger.info(f"Config downloaded via {method.value} from {self.device.host}")
                return xml, method.value

            except AuthenticationError:
                # Don't try fallback on auth errors - credentials are wrong
                raise
            except ConnectionError as e:
                last_error = e
                logger.warning(f"{method.value} failed for {self.device.id}: {e}")
                continue

        raise ConnectionError(
            f"All connection methods failed for '{self.device.id}'. Last error: {last_error}"
        )

    def test_all(self) -> DeviceStatus:
        """Test all connection methods and return status."""
        status = DeviceStatus(device_id=self.device.id)

        try:
            password = self._get_password()
        except ConnectionError as e:
            status.error = str(e)
            return status

        for method in [ConnectionMethod.SSH, ConnectionMethod.HTTPS, ConnectionMethod.HTTP]:
            try:
                connector = self._make_connector(method, password)
                if isinstance(connector, SSHConnector):
                    connector.connect()
                    connector.disconnect()
                else:
                    connector._login(connector._make_session())
                reachable = True
                err_msg = None
            except AuthenticationError as e:
                reachable = False
                err_msg = f"Authentication failed: {e}"
            except ConnectionError as e:
                reachable = False
                err_msg = str(e)
            except Exception as e:
                reachable = False
                err_msg = str(e)

            if method == ConnectionMethod.SSH:
                status.ssh_reachable = reachable
                status.ssh_error = err_msg
            elif method == ConnectionMethod.HTTPS:
                status.https_reachable = reachable
                status.https_error = err_msg
            else:
                status.http_reachable = reachable
                status.http_error = err_msg

        return status
