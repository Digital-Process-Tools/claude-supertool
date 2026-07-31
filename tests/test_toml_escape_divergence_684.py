"""#684 — the two TOML parsers must decide the same way about escapes.

`_load_at_file` parses a payload with stdlib `tomllib` on Python 3.11+ and with
`_mini_toml_loads` below it. `tomllib` treats an unrecognised escape as an
error; the fallback used to swallow the backslash and hand back a string the
caller never wrote. `path = "C:\\Users\\dev\\notes.txt"` became
`C:Usersdevnotes.txt`, and the op then reported "path not found" at an address
the parser had manufactured.

The bug is the *divergence*, so the assertions here are about agreement, not
about either parser alone. That is why the expectations live in one table:

* `test_fallback_matches_the_reference_table` runs on every interpreter in the
  matrix and holds the fallback to the table.
* `test_reference_table_is_a_truthful_transcript_of_tomllib` runs only on 3.11+
  and holds the *table* to tomllib.

Neither leg can drift on its own: the table cannot silently encode the
fallback's habits (3.11+ would fail), and the fallback cannot silently drift
from tomllib (every leg would fail). The CI matrix straddles the boundary
({ubuntu, macos, windows} x 3.9-3.12), so both legs really do run.
"""
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

import supertool


# (name, TOML source, expected) where expected is None for "must raise".
# Every entry was read off `tomllib` — see the 3.11+ test below, which is what
# keeps that claim honest.
CASES: Tuple[Tuple[str, str, Optional[Dict[str, Any]]], ...] = (
    # The reported case: a Windows path typed into a basic string.
    ("windows_path_basic", 'path = "C:\\Users\\dev\\notes.txt"', None),
    # Same path, correctly escaped — must still work.
    ("windows_path_escaped", 'path = "C:\\\\Users\\\\dev"', {"path": "C:\\Users\\dev"}),
    # Same path in a literal string — the documented route, never touched.
    ("windows_path_literal", "path = 'C:\\Users\\dev'", {"path": "C:\\Users\\dev"}),
    # Recognised escapes keep working.
    ("known_escapes", 'a = "x\\ny\\tz\\\\w\\"q"', {"a": "x\ny\tz\\w\"q"}),
    # Unrecognised single-letter escape.
    ("unknown_escape_e", 'a = "\\e"', None),
    # \u / \U are valid TOML the fallback never implemented — it used to mangle
    # them the same silent way. Strictness must not swallow these.
    ("unicode_short", 'a = "\\u00e9"', {"a": "\u00e9"}),
    ("unicode_long", 'a = "\\U0001F600"', {"a": "\U0001F600"}),
    ("unicode_too_short", 'a = "\\u41"', None),
    ("unicode_bad_hex", 'a = "\\uZZZZ"', None),
    # Multi-line basic strings run through the same escape rules.
    ("multiline_bad_escape", 'a = """C:\\Users"""', None),
    ("multiline_known_escape", 'a = """x\\ty"""', {"a": "x\ty"}),
    # A backslash at end of line inside """ is TOML's line continuation: it
    # eats the newline and the leading whitespace of the next line.
    ("multiline_continuation", 'a = """x\\\n   y"""', {"a": "xy"}),
    # ...but only inside """. In a single-line basic string it is an error.
    ("singleline_trailing_backslash", 'a = "x\\"', None),
)

CASE_IDS = tuple(name for name, _, _ in CASES)


def _outcome(parser: Any, source: str) -> Tuple[str, Any]:
    """Run *parser* and flatten it to ('ok', value) or ('error', None)."""
    try:
        return "ok", parser(source)
    except Exception:
        return "error", None


@pytest.mark.parametrize("name,source,expected", CASES, ids=CASE_IDS)
def test_fallback_matches_the_reference_table(
    name: str, source: str, expected: Optional[Dict[str, Any]]
) -> None:
    """The <3.11 parser, on every interpreter in the matrix."""
    status, value = _outcome(supertool._mini_toml_loads, source)
    if expected is None:
        assert status == "error", (
            f"{name}: fallback accepted {source!r} and produced {value!r}; "
            "tomllib rejects it"
        )
    else:
        assert status == "ok", f"{name}: fallback rejected {source!r}"
        assert value == expected, f"{name}: fallback produced {value!r}"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is 3.11+")
@pytest.mark.parametrize("name,source,expected", CASES, ids=CASE_IDS)
def test_reference_table_is_a_truthful_transcript_of_tomllib(
    name: str, source: str, expected: Optional[Dict[str, Any]]
) -> None:
    """The table is only worth anything if tomllib really says this."""
    import tomllib

    status, value = _outcome(tomllib.loads, source)
    if expected is None:
        assert status == "error", f"{name}: tomllib accepted {source!r} -> {value!r}"
    else:
        assert status == "ok", f"{name}: tomllib rejected {source!r}"
        assert value == expected, f"{name}: tomllib produced {value!r}"


@pytest.mark.parametrize("name,source,expected", CASES, ids=CASE_IDS)
def test_payload_route_agrees_with_the_table(
    tmp_path: Path, name: str, source: str, expected: Optional[Dict[str, Any]]
) -> None:
    """Whichever parser `_load_at_file` picked, the caller sees one answer.

    This is the seam the user actually touches, and the only place where the
    version split is invisible to them.
    """
    payload = tmp_path / f"{name}.toml"
    payload.write_text(source + "\n", encoding="utf-8")
    status, value = _outcome(supertool._load_at_file, "@" + str(payload))
    if expected is None:
        assert status == "error", (
            f"{name}: payload route accepted it and returned {value!r}"
        )
    else:
        assert (status, value) == ("ok", expected)


@pytest.fixture
def no_tomllib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import tomllib` fail, so `_load_at_file` takes the <3.11 route.

    A 3.11+ runner otherwise never reaches the fallback through the real seam,
    and the leg that carried the bug would go untested on the machine most
    likely to run these tests.
    """
    monkeypatch.setitem(sys.modules, "tomllib", None)


@pytest.mark.usefixtures("no_tomllib")
@pytest.mark.parametrize("name,source,expected", CASES, ids=CASE_IDS)
def test_payload_route_agrees_with_the_table_without_tomllib(
    tmp_path: Path, name: str, source: str, expected: Optional[Dict[str, Any]]
) -> None:
    payload = tmp_path / f"{name}.toml"
    payload.write_text(source + "\n", encoding="utf-8")
    status, value = _outcome(supertool._load_at_file, "@" + str(payload))
    if expected is None:
        assert status == "error", (
            f"{name}: fallback route accepted it and returned {value!r}"
        )
    else:
        assert (status, value) == ("ok", expected)


@pytest.mark.usefixtures("no_tomllib")
def test_a_rejected_escape_names_the_backslash_on_the_fallback(
    tmp_path: Path,
) -> None:
    """The reported failure, end to end, on the parser that produced it."""
    payload = tmp_path / "p.toml"
    payload.write_text('path = "C:\\Users\\dev\\notes.txt"\n', encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file("@" + str(payload))
    message = str(excinfo.value)
    assert "TOML parse error" in message
    assert "invalid escape" in message
    assert "not found" not in message


def test_a_rejected_escape_names_the_backslash(tmp_path: Path) -> None:
    """The old failure was silent. Whatever replaces it must not be.

    The reader arrives holding a path they typed and a tool saying it is not
    there; the error has to point at the escape, not at the filesystem.
    """
    payload = tmp_path / "p.toml"
    payload.write_text('path = "C:\\Users\\dev\\notes.txt"\n', encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file("@" + str(payload))
    message = str(excinfo.value).lower()
    assert "toml parse error" in message
    assert "\\" in message or "escape" in message
