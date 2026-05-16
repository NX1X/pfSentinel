"""pfSense XML config parsing utilities."""

from __future__ import annotations

from typing import Any
from xml.etree import (
    ElementTree as ET,  # kept for tostring()/ParseError; all parsing goes via defusedxml
)

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring


class PfSenseXMLError(ValueError):
    """Raised when XML does not look like a valid pfSense config."""


def validate_xml(xml_content: str) -> ET.Element:
    """Parse and validate pfSense config XML. Raises PfSenseXMLError on failure."""
    if not xml_content or not xml_content.strip():
        raise PfSenseXMLError("XML content is empty")

    try:
        root = _safe_fromstring(xml_content.encode("utf-8"))
    except ET.ParseError as e:
        raise PfSenseXMLError(f"XML parse error: {e}") from e
    except DefusedXmlException as e:
        raise PfSenseXMLError(f"Unsafe XML rejected (possible XXE/entity attack): {e}") from e

    if root.tag != "pfsense":
        raise PfSenseXMLError(f"Root element is '{root.tag}', expected 'pfsense'")

    # Minimal required sections
    if root.find("system") is None:
        raise PfSenseXMLError("Missing required <system> section")

    return root


def extract_info(xml_content: str) -> dict[str, str | None]:
    """Extract key metadata from pfSense config XML."""
    root = validate_xml(xml_content)

    system = root.find("system")
    assert system is not None  # validated above

    def _text(element: Any, tag: str) -> str | None:
        el = element.find(tag)
        return el.text.strip() if el is not None and el.text else None

    version = root.get("version")
    hostname = _text(system, "hostname")
    domain = _text(system, "domain")
    pfsense_version = _text(system, "version")

    fqdn = f"{hostname}.{domain}" if hostname and domain else hostname

    return {
        "version": version,
        "pfsense_version": pfsense_version,
        "hostname": hostname,
        "domain": domain,
        "fqdn": fqdn,
    }


def extract_sections(xml_content: str) -> dict[str, str]:
    """Extract top-level sections as XML strings for diffing."""
    root = validate_xml(xml_content)
    sections: dict[str, str] = {}
    for child in root:
        sections[child.tag] = ET.tostring(child, encoding="unicode")
    return sections


def count_rules(xml_content: str) -> int:
    """Count firewall rules in config."""
    root = validate_xml(xml_content)
    filter_el = root.find("filter")
    if filter_el is None:
        return 0
    return len(filter_el.findall("rule"))


def list_interfaces(xml_content: str) -> list[str]:
    """List interface names from config."""
    root = validate_xml(xml_content)
    interfaces = root.find("interfaces")
    if interfaces is None:
        return []
    return [iface.tag for iface in interfaces]


def list_users(xml_content: str) -> list[str]:
    """List usernames from config."""
    root = validate_xml(xml_content)
    system = root.find("system")
    if system is None:  # pragma: no cover  (validate_xml already requires <system>)
        return []
    return [u.findtext("name") or "" for u in system.findall("user") if u.findtext("name")]


def list_packages(xml_content: str) -> list[str]:
    """List installed package names from config."""
    root = validate_xml(xml_content)
    pkgs = root.find("installedpackages")
    if pkgs is None:
        return []
    result = []
    for pkg in pkgs.findall("package"):
        name = pkg.findtext("name")
        if name:
            result.append(name)
    return result
