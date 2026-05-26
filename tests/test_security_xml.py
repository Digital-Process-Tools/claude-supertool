"""XML-parser security tests for the xml/xml_attr/xml_count preset ops.

Each test probes a distinct attack surface. Severities in docstrings.
Run: python3 -m pytest tests/test_security_xml.py -v --no-cov
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "xml"


def _load(name: str):
    for mod in list(sys.modules.keys()):
        if mod in ("_common",) or mod.startswith("xml_"):
            sys.modules.pop(mod, None)
    if str(PRESET_DIR) not in sys.path:
        sys.path.insert(0, str(PRESET_DIR))
    spec = importlib.util.spec_from_file_location(f"xml_{name}", PRESET_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


query_mod = _load("query")
attr_mod = _load("attr")
count_mod = _load("count")


def _write_xml(tmp_path: Path, content: str, name: str = "test.xml") -> str:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    # Use forward-slash paths so the colon in Windows drive letters (C:\...)
    # doesn't confuse split_arg's colon-based field separator.
    return f.as_posix()


def _write_xml_bytes(tmp_path: Path, content: bytes, name: str = "test.xml") -> str:
    f = tmp_path / name
    f.write_bytes(content)
    # Use forward-slash paths for the same reason as _write_xml.
    return f.as_posix()


# ---------------------------------------------------------------------------
# 1. External Entity Expansion (XXE) — SEVERITY: HIGH
#    If /etc/passwd content appears in output, the parser resolved a
#    SYSTEM entity and leaked host filesystem data.
# ---------------------------------------------------------------------------

XXE_PAYLOAD = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<foo>&xxe;</foo>
"""

XXE_PAYLOAD_ATTR = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<root>
  <item id="&xxe;">secret</item>
</root>
"""


def test_xxe_file_entity_not_resolved_query(tmp_path: Path, capsys) -> None:
    """SEVERITY: HIGH — xml: op must NOT include /etc/passwd content.

    Python's expat does NOT expand SYSTEM entities by default — this pins
    that behavior and will fail loudly if it ever regresses.
    """
    path = _write_xml(tmp_path, XXE_PAYLOAD)
    with pytest.raises(SystemExit):
        query_mod.main(f"{path}:.//foo")
    out, err = capsys.readouterr()
    combined = out + err
    # If expat resolved the entity, passwd content would appear here.
    # Common passwd markers — if any appear, XXE succeeded → FAIL.
    assert "root:" not in combined, "XXE RESOLVED: /etc/passwd content leaked via xml: op"
    assert "/bin/" not in combined, "XXE RESOLVED: /etc/passwd content leaked via xml: op"
    assert "nobody" not in combined, "XXE RESOLVED: /etc/passwd content leaked via xml: op"


def test_xxe_file_entity_not_resolved_count(tmp_path: Path, capsys) -> None:
    """SEVERITY: HIGH — xml_count: op must NOT include /etc/passwd content."""
    path = _write_xml(tmp_path, XXE_PAYLOAD)
    with pytest.raises(SystemExit):
        count_mod.main(f"{path}:.//foo")
    out, err = capsys.readouterr()
    combined = out + err
    assert "root:" not in combined, "XXE RESOLVED via xml_count: op"
    assert "/bin/" not in combined, "XXE RESOLVED via xml_count: op"


def test_xxe_file_entity_not_resolved_attr(tmp_path: Path, capsys) -> None:
    """SEVERITY: HIGH — xml_attr: op must NOT include /etc/passwd content."""
    path = _write_xml(tmp_path, XXE_PAYLOAD_ATTR)
    with pytest.raises(SystemExit):
        attr_mod.main(f"{path}:.//item:id")
    out, err = capsys.readouterr()
    combined = out + err
    assert "root:" not in combined, "XXE RESOLVED via xml_attr: op"
    assert "/bin/" not in combined, "XXE RESOLVED via xml_attr: op"


def test_xxe_graceful_error_not_silent(tmp_path: Path, capsys) -> None:
    """SEVERITY: MEDIUM — XXE payload must exit non-zero or error, not silently succeed.

    The parser may refuse to expand but still parse successfully (producing
    empty entity text). Either an error exit OR an empty/clean result is
    acceptable — what's NOT acceptable is resolving the entity and returning data.
    """
    path = _write_xml(tmp_path, XXE_PAYLOAD)
    try:
        query_mod.main(f"{path}:.//foo")
        out, err = capsys.readouterr()
        # If it didn't exit: the element must be empty or show no passwd data
        assert "root:" not in (out + err)
        assert "/bin/" not in (out + err)
    except SystemExit as exc:
        # A non-zero exit is fine (parse error on entity expansion attempt)
        out, err = capsys.readouterr()
        assert "root:" not in (out + err), "XXE data leaked before exit"
        assert "/bin/" not in (out + err), "XXE data leaked before exit"


# ---------------------------------------------------------------------------
# 2. Billion-Laughs DoS — SEVERITY: HIGH
#    Exponential entity expansion should not OOM or hang. Python expat
#    by default does NOT expand internal entities recursively, so this
#    should either parse quickly (ignoring entities) or raise an error.
#    We enforce a 5-second wall-clock limit.
# ---------------------------------------------------------------------------

BILLION_LAUGHS = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<lolz>&lol9;</lolz>
"""

TIMEOUT_SECONDS = 5


def _run_with_timeout(fn, *args, timeout=TIMEOUT_SECONDS):
    """Run fn(*args) in a thread; raise AssertionError if it exceeds timeout."""
    exc_box: list[BaseException] = []
    result_box: list = []

    def _target():
        try:
            result_box.append(fn(*args))
        except BaseException as e:  # noqa: BLE001
            exc_box.append(e)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(
            f"billion-laughs DoS: fn did not complete within {timeout}s — likely OOM/hang"
        )
    if exc_box:
        raise exc_box[0]


def test_billion_laughs_does_not_hang(tmp_path: Path, capsys) -> None:
    """SEVERITY: HIGH — billion-laughs must not hang or OOM. Must complete in <5s."""
    path = _write_xml(tmp_path, BILLION_LAUGHS)

    def _run():
        try:
            query_mod.main(f"{path}:.//lolz")
        except SystemExit:
            pass

    _run_with_timeout(_run, timeout=TIMEOUT_SECONDS)
    # If we reach here, the parser completed without hanging — pass
    out, err = capsys.readouterr()
    # Entity content must not have been fully expanded (no billion "lol"s)
    combined = out + err
    # A fully expanded string would be ~10^9 chars — even 1000 repetitions
    # of "lol" would be a sign of partial expansion. Pin at 500 chars of "lol".
    lol_count = combined.count("lol")
    assert lol_count < 500, (
        f"billion-laughs partially expanded: found {lol_count} 'lol' occurrences in output"
    )


# ---------------------------------------------------------------------------
# 3. External DTD Load — SEVERITY: HIGH
#    The parser must not attempt a network fetch for an external DTD.
#    We pin this by using a non-routable address — if it hangs, the test
#    times out and fails. If it errors immediately (no network), that's fine.
# ---------------------------------------------------------------------------

EXTERNAL_DTD = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo SYSTEM "http://192.0.2.1/evil.dtd">
<foo>
  <bar>hello</bar>
</foo>
"""


def test_external_dtd_does_not_fetch(tmp_path: Path, capsys) -> None:
    """SEVERITY: HIGH — must not make a network request for external DTD.

    Uses 192.0.2.1 (TEST-NET-1, RFC 5737) — guaranteed non-routable,
    so a connection attempt would hang rather than fail fast.
    A 3s timeout catches a blocking fetch attempt.
    """
    path = _write_xml(tmp_path, EXTERNAL_DTD)

    def _run():
        try:
            query_mod.main(f"{path}:.//bar")
        except SystemExit:
            pass

    _run_with_timeout(_run, timeout=3)
    # If we reach here: no hang → no network fetch attempted (or error was immediate)


# ---------------------------------------------------------------------------
# 4. Quadratic Blowup (Parameter Entities) — SEVERITY: MEDIUM
#    Parameter entity expansion can also cause quadratic/exponential blowup.
#    Must complete in bounded time.
# ---------------------------------------------------------------------------

QUADRATIC_BLOWUP = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [
  <!ENTITY % a "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">
  <!ENTITY % b "%a;%a;%a;%a;%a;%a;%a;%a;%a;%a;%a;%a;%a;%a;%a;%a;">
  <!ENTITY % c "%b;%b;%b;%b;%b;%b;%b;%b;%b;%b;%b;%b;%b;%b;%b;%b;">
  <!ENTITY % d "%c;%c;%c;%c;%c;%c;%c;%c;%c;%c;%c;%c;%c;%c;%c;%c;">
]>
<foo>harmless</foo>
"""


def test_parameter_entity_blowup_bounded(tmp_path: Path, capsys) -> None:
    """SEVERITY: MEDIUM — parameter entity expansion must complete in <5s."""
    path = _write_xml(tmp_path, QUADRATIC_BLOWUP)

    def _run():
        try:
            query_mod.main(f"{path}:.//foo")
        except SystemExit:
            pass

    _run_with_timeout(_run, timeout=TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# 5. Path Traversal — SEVERITY: MEDIUM
#    xml:../../../etc/passwd:.//foo — must not read arbitrary host paths.
#    Current behavior (documented): no chroot, so traversal currently works.
#    This test PINS the behavior — will fail if XXE via path traversal lands
#    passwd content in output.
# ---------------------------------------------------------------------------

def test_path_traversal_does_not_leak_passwd(tmp_path: Path, capsys, monkeypatch) -> None:
    """SEVERITY: MEDIUM — path traversal must not silently leak sensitive files.

    Note: the parser currently has NO chroot restriction (same as op_read).
    This test documents that behavior and catches any regression where
    traversal + XML parse could leak /etc/passwd-style content.
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    # Try to read a file we know is likely XML-parseable (or not)
    # The actual /etc/passwd is not valid XML, so it will be a parse error.
    # What we care about: no passwd content in output.
    try:
        query_mod.main("../../../etc/passwd:.//root")
    except SystemExit:
        pass

    out, err = capsys.readouterr()
    combined = out + err
    # If expat somehow parsed passwd and found a "root" element, that would be bizarre,
    # but any passwd line content appearing = a problem.
    assert "root:" not in combined
    assert "/bin/bash" not in combined
    assert "/bin/sh" not in combined


# ---------------------------------------------------------------------------
# 6. NUL Byte in Path — SEVERITY: LOW
#    A NUL byte in the path argument must not crash the process.
# ---------------------------------------------------------------------------

def test_nul_byte_in_path_clean_error(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — NUL in path must produce a clean ERROR, not a crash/traceback."""
    bad_path = f"{tmp_path}/foo\x00.xml"
    with pytest.raises(SystemExit) as exc:
        query_mod.main(f"{bad_path}:.//item")
    assert exc.value.code != 0
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err), "NUL byte caused a Python traceback"


def test_nul_byte_in_path_xml_count(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — xml_count: NUL in path must not crash."""
    bad_path = f"{tmp_path}/foo\x00.xml"
    with pytest.raises(SystemExit) as exc:
        count_mod.main(f"{bad_path}:.//item")
    assert exc.value.code != 0
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


# ---------------------------------------------------------------------------
# 7. Symlink to External File — SEVERITY: LOW (behavior pinning)
#    A symlink pointing outside cwd — document current behavior.
#    If the target is valid XML, the op reads through the symlink.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows symlink creation requires admin privileges or Developer Mode",
)
def test_symlink_to_valid_xml_reads_through(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — symlink to valid XML: current behavior is read-through (pin it)."""
    real = tmp_path / "real.xml"
    real.write_text('<?xml version="1.0"?><root><item id="42">data</item></root>\n')
    link = tmp_path / "link.xml"
    link.symlink_to(real)

    query_mod.main(f"{link!s}:.//item")
    out, err = capsys.readouterr()
    # Current behavior: reads through symlink — pin this.
    assert "item" in out, "symlink read-through behavior changed (was: works)"


def test_symlink_to_nonxml_file_clean_error(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — symlink to a non-XML file must give a clean parse error, not crash."""
    real = tmp_path / "binary.bin"
    real.write_bytes(b"\x00\x01\x02\x03notxml")
    link = tmp_path / "link.xml"
    link.symlink_to(real)

    with pytest.raises(SystemExit) as exc:
        query_mod.main(f"{link!s}:.//item")
    assert exc.value.code != 0
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


# ---------------------------------------------------------------------------
# 8. Malformed XML — SEVERITY: LOW
#    Truncated, mixed encoding, and BOM-prefixed input must all produce
#    clean errors, not Python tracebacks.
# ---------------------------------------------------------------------------

def test_malformed_truncated_clean_error(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — truncated XML must exit non-zero with clean error."""
    path = _write_xml(tmp_path, "<root><unclosed>")
    with pytest.raises(SystemExit) as exc:
        query_mod.main(f"{path}:.//unclosed")
    assert exc.value.code != 0
    _, err = capsys.readouterr()
    assert "Traceback" not in err
    assert "ERROR" in err or "malformed" in err.lower()


def test_malformed_bom_utf16_clean_error(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — UTF-16 BOM on a non-UTF-16 body must not crash."""
    # UTF-16 BOM followed by ASCII — confusing encoding declaration
    path = _write_xml_bytes(
        tmp_path,
        b"\xff\xfe<?xml version='1.0'?><root/>",
        name="bom16.xml",
    )
    # May parse or may error — either is fine, no crash/traceback allowed
    try:
        query_mod.main(f"{path}:.//root")
    except SystemExit:
        pass
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


def test_malformed_encoding_mismatch_clean_error(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — encoding declaration mismatch must not crash."""
    # Claims UTF-8 but contains raw latin-1 bytes
    path = _write_xml_bytes(
        tmp_path,
        b"<?xml version='1.0' encoding='UTF-8'?><root><item>\xe9\xe0\xf9</item></root>",
        name="enc_mismatch.xml",
    )
    try:
        query_mod.main(f"{path}:.//item")
    except SystemExit:
        pass
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


def test_malformed_empty_file_clean_error(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — empty file must exit non-zero cleanly."""
    path = _write_xml_bytes(tmp_path, b"", name="empty.xml")
    with pytest.raises(SystemExit) as exc:
        query_mod.main(f"{path}:.//item")
    assert exc.value.code != 0
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


def test_malformed_only_declaration_clean_error(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — XML declaration with no root element must not crash."""
    path = _write_xml(tmp_path, "<?xml version='1.0' encoding='UTF-8'?>")
    with pytest.raises(SystemExit) as exc:
        query_mod.main(f"{path}:.//item")
    assert exc.value.code != 0
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


# ---------------------------------------------------------------------------
# 9. Namespace Injection via XPath — SEVERITY: LOW
#    Namespaces in XPath expressions with smuggled declarations should not
#    cause undefined behavior or silent data access.
# ---------------------------------------------------------------------------

NS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root xmlns:ns="http://example.com/ns">
  <ns:item id="1">ns-content</ns:item>
  <item id="2">plain-content</item>
</root>
"""


def test_namespace_xpath_does_not_crash(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — XPath with namespace prefix must not crash (may error cleanly)."""
    path = _write_xml(tmp_path, NS_XML)
    # ET's XPath subset doesn't support arbitrary namespace prefixes;
    # this should either error cleanly or return 0 matches.
    try:
        query_mod.main(f"{path}:.//ns:item")
    except SystemExit as exc:
        out, err = capsys.readouterr()
        assert "Traceback" not in (out + err)
        return
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


def test_namespace_injection_in_xpath_attribute(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — smuggled namespace-like string in XPath predicate must not crash."""
    path = _write_xml(tmp_path, NS_XML)
    # Attempt to inject a namespace declaration via XPath string
    injected_xpath = ".//item[@xmlns:evil='http://evil.com']"
    try:
        query_mod.main(f"{path}:{injected_xpath}")
    except SystemExit:
        pass
    out, err = capsys.readouterr()
    assert "Traceback" not in (out + err)


# ---------------------------------------------------------------------------
# 10. XPath Complexity / ReDoS-equivalent — SEVERITY: LOW
#     Deeply nested descendant-or-self axis steps can cause quadratic
#     evaluation in naive ET implementations. Must complete in <5s.
# ---------------------------------------------------------------------------

def _make_deep_xml(tmp_path: Path, depth: int = 50) -> str:
    """Build a deeply nested XML document."""
    inner = "<leaf>x</leaf>"
    for i in range(depth):
        inner = f"<n{i}>{inner}</n{i}>"
    content = f'<?xml version="1.0"?><root>{inner}</root>'
    return _write_xml(tmp_path, content, name="deep.xml")


def test_xpath_deep_descendant_bounded(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — deep //*//*//*//* XPath must complete in <5s."""
    path = _make_deep_xml(tmp_path, depth=50)

    def _run():
        try:
            query_mod.main(f"{path}://*")
        except SystemExit:
            pass

    _run_with_timeout(_run, timeout=TIMEOUT_SECONDS)


def test_xpath_wildcard_descendant_stress(tmp_path: Path, capsys) -> None:
    """SEVERITY: LOW — broad wildcard //*//*//*//* on a wide+deep tree must be bounded."""
    # Build a wide tree: 5 levels, 4 children each = 4^5 = 1024 leaves
    def _make_wide(depth, branching=4):
        if depth == 0:
            return "<leaf/>"
        children = "".join(f"<child>{_make_wide(depth-1, branching)}</child>" for _ in range(branching))
        return children

    content = f'<?xml version="1.0"?><root>{_make_wide(5)}</root>'
    path = _write_xml(tmp_path, content, name="wide.xml")

    def _run():
        try:
            # This is the expensive XPath pattern
            query_mod.main(f"{path}:.//*//*")
        except SystemExit:
            pass

    _run_with_timeout(_run, timeout=TIMEOUT_SECONDS)
