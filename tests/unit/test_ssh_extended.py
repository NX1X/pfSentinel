"""Tests for SSHConnector extended methods (exec_command, download_file, etc.)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.connection import ConnectionError, SSHConnector


@pytest.fixture
def device():
    return DeviceConfig(
        id="test-fw",
        label="Test Firewall",
        host="192.168.1.1",
    )


@pytest.fixture
def connector(device):
    return SSHConnector(device, password="test-pass")


class TestExecCommand:
    def test_exec_command_returns_stdout_stderr_exitcode(self, connector):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"output data"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        stdout, stderr, code = connector.exec_command("ls /tmp")
        assert stdout == "output data"
        assert stderr == ""
        assert code == 0

    def test_exec_command_nonzero_exit(self, connector):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b""
        mock_stdout.channel.recv_exit_status.return_value = 1
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b"command not found"

        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        stdout, stderr, code = connector.exec_command("cat /nonexistent")
        assert code == 1
        assert "command not found" in stderr

    def test_exec_command_rejects_disallowed_command(self, connector):
        mock_client = MagicMock()
        connector._client = mock_client
        with pytest.raises(ConnectionError, match="Command not in allowlist"):
            connector.exec_command("rm -rf /")

    def test_exec_command_connects_if_not_connected(self, connector):
        connector._client = None
        with patch.object(connector, "connect") as mock_connect:
            mock_client = MagicMock()
            mock_stdout = MagicMock()
            mock_stdout.read.return_value = b"ok"
            mock_stdout.channel.recv_exit_status.return_value = 0
            mock_stderr = MagicMock()
            mock_stderr.read.return_value = b""
            mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

            def set_client():
                connector._client = mock_client

            mock_connect.side_effect = set_client
            connector.exec_command("cat /etc/version")
            mock_connect.assert_called_once()


class TestDownloadFile:
    def test_download_file_creates_parent_dirs(self, connector, tmp_path: Path):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp

        local_path = tmp_path / "sub" / "dir" / "file.txt"
        connector.download_file("/remote/file.txt", local_path)

        assert local_path.parent.exists()
        mock_sftp.get.assert_called_once_with("/remote/file.txt", str(local_path))
        mock_sftp.close.assert_called_once()


class TestDownloadFiles:
    def test_download_files_returns_downloaded_paths(self, connector, tmp_path: Path):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp

        # Simulate successful download by actually creating the files
        def mock_get(remote, local):
            Path(local).write_text("data")

        mock_sftp.get.side_effect = mock_get

        paths = connector.download_files(["/remote/a.rrd", "/remote/b.rrd"], tmp_path)
        assert len(paths) == 2

    def test_download_files_skips_missing(self, connector, tmp_path: Path):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp

        def mock_get(remote, local):
            if "missing" in remote:
                raise FileNotFoundError(f"No such file: {remote}")
            Path(local).write_text("data")

        mock_sftp.get.side_effect = mock_get

        paths = connector.download_files(["/remote/exists.rrd", "/remote/missing.rrd"], tmp_path)
        assert len(paths) == 1


class TestListRemoteFiles:
    def test_list_remote_files_with_pattern(self, connector):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp
        mock_sftp.listdir.return_value = ["wan.rrd", "lan.rrd", "README.txt"]

        result = connector.list_remote_files("/var/db/rrd", "*.rrd")
        assert len(result) == 2
        assert "/var/db/rrd/wan.rrd" in result
        assert "/var/db/rrd/lan.rrd" in result

    def test_list_remote_files_missing_dir_returns_empty(self, connector):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp
        mock_sftp.listdir.side_effect = FileNotFoundError("No such directory")

        result = connector.list_remote_files("/nonexistent")
        assert result == []


class TestStreamCommandToFile:
    def test_stream_writes_to_file(self, connector, tmp_path: Path):
        mock_client = MagicMock()
        connector._client = mock_client

        # Simulate chunked stdout output
        chunks = [b"chunk1", b"chunk2", b"chunk3", b""]
        mock_stdout = MagicMock()
        mock_stdout.read.side_effect = chunks
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        dest = tmp_path / "output.tar.gz"
        bytes_written = connector.stream_command_to_file("tar czf - /etc", dest)

        assert bytes_written == 18  # chunk1(6) + chunk2(6) + chunk3(6)
        assert dest.exists()
        assert dest.read_bytes() == b"chunk1chunk2chunk3"

    def test_stream_raises_on_nonzero_exit(self, connector, tmp_path: Path):
        mock_client = MagicMock()
        connector._client = mock_client

        mock_stdout = MagicMock()
        mock_stdout.read.side_effect = [b"partial", b""]
        mock_stdout.channel.recv_exit_status.return_value = 2
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b"tar: error"

        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        dest = tmp_path / "output.tar.gz"
        with pytest.raises(Exception, match="exited with code 2"):
            connector.stream_command_to_file("tar czf - /bad", dest)
