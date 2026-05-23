"""End-to-end tests.

These tests exercise the real CLI through Typer's CliRunner against a
local paramiko-based fake SSH server. No mocks of the SSH transport;
real sockets, real auth, real SFTP.
"""
