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
