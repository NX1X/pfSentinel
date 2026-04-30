"""Change detection between pfSense backups."""

from __future__ import annotations

import difflib
from pathlib import Path

from loguru import logger

from pfsentinel.models.backup import BackupIndex, BackupRecord, ChangeCategory
from pfsentinel.utils import compression, xml_parser


class DiffService:
    """Detect changes between the current config and the last backup."""

    def __init__(self, backup_root: Path) -> None:
        self._backup_root = backup_root

    def _load_last_xml(self, device_id: str, index: BackupIndex) -> str | None:
        """Load XML content from the most recent backup."""
        latest = index.latest()
        if not latest:
            return None

        path = self._backup_root / device_id / latest.relative_path
        if not path.exists():
            logger.warning(f"Latest backup file not found: {path}")
            return None

        try:
            return compression.read_xml(path)
        except Exception as e:
            logger.warning(f"Could not read latest backup for diff: {e}")
            return None

    def detect(self, device_id: str, current_xml: str, index: BackupIndex) -> list[ChangeCategory]:
        """Compare current XML against last backup and return list of change categories.

        Returns [ChangeCategory.INITIAL] if no prior backup exists.
        Returns [ChangeCategory.MINOR] if no significant changes detected.
        """
        last_xml = self._load_last_xml(device_id, index)

        if last_xml is None:
            logger.info(f"No prior backup for {device_id} - marking as initial")
            return [ChangeCategory.INITIAL]

        changes: list[ChangeCategory] = []

        try:
            current = xml_parser.extract_sections(current_xml)
            last = xml_parser.extract_sections(last_xml)
        except xml_parser.PfSenseXMLError as e:
            logger.warning(f"Could not parse XML for diff: {e}")
            return [ChangeCategory.MINOR]

        # Check interfaces
        if self._section_changed(current.get("interfaces"), last.get("interfaces")):
            changes.append(ChangeCategory.INTERFACES)

        # Check firewall rules
        if self._section_changed(current.get("filter"), last.get("filter")):
            changes.append(ChangeCategory.FIREWALL)

        # Check users (inside system section)
        current_users = xml_parser.list_users(current_xml)
        last_users = xml_parser.list_users(last_xml)
        if sorted(current_users) != sorted(last_users):
            changes.append(ChangeCategory.USERS)

        # Check system section (excluding user-specific parts)
        if self._section_changed(current.get("system"), last.get("system")):
            if ChangeCategory.USERS not in changes:
                changes.append(ChangeCategory.SYSTEM)

        # Check packages
        current_pkgs = xml_parser.list_packages(current_xml)
        last_pkgs = xml_parser.list_packages(last_xml)
        if sorted(current_pkgs) != sorted(last_pkgs):
            changes.append(ChangeCategory.PACKAGES)

        # Check DHCP
        if self._section_changed(current.get("dhcpd"), last.get("dhcpd")):
            changes.append(ChangeCategory.DHCP)

        # Check VPN (OpenVPN + IPsec)
        vpn_sections = ["openvpn", "ipsec"]
        for section in vpn_sections:
            if self._section_changed(current.get(section), last.get(section)):
                if ChangeCategory.VPN not in changes:
                    changes.append(ChangeCategory.VPN)

        # Check routes
        if self._section_changed(current.get("staticroutes"), last.get("staticroutes")):
            changes.append(ChangeCategory.ROUTES)

        if not changes:
            return [ChangeCategory.MINOR]

        logger.info(f"Detected changes for {device_id}: {[c.value for c in changes]}")
        return changes

    def _section_changed(self, current: str | None, last: str | None) -> bool:
        """Return True if two XML section strings differ."""
        if current is None and last is None:
            return False
        if current is None or last is None:
            return True
        # Normalize whitespace before comparison
        return current.strip() != last.strip()

    def generate_text_diff(self, record_a: BackupRecord, record_b: BackupRecord) -> str:
        """Generate unified text diff between two backup files."""
        path_a = self._backup_root / record_a.device_id / record_a.relative_path
        path_b = self._backup_root / record_b.device_id / record_b.relative_path

        if not path_a.exists():
            return f"Error: File not found: {path_a}"
        if not path_b.exists():
            return f"Error: File not found: {path_b}"

        try:
            xml_a = compression.read_xml(path_a)
            xml_b = compression.read_xml(path_b)
        except Exception as e:
            return f"Error reading backup files: {e}"

        lines_a = xml_a.splitlines(keepends=True)
        lines_b = xml_b.splitlines(keepends=True)

        diff = difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=record_a.filename,
            tofile=record_b.filename,
            n=3,
        )
        return "".join(diff)
