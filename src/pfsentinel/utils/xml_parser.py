"""pfSense XML config parsing utilities.

Parsing is hardened against XXE, entity-expansion ("billion laughs") and
external-resource attacks. pfSense config.xml is fetched over the network from
a device pfSentinel does not control, so it is treated as untrusted input.

The hardening has two layers:

1. A restricted ``lxml`` parser (no entity resolution, no network, no DTD
   loading, no huge trees, no error recovery).
2. An explicit DOCTYPE rejection, so a config carrying *any* DTD fails loudly
   rather than parsing into a document with silently-unresolved entity refs.
"""

from __future__ import annotations

from typing import Any

from lxml import etree


def _make_parser() -> etree.XMLParser:
    """Build the hardened parser used for every parse in this module.

    A fresh parser per call: libxml2 parsers carry mutable error state and are
    not safe to share across threads, and pfSentinel parses concurrently during
    multi-device backups.
    """
    return etree.XMLParser(
        resolve_entities=False,  # never expand entity refs (XXE / billion laughs)
        no_network=True,  # never fetch external resources
        load_dtd=False,  # never load an external DTD
        dtd_validation=False,
        huge_tree=False,  # keep libxml2's built-in expansion/depth limits
        recover=False,  # malformed input fails instead of being silently repaired
    )


class PfSenseXMLError(ValueError):
    """Raised when XML does not look like a valid pfSense config."""


def validate_xml(xml_content: str) -> etree._Element:
    """Parse and validate pfSense config XML. Raises PfSenseXMLError on failure."""
    if not xml_content or not xml_content.strip():
        raise PfSenseXMLError("XML content is empty")

    try:
        # Encode first: lxml refuses str input carrying an encoding declaration.
        # Suppression note (S320): the flagged risk is exactly what _make_parser()
        # neutralises - entity resolution, network access, DTD loading and huge
        # trees are all disabled, and any DOCTYPE is rejected below. Covered by
        # TestXxeHardening in tests/unit/test_xml_parser.py.
        root = etree.fromstring(  # noqa: S320
            xml_content.encode("utf-8"), parser=_make_parser()
        )
    except etree.XMLSyntaxError as e:
        raise PfSenseXMLError(f"XML parse error: {e}") from e

    # A pfSense config has no legitimate reason to carry a DTD. Rejecting it
    # outright makes entity-based attacks fail loudly instead of parsing into a
    # document with unresolved entity references.
    docinfo = root.getroottree().docinfo
    if docinfo.internalDTD is not None or docinfo.externalDTD is not None:
        raise PfSenseXMLError(
            "Unsafe XML rejected (possible XXE/entity attack): "
            "config declares a DOCTYPE/DTD, which is not permitted"
        )

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
    assert system is not None  # validated above  # noqa: S101

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
        # Comments and PIs are _Element subclasses whose .tag is a callable,
        # not a str; skip them so only real config sections are returned.
        if not isinstance(child.tag, str):
            continue
        sections[child.tag] = etree.tostring(child, encoding="unicode")
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
    return [iface.tag for iface in interfaces if isinstance(iface.tag, str)]


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
