"""Property-based fuzzing for the pfSense XML parser.

`config.xml` is fetched over the network from a device pfSentinel does not
control, so `validate_xml` is the project's main untrusted-input boundary. The
hand-written cases in `test_xml_parser.py` cover known attack shapes; this file
covers the shapes nobody thought of.

The central invariant every test here asserts:

    For ANY input, validate_xml() either returns a well-formed pfSense root
    element, or raises PfSenseXMLError. Nothing else escapes - no lxml
    exception, no UnicodeError, no RecursionError, no hang.

A leaked exception type matters because callers only catch PfSenseXMLError; a
raw lxml error would propagate as an unhandled crash mid-backup.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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

# Parsing is CPU-bound and fast; a finite deadline turns an accidental
# unbounded expansion or catastrophic backtrack into a test failure rather
# than a hung CI job.
_SETTINGS = settings(
    max_examples=250,
    deadline=2000,  # ms per example
    suppress_health_check=[HealthCheck.too_slow],
)


def _assert_contract(xml: str) -> None:
    """validate_xml must return a valid pfSense root or raise PfSenseXMLError."""
    try:
        root = validate_xml(xml)
    except PfSenseXMLError:
        return  # the only permitted failure mode
    assert root.tag == "pfsense"
    assert root.find("system") is not None


# --- XML-ish document generation ------------------------------------------

_TAG_NAMES = st.sampled_from(
    ["pfsense", "system", "hostname", "domain", "filter", "rule", "interfaces", "x", "a:b"]
)

_ENTITY_REFS = st.sampled_from(
    ["&amp;", "&lt;", "&xxe;", "&lol;", "&#x41;", "&#65;", "&undefined;", "&"]
)

_DOCTYPES = st.sampled_from(
    [
        "",
        "<!DOCTYPE pfsense>",
        '<!DOCTYPE pfsense SYSTEM "http://example.invalid/e.dtd">',
        '<!DOCTYPE pfsense [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
        '<!DOCTYPE pfsense [<!ENTITY lol "lol"><!ENTITY lol1 "&lol;&lol;&lol;">]>',
        '<!DOCTYPE pfsense [<!ENTITY % pe SYSTEM "http://example.invalid/e.dtd">%pe;]>',
    ]
)


@st.composite
def _xml_documents(draw: st.DrawFn) -> str:
    """Generate XML-shaped documents: valid, malformed, and hostile."""
    doctype = draw(_DOCTYPES)
    depth = draw(st.integers(min_value=0, max_value=12))
    tag = draw(_TAG_NAMES)
    body = draw(st.one_of(st.text(max_size=40), _ENTITY_REFS))

    inner = body
    for _ in range(depth):
        inner = f"<{tag}>{inner}</{tag}>"

    root = draw(st.sampled_from(["pfsense", "notpfsense", tag]))
    decl = draw(
        st.sampled_from(['<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-8"?>', ""])
    )
    return f"{decl}{doctype}<{root}><system>{inner}</system></{root}>"


# --- Contract tests --------------------------------------------------------


class TestValidateXmlContract:
    @_SETTINGS
    @given(st.text())
    def test_arbitrary_text_never_leaks_unexpected_exception(self, raw: str) -> None:
        _assert_contract(raw)

    @_SETTINGS
    @given(_xml_documents())
    def test_generated_xml_documents_hold_contract(self, doc: str) -> None:
        _assert_contract(doc)

    @_SETTINGS
    @given(st.binary(max_size=400))
    def test_arbitrary_bytes_decoded_hold_contract(self, blob: bytes) -> None:
        _assert_contract(blob.decode("utf-8", errors="replace"))

    @_SETTINGS
    @given(st.integers(min_value=1, max_value=200))
    def test_deep_nesting_is_bounded(self, depth: int) -> None:
        """Deeply nested input must not blow the stack or hang."""
        payload = "<pfsense><system>" + ("<a>" * depth) + ("</a>" * depth) + "</system></pfsense>"
        _assert_contract(payload)

    @_SETTINGS
    @given(st.text(alphabet="<>&;/\"'= \t\n", max_size=120))
    def test_xml_metacharacter_soup_holds_contract(self, soup: str) -> None:
        """Input made only of XML metacharacters must still fail cleanly."""
        _assert_contract(soup)


class TestDoctypeAlwaysRejected:
    @_SETTINGS
    @given(_xml_documents())
    def test_any_doctype_is_refused(self, doc: str) -> None:
        """No document carrying a DTD may ever parse successfully."""
        if "<!DOCTYPE" not in doc:
            return
        try:
            validate_xml(doc)
        except PfSenseXMLError:
            return
        raise AssertionError(f"DOCTYPE accepted: {doc[:200]!r}")


class TestDownstreamHelpersContract:
    """The helpers all call validate_xml, so they inherit the same contract."""

    @_SETTINGS
    @given(_xml_documents())
    def test_helpers_only_raise_pfsense_error(self, doc: str) -> None:
        for fn in (
            extract_info,
            extract_sections,
            count_rules,
            list_interfaces,
            list_users,
            list_packages,
        ):
            try:
                fn(doc)
            except PfSenseXMLError:
                pass


class TestNoSecretLeakage:
    @_SETTINGS
    @given(st.sampled_from(["/etc/passwd", "/etc/shadow", "C:/Windows/win.ini"]))
    def test_system_entity_never_resolves(self, target: str) -> None:
        """A SYSTEM entity pointing anywhere must never yield file content."""
        payload = (
            '<?xml version="1.0"?>'
            f'<!DOCTYPE pfsense [<!ENTITY xxe SYSTEM "file://{target}">]>'
            "<pfsense><system><hostname>&xxe;</hostname></system></pfsense>"
        )
        try:
            root = validate_xml(payload)
        except PfSenseXMLError:
            return
        # If it ever parses, the entity must not have expanded to anything.
        hostname = root.findtext("system/hostname") or ""
        raise AssertionError(f"DOCTYPE payload parsed; hostname={hostname!r}")
