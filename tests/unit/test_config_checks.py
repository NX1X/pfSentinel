"""Risky-change alerts and the restore check (operator request, 2026-11-20)."""

from __future__ import annotations

import pytest

from pfsentinel.services.config_checks import detect_risky_changes, restore_check

BCRYPT = "$2y$10$abcdefghijklmnopqrstuuSECRETHASHVALUE0123456789abcdefg"

BASE = f"""<?xml version="1.0"?>
<pfsense>
  <system>
    <hostname>fw</hostname>
    <group><name>admins</name><gid>1999</gid><member>0</member><priv>page-all</priv></group>
    <group><name>backup-operators</name><gid>2000</gid><member>2001</member>
      <priv>user-shell-access</priv></group>
    <user><name>admin</name><uid>0</uid><groupname>admins</groupname>
      <bcrypt-hash>{BCRYPT}</bcrypt-hash></user>
    <user><name>backup-user</name><uid>2001</uid><groupname>backup-operators</groupname></user>
    <webgui><protocol>https</protocol><port>443</port><ssl-certref>abc</ssl-certref></webgui>
    <ssh><enable>enabled</enable><sshdkeyonly>enabled</sshdkeyonly></ssh>
  </system>
  <interfaces><wan><if>em0</if></wan><lan><if>em1</if></lan></interfaces>
  <filter>
    <rule><tracker>100</tracker><type>pass</type><interface>lan</interface>
      <descr>LAN out</descr><updated><time>1</time></updated></rule>
  </filter>
  <nat>
    <rule><tracker>200</tracker><protocol>tcp</protocol><local-port>8080</local-port>
      <descr>Web server</descr></rule>
  </nat>
  <ca><refid>ca1</refid><descr>Home CA</descr><prv>PRIVATE-KEY-DATA</prv></ca>
  <cert><refid>abc</refid></cert>
</pfsense>
"""


def _edit(old: str, new: str) -> str:
    assert old in BASE, old
    return BASE.replace(old, new)


def _summaries(new_xml: str) -> list[str]:
    return [c.summary for c in detect_risky_changes(BASE, new_xml)]


def test_identical_configs_have_no_risky_changes() -> None:
    assert detect_risky_changes(BASE, BASE) == []


def test_only_a_timestamp_changed_is_not_a_risk() -> None:
    assert _summaries(_edit("<time>1</time>", "<time>2</time>")) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "<user><name>backup-user</name>",
            "<user><name>eve</name><uid>3000</uid><priv>page-all</priv></user>"
            "<user><name>backup-user</name>",
            "New admin user 'eve'",
        ),
        (
            "<groupname>backup-operators</groupname></user>",
            "<groupname>admins</groupname></user>",
            "User 'backup-user' gained admin rights",
        ),
        (BCRYPT, "$2y$10$NEWHASH", "Admin user 'admin' was changed"),
        (
            "<webgui>",
            "<webgui><authmode>RADIUS-MFA</authmode>",
            "GUI authentication server: Local Database -> RADIUS-MFA",
        ),
        (
            "<interface>lan</interface>",
            "<interface>wan</interface>",
            "WAN pass rule new: 'LAN out' (any, port any)",
        ),
        (
            "<local-port>8080</local-port>",
            "<local-port>22</local-port>",
            "Port forward changed: 'Web server' (tcp, port 22)",
        ),
        (
            "<sshdkeyonly>enabled</sshdkeyonly>",
            "",
            "SSH login with a password alone is now on",
        ),
        ("<webgui>", "<webgui><noantilockout/>", "Anti-lockout rule turned OFF"),
        (
            "<protocol>https</protocol>",
            "<protocol>http</protocol>",
            "Web GUI protocol changed: https -> http",
        ),
        (
            "<ssl-certref>abc</ssl-certref>",
            "<ssl-certref>xyz</ssl-certref>",
            "Web GUI certificate changed",
        ),
    ],
)
def test_each_risky_change_is_reported_once(old: str, new: str, expected: str) -> None:
    assert _summaries(_edit(old, new)) == [expected]


def test_a_group_gaining_admin_also_names_its_members() -> None:
    assert _summaries(_edit("<priv>user-shell-access</priv>", "<priv>page-all</priv>")) == [
        "User 'backup-user' gained admin rights",
        "Group 'backup-operators' has admin rights now",
    ]


def test_messages_never_contain_secrets() -> None:
    changed = _edit(BCRYPT, "$2y$10$NEWHASH").replace("PRIVATE-KEY-DATA", "OTHER-KEY")
    text = " ".join(_summaries(changed))
    assert "$2y$" not in text and "KEY" not in text and "NEWHASH" not in text


class TestRestoreCheck:
    def test_realistic_config_passes_with_counts(self) -> None:
        result = restore_check(BASE)
        assert result.ok
        assert result.counts == {
            "users": 2,
            "admins": 1,
            "interfaces": 2,
            "rules": 1,
            "CAs": 1,
            "certificates": 1,
        }
        assert result.summary.startswith("OK: 2 users, 1 admins")

    @pytest.mark.parametrize(
        ("old", "new", "problem"),
        [
            (
                "<interfaces><wan><if>em0</if></wan><lan><if>em1</if></lan></interfaces>",
                "",
                "no interfaces",
            ),
            ("<member>0</member><priv>page-all</priv>", "", "no user with admin rights"),
            ("<filter>", "<notfilter>", "no firewall filter section"),
        ],
    )
    def test_missing_sections_fail(self, old: str, new: str, problem: str) -> None:
        xml = (
            _edit(old, new).replace("</filter>", "</notfilter>")
            if old == "<filter>"
            else _edit(old, new)
        )
        result = restore_check(xml)
        assert not result.ok
        assert problem in result.summary

    def test_unparseable_xml_fails(self) -> None:
        result = restore_check("<pfsense><unclosed></pfsense>")
        assert not result.ok and "does not parse" in result.summary

    def test_no_ca_is_a_note_not_a_failure(self) -> None:
        result = restore_check(
            _edit(
                "<ca><refid>ca1</refid><descr>Home CA</descr><prv>PRIVATE-KEY-DATA</prv></ca>", ""
            )
        )
        assert result.ok and "no CA" in result.summary
