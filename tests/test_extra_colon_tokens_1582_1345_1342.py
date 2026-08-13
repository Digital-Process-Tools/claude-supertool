"""#1582 / #1345 / #1342 — one seam: a colon token the dispatcher peels and
never reads.

* #1582 — `read:PATH:::lines=66-76` returned the whole file. The unknown key was
  dropped, and an ignored *narrowing* returns MORE than was asked for, which
  reads as a superset of correct. 13549 bytes where ~1500 were asked for.
* #1345 — `grep:PAT:PATH:5:3:2` peels three trailing integers and reads two, so
  the third runs a call nobody typed. The reasoning was already written down
  beside `_GREP_ALL_OUTSIDE_LIMIT_SLOT` (#1328); only `all` was closed there.
* #1342 — a ranged read whose end lands on the last line declines to say which
  of EOF and the limit closed the window, while printing the file's total line
  count one line above. A decline emitted where the answer is available is the
  three-state contract used as a shrug.

The first two are refusals: an argument that was not read is a question that was
not answered. The third is a *disclosure* on a call that succeeded, so it stays a
render change — turning it into a refusal would break working calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import supertool


def _lines(tmp_path: Path, name: str, count: int) -> Path:
    f = tmp_path / name
    f.write_text("".join(f"line{i}" + chr(10) for i in range(1, count + 1)))
    return f


# ---------------------------------------------------------------------------
# #1582 — read
# ---------------------------------------------------------------------------

def test_unknown_read_key_is_refused_not_dropped(tmp_path: Path) -> None:
    """The filed call. `lines=` is not a field; the file came back entire."""
    f = _lines(tmp_path, "doc.md", 40)
    out = supertool.dispatch(f"read:{f}:::lines=6-9")
    assert "ERROR:" in out, (
        "an unknown key that narrows nothing returns the whole file, which is "
        "indistinguishable from a call that did what was typed: " + repr(out))
    assert "line20" not in out, (
        "refusing means returning no content, not content plus a note: "
        + repr(out))


def test_the_refusal_names_the_token_and_the_range_form(tmp_path: Path) -> None:
    """`lines=` is the spelling a reader reaches for after seeing START-END."""
    f = _lines(tmp_path, "doc.md", 40)
    out = supertool.dispatch(f"read:{f}:::lines=6-9")
    assert "lines=6-9" in out, "the refusal must quote what was ignored: " + repr(out)
    assert f"read:{f}:6-9" in out, (
        "the remedy is the syntax form, spelled out for this call: " + repr(out))


def test_a_bare_unknown_key_is_refused(tmp_path: Path) -> None:
    f = _lines(tmp_path, "doc.md", 40)
    out = supertool.dispatch(f"read:{f}:::bogus=1")
    assert "ERROR:" in out and "bogus=1" in out, repr(out)


def test_a_trailing_token_past_offset_and_limit_is_refused(tmp_path: Path) -> None:
    """`read:PATH:1:2:full` ran offset 1 limit 2 and dropped `full` (#1582)."""
    f = _lines(tmp_path, "doc.md", 40)
    out = supertool.dispatch(f"read:{f}:1:2:full")
    assert "ERROR:" in out, repr(out)
    assert "full" in out, repr(out)


def test_the_documented_grep_key_still_narrows(tmp_path: Path) -> None:
    """The one colon key `read` does route. A refusal that took this with it
    would have closed the op's most-used narrowing."""
    f = tmp_path / "doc.md"
    f.write_text("alpha" + chr(10) + "beta" + chr(10) + "gamma" + chr(10))
    out = supertool.dispatch(f"read:{f}:::grep=beta")
    assert "ERROR" not in out, repr(out)
    assert "beta" in out and "alpha" not in out, repr(out)


def test_empty_tokens_alone_are_not_extra(tmp_path: Path) -> None:
    """`:::` yields empty parts. They are separator artifacts, not arguments."""
    f = tmp_path / "doc.md"
    f.write_text("alpha" + chr(10) + "beta" + chr(10))
    out = supertool.dispatch(f"read:{f}::")
    assert "ERROR" not in out, repr(out)
    assert "alpha" in out, repr(out)


# ---------------------------------------------------------------------------
# #1345 — grep, and the three siblings the issue asks about
# ---------------------------------------------------------------------------

def test_third_trailing_integer_is_refused(tmp_path: Path) -> None:
    """The filed call: limit 5, context 3, and `2` on the floor."""
    f = _lines(tmp_path, "code.py", 30)
    out = supertool.dispatch(f"grep:line1:{f}:5:3:2")
    assert "ERROR:" in out, (
        "a peeled-and-unread token runs a call nobody typed: " + repr(out))


def test_the_grep_refusal_names_the_slot_order(tmp_path: Path) -> None:
    f = _lines(tmp_path, "code.py", 30)
    out = supertool.dispatch(f"grep:line1:{f}:5:3:2")
    assert "LIMIT" in out and "CONTEXT" in out, repr(out)


def test_two_trailing_integers_still_work(tmp_path: Path) -> None:
    f = _lines(tmp_path, "code.py", 30)
    out = supertool.dispatch(f"grep:line1:{f}:5:3")
    assert "ERROR" not in out, repr(out)
    assert "line1" in out, repr(out)


def test_misplaced_all_keeps_its_own_message(tmp_path: Path) -> None:
    """#1328's refusal is more specific and must not be shadowed."""
    f = _lines(tmp_path, "code.py", 30)
    out = supertool.dispatch(f"grep:line1:{f}:5:3:all")
    assert "outside the LIMIT slot" in out, repr(out)


def test_grep_around_extra_token_is_refused(tmp_path: Path) -> None:
    """grep_around:PATTERN:PATH:N:LIMIT — a fifth token was dropped."""
    f = _lines(tmp_path, "code.py", 30)
    out = supertool.dispatch(f"grep_around:line1:{f}:2:5:9")
    assert "ERROR:" in out, repr(out)
    assert "9" in out, repr(out)


def test_around_line_extra_token_is_refused(tmp_path: Path) -> None:
    """around_line:PATH:LINE[:N] — a fourth token was dropped."""
    f = _lines(tmp_path, "code.py", 30)
    out = supertool.dispatch(f"around_line:{f}:10:2:9")
    assert "ERROR:" in out, repr(out)


@pytest.mark.parametrize("call", [
    "head:{p}:3:zzz",
    "tail:{p}:3:zzz",
    "wc:{p}:zzz",
    "stat:{p}:zzz",
    "map:{p}:zzz",
    "ls:{d}:zzz",
    "tree:{d}:1:zzz",
    "diff:{p}:{p}:zzz",
    "glob:{d}/*.py:zzz",
])
def test_fixed_slot_ops_refuse_an_extra_token(
        tmp_path: Path, call: str) -> None:
    """Same seam, same silence: every one of these answered and dropped it.

    Measured before the fix on 2c8eaf9 — twelve probed, twelve silent. The
    table is the whole population of fixed-slot builtins, so this is the class
    rather than the instance the issue happened to name.
    """
    f = _lines(tmp_path, "code.py", 30)
    out = supertool.dispatch(call.format(p=f, d=tmp_path))
    assert "ERROR:" in out, repr(out)
    assert "zzz" in out, repr(out)


def test_every_table_entry_has_a_case_above() -> None:
    """A row added to `_MAX_COLON_SLOTS` with no case here is a slot number
    nobody exercised — the sweep would read as covering the table and not."""
    covered = {c.split(":", 1)[0] for c in
               test_fixed_slot_ops_refuse_an_extra_token.pytestmark[0].args[1]}
    covered |= {"around_line", "grep_around"}
    missing = sorted(set(supertool._MAX_COLON_SLOTS) - covered)
    assert not missing, missing


def test_fixed_slot_ops_are_unaffected_without_an_extra_token(
        tmp_path: Path) -> None:
    f = _lines(tmp_path, "code.py", 30)
    for call in (f"head:{f}:3", f"tail:{f}:3", f"wc:{f}", f"stat:{f}"):
        out = supertool.dispatch(call)
        assert "ERROR" not in out, f"{call}: {out!r}"


# `_MAX_COLON_SLOTS` reads as an inventory of ops, so it is swept for ghosts by
# `tests/test_registry_names_dispatch_1285.py` alongside the other four tables
# rather than checked again here.


# ---------------------------------------------------------------------------
# Adjacent, found while auditing the read tail: `full` beside an explicit range
# ---------------------------------------------------------------------------

def test_full_does_not_swallow_an_explicit_range(
        tmp_path: Path, monkeypatch) -> None:
    """`read:PATH:2-4:full` returned lines 2..EOF from a human shell.

    `full` lifts the default line cap; the range is not a default. Worse than a
    plain drop, because the window note then relabelled the result `range 2-8
    (START-END form)` — the discarded END laundered by the line written to
    disclose it. The byte cap stays lifted, which is the rest of what `full`
    means.
    """
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    f = _lines(tmp_path, "doc.md", 8)
    out = supertool.dispatch(f"read:{f}:2-4:full")
    assert "range 2-8" not in out, (
        "the note reported a range the caller never typed: " + repr(out))
    assert "line5" not in out, (
        "the explicit END was discarded: " + repr(out))
    assert "line2" in out and "line4" in out, repr(out)


def test_full_without_a_range_still_lifts_the_line_cap(
        tmp_path: Path, monkeypatch) -> None:
    """The guard must not reach the shape `full` exists for."""
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(supertool, "MAX_READ_LINES", 3, raising=False)
    f = _lines(tmp_path, "doc.md", 8)
    out = supertool.dispatch(f"read:{f}:full")
    assert "line8" in out, repr(out)


# ---------------------------------------------------------------------------
# #1342 — the window note
# ---------------------------------------------------------------------------

def test_eof_and_limit_on_the_same_line_is_not_a_decline() -> None:
    """`end == total` is decided by a number the header already printed."""
    note = supertool._read_window_note(
        path="f.txt", offset=17, limit=3, shown=3, last_scanned=20,
        line_count=20, capped=False, byte_cap=20480, range_form=True)
    assert "cannot be told apart" not in note, (
        "the op holds the total line count and is declining anyway: "
        + repr(note))
    assert "the end of the file" in note, repr(note)


def test_the_note_still_says_the_limit_landed_there_too() -> None:
    """Decisive is not the same as silent — the caller who set the limit is
    told it was reached, they are just told the file also ends there."""
    note = supertool._read_window_note(
        path="f.txt", offset=17, limit=3, shown=3, last_scanned=20,
        line_count=20, capped=False, byte_cap=20480, range_form=True)
    assert "the limit was reached" in note, repr(note)


def test_a_limit_short_of_eof_is_unchanged() -> None:
    note = supertool._read_window_note(
        path="f.txt", offset=0, limit=3, shown=3, last_scanned=3,
        line_count=20, capped=False, byte_cap=20480, range_form=True)
    assert "stopping at line 3: the limit was reached" in note, repr(note)


def test_two_reasons_that_do_not_include_eof_still_decline() -> None:
    """The byte cap and the limit landing together is a real ambiguity: more
    file remains and nothing on hand says which bound would move first."""
    note = supertool._read_window_note(
        path="f.txt", offset=0, limit=3, shown=3, last_scanned=3,
        line_count=20, capped=True, byte_cap=20480, range_form=True)
    assert "cannot be told apart" in note, repr(note)
