"""`validators/SCHEMA.md` and the core's strip set are one contract written twice (#1042).

`_VALIDATOR_CORE_ONLY_KEYS` in `_supertool.py` is the code's half: the keys the
core stamps on a result and drops from every adapter payload before a decision
reads it. `validators/SCHEMA.md` is the prose half. Nothing compared them, so
they could disagree for months — and the first symptom would be somebody
reasoning from the document about code that does something else, which is
literally how #1036 happened: SCHEMA.md forbade `no_verdict` to adapters, that
sentence was read as covering `timeout`, and it did not.

Two directions, and they fail differently:

* **code-owns-but-doc-omits** — a key enters the strip set and the table never
  names it. An adapter author reads the doc, emits the field, and it silently
  vanishes. This is #1239's direction one layer over.
* **doc-declares-but-core-strips** — the table lists a field as adapter-writable
  and the core drops it before anything reads it. That one reads as a *product
  bug* to whoever tries it: the documented field simply does not work, with no
  error anywhere (#1269 argues this is the more interesting direction, and it
  had never been checked here).

Both are cheap once each side is parsed, so both run.

**The parse is the part that can lie.** A reader that stops at the first blank
line inside `## Fields` finds seven adapter fields and misses `diff` and
`skipped` — and every assertion below then passes, on an incomplete read. That
is this repo's standing defect (an absence produced by the tool, read as an
absence in the world) appearing inside the guard written to prevent an instance
of it, exactly as #1239's substring-vs-AST discovery nearly did. So the field
reader spans blank-line-separated table fragments, and a separate assertion
pins the section to a single table so the split cannot come back.

**A side that cannot be parsed is `skipped`, not `ok`** (`docs/validators.md`
§"Declining instead of guessing", line 716). In a pytest guard the third state
is a **loud failure naming which side went unread** — never `pytest.skip`,
which is the one spelling of "I did not look" that CI renders as green.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import supertool

ROOT = Path(__file__).parent.parent
SCHEMA = ROOT / "validators" / "SCHEMA.md"

#: A field row: the first cell is a backticked name.
_ROW = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|")
_HEADING = re.compile(r"^#{1,6}\s")
#: A table's delimiter row: `|---|---|`.
_DELIM = re.compile(r"^\|[\s:|-]+\|\s*$")

ADAPTER_HEADING = "## Fields"
CORE_ONLY_HEADING = "### Core-only fields"


class SchemaUnreadable(AssertionError):
    """The guard could not read a side. Never silently a pass."""


def _lines() -> list:
    try:
        return SCHEMA.read_text(encoding="utf-8").split(chr(10))
    except OSError as exc:  # pragma: no cover - the file is in-tree
        raise SchemaUnreadable(
            f"{SCHEMA} could not be read ({exc}) — this guard has no opinion "
            f"about the contract and must not report one") from None


def _section(heading: str) -> list:
    """The lines under `heading`, up to the next heading of any level.

    Bounded by *any* heading rather than by the next `##`: `## Fields` is
    followed by `### Error object`, whose table describes the fields of one
    error object rather than of the payload. Running to the next `##` would
    fold those six names into the adapter surface.
    """
    lines = _lines()
    try:
        start = lines.index(heading)
    except ValueError:
        raise SchemaUnreadable(
            f"{SCHEMA.name} has no {heading!r} section, so this guard cannot "
            f"read that side of the contract. It says so rather than passing — "
            f"an unread side is `skipped`, not `ok` (docs/validators.md, "
            f"'Declining instead of guessing')."
        ) from None
    out = []
    for line in lines[start + 1:]:
        if _HEADING.match(line):
            break
        out.append(line)
    return out


def _fields(heading: str) -> frozenset:
    """Every backticked first-cell name in `heading`'s section.

    Deliberately blind to blank lines between table fragments: under-reading
    the table is the failure mode that makes every assertion below pass on
    half a contract.
    """
    names = set()
    for line in _section(heading):
        match = _ROW.match(line)
        if match:
            names.add(match.group(1))
    if not names:
        raise SchemaUnreadable(
            f"{heading!r} in {SCHEMA.name} parsed to zero fields — the reader "
            f"found nothing, which is not the same fact as a contract with no "
            f"fields, and must not be reported as one")
    return frozenset(names)


def _core_only() -> frozenset:
    return frozenset(getattr(supertool, "_VALIDATOR_CORE_ONLY_KEYS", ()))


# ---------------------------------------------------------------------------
# The reader has to be honest before its verdicts mean anything
# ---------------------------------------------------------------------------

def test_the_sweep_actually_read_both_sides() -> None:
    """A guard whose discovery went quiet reads exactly like a clean contract."""
    adapter, core = _fields(ADAPTER_HEADING), _fields(CORE_ONLY_HEADING)
    assert len(adapter) >= 8, sorted(adapter)
    assert len(core) >= 4, sorted(core)
    for expected in ("tool", "file", "ok", "count", "errors", "skipped"):
        assert expected in adapter, sorted(adapter)


def test_a_table_split_by_a_blank_line_is_still_read_whole(
        tmp_path, monkeypatch) -> None:
    """The under-read that would have made every verdict below vacuous.

    `SCHEMA.md` really did carry this: a blank line after the `metrics` row
    left `diff` and `skipped` outside the table, rendering as a paragraph of
    pipes, and a reader that stopped at the blank line dropped `skipped` — the
    third state, the most-cited field in the whole contract — from the adapter
    surface without failing anything.
    """
    doc = tmp_path / "SCHEMA.md"
    doc.write_text(chr(10).join([
        "## Fields", "",
        "| Field | Type |", "|-------|------|",
        "| `tool` | string |", "",
        "| `skipped` | string |", "",
        "### Error object", "", "| `line` | int |",
    ]), encoding="utf-8")
    monkeypatch.setitem(globals(), "SCHEMA", doc)
    assert _fields(ADAPTER_HEADING) == frozenset({"tool", "skipped"})


def test_an_unreadable_side_is_reported_not_passed(
        tmp_path, monkeypatch) -> None:
    """`skipped`, not `ok` — and in a test harness that means a red, because
    `pytest.skip` is the one way of saying "I did not look" that CI prints
    green."""
    doc = tmp_path / "SCHEMA.md"
    doc.write_text("# Validator Output Schema" + chr(10), encoding="utf-8")
    monkeypatch.setitem(globals(), "SCHEMA", doc)
    with pytest.raises(SchemaUnreadable):
        _fields(ADAPTER_HEADING)
    with pytest.raises(SchemaUnreadable):
        _fields(CORE_ONLY_HEADING)


def _orphaned_rows(heading: str) -> list:
    """Field rows the renderer will not see as table rows.

    A table block runs from its delimiter row until the first blank line. Rows
    after that blank are a *paragraph* of literal pipe characters, whatever
    they look like in the source.

    Counting delimiter rows cannot find this and would report clean: the
    orphaned fragment has no delimiter of its own, so the count stays 1. That
    vacuous version of this assertion passed on the very file carrying the
    defect — the guard reporting `ok` because it looked for the wrong absence.
    """
    orphaned, in_table = [], False
    for line in _section(heading):
        if _DELIM.match(line):
            in_table = True
        elif not line.strip():
            in_table = False
        elif _ROW.match(line) and not in_table:
            orphaned.append(line.split("|")[1].strip())
    return orphaned


def test_no_field_row_falls_outside_its_table() -> None:
    """A blank line mid-table ends it, and the rows after it render as a
    paragraph of literal pipes. `SCHEMA.md` shipped that way in d822e93 (#411)
    and it was only found while building this guard: `diff` and `skipped` — the
    third state — were outside the rendered field table for two weeks."""
    for heading in (ADAPTER_HEADING, CORE_ONLY_HEADING):
        orphaned = _orphaned_rows(heading)
        assert not orphaned, (
            f"{heading!r} declares {orphaned} after a blank line, so the table "
            f"has already ended and they render as a paragraph of pipe "
            f"characters rather than as fields.")


def test_the_orphan_check_sees_a_split_table(tmp_path, monkeypatch) -> None:
    """The check above must fail on the shape it exists to catch — the version
    that counted delimiter rows did not, and passed on the real defect."""
    doc = tmp_path / "SCHEMA.md"
    doc.write_text(chr(10).join([
        "## Fields", "",
        "| Field | Type |", "|-------|------|",
        "| `tool` | string |", "",
        "| `skipped` | string |", "",
    ]), encoding="utf-8")
    monkeypatch.setitem(globals(), "SCHEMA", doc)
    assert _orphaned_rows(ADAPTER_HEADING) == ["`skipped`"]


# ---------------------------------------------------------------------------
# Direction 1 — code owns a key and the doc does not name it (#1239's direction)
# ---------------------------------------------------------------------------

def test_every_key_the_core_strips_is_declared_core_only_in_the_doc() -> None:
    undeclared = sorted(_core_only() - _fields(CORE_ONLY_HEADING))
    assert not undeclared, (
        f"_VALIDATOR_CORE_ONLY_KEYS holds {undeclared}, which "
        f"{SCHEMA.name}'s {CORE_ONLY_HEADING!r} table does not name. An "
        f"adapter author reads the doc, emits the field, and the core drops it "
        f"with no error anywhere (#1042).")


# ---------------------------------------------------------------------------
# Direction 2 — the doc names a field the core does not treat that way (#1269)
# ---------------------------------------------------------------------------

def test_no_field_the_doc_declares_adapter_writable_is_stripped() -> None:
    """The direction that reads as a product bug rather than as stale prose.

    A field in the adapter table that the core strips is documented, emitted,
    and then silently gone — the reporter has no error to look at and every
    test is green.
    """
    swallowed = sorted(_fields(ADAPTER_HEADING) & _core_only())
    assert not swallowed, (
        f"{SCHEMA.name} documents {swallowed} as adapter-writable and the core "
        f"strips it from every payload before any decision reads it. A "
        f"documented field that does nothing and reports nothing (#1269).")


def test_the_docs_core_only_table_matches_the_core_exactly() -> None:
    """The inverse of direction 1: the doc naming a key the core does *not*
    strip. That tells an adapter author a field is unavailable when it is in
    fact honoured, and tells a core reader the boundary is wider than it is.
    """
    declared, stripped = _fields(CORE_ONLY_HEADING), _core_only()
    assert declared == stripped, (
        f"doc declares {sorted(declared)}, core strips {sorted(stripped)}")


def test_the_two_tables_do_not_overlap() -> None:
    overlap = sorted(_fields(ADAPTER_HEADING) & _fields(CORE_ONLY_HEADING))
    assert not overlap, overlap


# ---------------------------------------------------------------------------
# The third copy — a test asserting against its own transcription of the doc
# ---------------------------------------------------------------------------

def test_the_1036_tests_transcription_of_the_table_still_matches_it() -> None:
    """`test_adapter_cannot_forge_core_keys_1036.py:53` hardcodes the adapter
    field list, and `test_every_core_only_key_read_by_a_decision_is_stripped`
    uses it as the *exemption list* for its class check. So a field added to
    SCHEMA.md and not to that frozenset does not merely go stale — it narrows
    a live guard, which then reports a key as unowned that the doc allows.

    Checked here rather than repaired there: the count of what has drifted is
    the argument for this guard existing, and that file belongs to #1036.
    """
    from test_adapter_cannot_forge_core_keys_1036 import SCHEMA_ADAPTER_KEYS
    assert frozenset(SCHEMA_ADAPTER_KEYS) == _fields(ADAPTER_HEADING), (
        f"the 1036 test transcribes {sorted(SCHEMA_ADAPTER_KEYS)} from "
        f"{SCHEMA.name}, which now declares {sorted(_fields(ADAPTER_HEADING))}")


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(1042)
