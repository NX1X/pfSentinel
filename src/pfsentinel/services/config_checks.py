"""Checks on a pfSense config.xml: risky changes and a restore check.

Both read the XML with the hardened parser in utils/xml_parser.py. Messages
name settings, users and rules, never values such as password hashes, keys
or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from pfsentinel.utils import xml_parser

# Rule fields that change on every edit without changing what the rule does.
_VOLATILE = {"created", "updated"}
ADMIN_PRIV = "page-all"


@dataclass(frozen=True)
class RiskyChange:
    kind: str  # admin, auth-server, wan-rule, port-forward, ssh-password, anti-lockout, webgui,
    # count-drop
    summary: str


def _text(root: Any, path: str) -> str | None:
    node = root.find(path)
    if node is None:
        return None
    return (node.text or "").strip()


def _canonical(element: Any) -> bytes:
    """The element without volatile children, as bytes for comparison."""
    copy = etree.fromstring(etree.tostring(element))
    for child in list(copy):
        if child.tag in _VOLATILE:
            copy.remove(child)
    return etree.tostring(copy, method="c14n")


def _admin_users(root: Any) -> dict[str, bytes]:
    """name -> canonical XML of every user with admin rights (directly or by group)."""
    admin_groups = {
        _text(g, "name")
        for g in root.findall("system/group")
        if ADMIN_PRIV in [(p.text or "").strip() for p in g.findall("priv")]
    }
    admin_uids = {
        (m.text or "").strip()
        for g in root.findall("system/group")
        if _text(g, "name") in admin_groups
        for m in g.findall("member")
    }
    users: dict[str, bytes] = {}
    for user in root.findall("system/user"):
        name = _text(user, "name") or "?"
        privs = [(p.text or "").strip() for p in user.findall("priv")]
        groups = {(g.text or "").strip() for g in user.findall("groupname")}
        if (
            ADMIN_PRIV in privs
            or _text(user, "uid") in admin_uids
            or groups & {g for g in admin_groups if g}
        ):
            users[name] = _canonical(user)
    return users


def _admin_groups(root: Any) -> dict[str, bytes]:
    return {
        _text(g, "name") or "?": _canonical(g)
        for g in root.findall("system/group")
        if ADMIN_PRIV in [(p.text or "").strip() for p in g.findall("priv")]
    }


def _rule_key(rule: Any) -> str:
    return _text(rule, "tracker") or _canonical(rule).decode("utf-8", "replace")


def _rule_label(rule: Any) -> str:
    descr = _text(rule, "descr") or "no description"
    proto = _text(rule, "protocol") or "any"
    port = _text(rule, "destination/port") or _text(rule, "local-port") or "any"
    return f"'{descr}' ({proto}, port {port})"


def _wan_pass_rules(root: Any) -> dict[str, tuple[bytes, str]]:
    rules = {}
    for rule in root.findall("filter/rule"):
        interfaces = (_text(rule, "interface") or "").split(",")
        if "wan" in interfaces and (_text(rule, "type") or "pass") == "pass":
            if rule.find("disabled") is None:
                rules[_rule_key(rule)] = (_canonical(rule), _rule_label(rule))
    return rules


def _port_forwards(root: Any) -> dict[str, tuple[bytes, str]]:
    return {
        _rule_key(rule): (_canonical(rule), _rule_label(rule))
        for rule in root.findall("nat/rule")
        if rule.find("disabled") is None
    }


def _new_or_changed(
    old: dict[str, tuple[bytes, str]], new: dict[str, tuple[bytes, str]]
) -> list[tuple[str, str]]:
    out = []
    for key, (canon, label) in new.items():
        if key not in old:
            out.append(("new", label))
        elif old[key][0] != canon:
            out.append(("changed", label))
    return out


def _ssh_password_allowed(root: Any) -> bool:
    """True if pfSense's sshd is on and accepts a password alone."""
    if root.find("system/ssh/enable") is None:
        return False
    # sshdkeyonly: "enabled" = key only, "both" = key and password; absent = password.
    return _text(root, "system/ssh/sshdkeyonly") not in ("enabled", "both")


# What a count-drop alert watches. A drop of DROP_RATIO or more (and at least
# DROP_MIN items) is unusual for a normal edit and typical for a bad restore,
# a broken package or a hostile change.
_COUNTED = {
    "firewall rules": "filter/rule",
    "port forwards": "nat/rule",
    "users": "system/user",
    "certificates": "cert",
    "CAs": "ca",
}
DROP_RATIO = 0.2
DROP_MIN = 2


def _counts(root: Any) -> dict[str, int]:
    counts = {label: len(root.findall(path)) for label, path in _COUNTED.items()}
    interfaces = root.find("interfaces")
    counts["interfaces"] = 0 if interfaces is None else len(interfaces)
    return counts


def count_drops(old_root: Any, new_root: Any) -> list[RiskyChange]:
    """Sharp drops in rules, users, certificates or interfaces since the previous config."""
    before, after = _counts(old_root), _counts(new_root)
    out = []
    for label, was in before.items():
        now = after[label]
        lost = was - now
        if was and (now == 0 or (lost >= DROP_MIN and lost / was >= DROP_RATIO)):
            out.append(
                RiskyChange(
                    "count-drop",
                    f"{label[0].upper()}{label[1:]} dropped from {was} to {now} (-{lost})",
                )
            )
    return out


def detect_risky_changes(old_xml: str, new_xml: str) -> list[RiskyChange]:
    """Security-relevant differences from the previous config, one per change."""
    old = xml_parser.validate_xml(old_xml)
    new = xml_parser.validate_xml(new_xml)
    changes: list[RiskyChange] = []

    old_admins, new_admins = _admin_users(old), _admin_users(new)
    for name, canon in new_admins.items():
        if name not in old_admins:
            known = name in {_text(u, "name") for u in old.findall("system/user")}
            changes.append(
                RiskyChange(
                    "admin",
                    f"User '{name}' gained admin rights" if known else f"New admin user '{name}'",
                )
            )
        elif old_admins[name] != canon:
            changes.append(RiskyChange("admin", f"Admin user '{name}' was changed"))
    old_groups, new_groups = _admin_groups(old), _admin_groups(new)
    for name, canon in new_groups.items():
        if name not in old_groups:
            changes.append(RiskyChange("admin", f"Group '{name}' has admin rights now"))
        elif old_groups[name] != canon:
            changes.append(RiskyChange("admin", f"Admin group '{name}' was changed"))

    old_auth = _text(old, "system/webgui/authmode") or "Local Database"
    new_auth = _text(new, "system/webgui/authmode") or "Local Database"
    if old_auth != new_auth:
        changes.append(
            RiskyChange("auth-server", f"GUI authentication server: {old_auth} -> {new_auth}")
        )

    for what, label in _new_or_changed(_wan_pass_rules(old), _wan_pass_rules(new)):
        changes.append(RiskyChange("wan-rule", f"WAN pass rule {what}: {label}"))
    for what, label in _new_or_changed(_port_forwards(old), _port_forwards(new)):
        changes.append(RiskyChange("port-forward", f"Port forward {what}: {label}"))

    if _ssh_password_allowed(new) and not _ssh_password_allowed(old):
        changes.append(RiskyChange("ssh-password", "SSH login with a password alone is now on"))

    old_lockout = old.find("system/webgui/noantilockout") is None
    new_lockout = new.find("system/webgui/noantilockout") is None
    if old_lockout != new_lockout:
        state = "on" if new_lockout else "OFF"
        changes.append(RiskyChange("anti-lockout", f"Anti-lockout rule turned {state}"))

    for setting, label in (
        ("protocol", "protocol"),
        ("port", "port"),
        ("ssl-certref", "certificate"),
    ):
        before = _text(old, f"system/webgui/{setting}")
        after = _text(new, f"system/webgui/{setting}")
        if before != after:
            detail = (
                f": {before or 'default'} -> {after or 'default'}"
                if setting != "ssl-certref"
                else ""
            )
            changes.append(RiskyChange("webgui", f"Web GUI {label} changed{detail}"))
    changes.extend(count_drops(old, new))
    return changes


@dataclass
class RestoreCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        counts = ", ".join(f"{n} {k}" for k, n in self.counts.items())
        if self.ok:
            return f"OK: {counts}" + (f" ({'; '.join(self.problems)})" if self.problems else "")
        return "FAILED: " + "; ".join(self.problems)


def restore_check(xml: str) -> RestoreCheck:
    """Would this config.xml restore a working firewall? Checks the key sections.

    Fails when the XML does not parse, or when it has no users, no admin user,
    no interfaces or no filter section. A config without a CA is valid, so a
    missing CA list is a note only.
    """
    try:
        root = xml_parser.validate_xml(xml)
    except xml_parser.PfSenseXMLError as e:
        return RestoreCheck(False, [f"config.xml does not parse: {e}"])

    users = root.findall("system/user")
    interfaces = root.find("interfaces")
    counts = {
        "users": len(users),
        "admins": len(_admin_users(root)),
        "interfaces": 0 if interfaces is None else len(interfaces),
        "rules": len(root.findall("filter/rule")),
        "CAs": len(root.findall("ca")),
        "certificates": len(root.findall("cert")),
    }
    problems = []
    if not users:
        problems.append("no system users")
    elif not counts["admins"]:
        problems.append("no user with admin rights")
    if not counts["interfaces"]:
        problems.append("no interfaces")
    if root.find("filter") is None:
        problems.append("no firewall filter section")
    ok = not problems
    if not counts["CAs"]:
        problems.append("no CA (fine if you use none)")
    return RestoreCheck(ok, problems, counts)
