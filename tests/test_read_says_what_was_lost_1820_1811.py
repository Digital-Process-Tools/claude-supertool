"""#1820 and #1811 — `read` must say whether anything was lost, and where to go next.

Both are the house defect on the same op's own messages.

#1820: a window that came back **whole** and a window that was **cut short** both
rendered as `stopping at line N: ...`. The stop clause names which bound closed
the window; it never said whether that bound cost the caller anything. The only
way to tell a satisfied read from a truncated one was to read again, wider, and
compare — which is what it cost the reporter.

Second half of #1820: the word "limit" was doing two jobs. `read:PATH:10:20` ends
at a bound the caller typed and needs no action; `read:PATH:10` ends at the
`read.max_lines` default the caller never named, which is the op's own cap on
output and hides the rest of the file. Both said `the limit was reached`.

#1811: a whole-file read of a large file previews its head and then stops. The
stop was disclosed but the way out was not — `... (N more lines)` names no
remedy at all, and the byte-cap footer named only `read:PATH:OFFSET:LIMIT`, the
one form this repo has three issues' worth of evidence that callers misread
(#382, #1417, #1489).

Every negative assertion here is paired with a positive control in the same
fixture, so a harness that renders nothing cannot pass the "must not say" half.
"""

from __future__ import annotations

import re
from pathlib import Path

import supertool

_WINDOW_RE = re.compile(r"^window: .*$", re.M)


def _window(out: str) -> str:
    m = _WINDOW_RE.search(out)
    assert m, f"no window note in:\n{out[:2000]}"
    return m.group(0)


def _lines(tmp_path: Path, n: int, name: str = "many.txt") -> Path:
    f = tmp_path / name
    f.write_bytes(b"".join(b"L%d\n" % i for i in range(1, n + 1)))
    return f


# --- #1820: satisfied is not the same render as truncated -------------------


def test_a_satisfied_range_says_nothing_was_cut(tmp_path: Path) -> None:
    """The report's own case: `read:PATH:START-END` fully delivered.

    Today this renders `stopping at line 20: the limit was reached`, which is
    the sentence a truncated read prints too.
    """
    f = _lines(tmp_path, 100)
    note = _window(supertool.dispatch(f"read:{f}:10-20"))
    assert "nothing was cut" in note, note


def test_a_cut_window_does_not_claim_nothing_was_cut(tmp_path: Path) -> None:
    """Positive control for the assertion above.

    A read the byte cap genuinely cut short must NOT carry the satisfied
    clause — otherwise the clause is decoration and the two states are still
    one. Line 2 is over the cap on its own and lines follow it.
    """
    f = tmp_path / "fat.txt"
    f.write_text("a\n" + "N" * 25000 + "\n" + "c\n" + "d\n")
    note = _window(supertool.dispatch(f"read:{f}:1:3"))
    assert "cut short" in note, note
    assert "nothing was cut" not in note, note


def test_eof_does_not_also_claim_nothing_was_cut(tmp_path: Path) -> None:
    """EOF already settles it in its own words (#1342) — `nothing follows line
    N`. Adding a second verdict beside it would be two speakers on one
    question, which is what #1489 removed one clause over."""
    f = _lines(tmp_path, 100)
    note = _window(supertool.dispatch(f"read:{f}:90:10"))
    assert "nothing follows line 100" in note, note
    assert "nothing was cut" not in note, note


# --- #1820: which limit, the caller's or the op's ---------------------------


def test_the_op_default_line_cap_is_not_called_the_callers_limit(
    tmp_path: Path,
) -> None:
    """`read:PATH:10` names no LIMIT. The bound that ends it is
    `read.max_lines`, and calling that "the limit" reports the op's own output
    cap in the vocabulary of a request the caller never made."""
    f = _lines(tmp_path, 1000)
    note = _window(supertool.dispatch(f"read:{f}:10"))
    assert "read.max_lines" in note, note
    # And it is NOT the satisfied state. Caught building this: the first
    # version fired the #1820 verdict here too, so the note read `you named no
    # LIMIT, so this bound is the op's — the window ends here because it was
    # asked to`. Nobody asked, and 690 lines are below the bound.
    assert "nothing was cut" not in note, note
    assert "690 lines of the file are below it" in note, note


def test_a_typed_limit_is_still_called_the_limit(tmp_path: Path) -> None:
    """Positive control: the caller DID close this window, and #945's own
    tests pin that wording. The reword above must not reach this case."""
    f = _lines(tmp_path, 1000)
    note = _window(supertool.dispatch(f"read:{f}:10:20"))
    assert "the limit was reached" in note, note
    assert "read.max_lines" not in note, note


# --- #1820 at offset 0, where there is no window note at all ----------------


def test_a_satisfied_range_from_line_1_is_not_rendered_as_a_truncation(
    tmp_path: Path,
) -> None:
    """`read:PATH:1-50` never reaches the window note.

    `_read_window_note` is called only when `offset > 0`, and a range starting
    at line 1 has an offset of 0 — so the whole #1820 fix above missed the
    shape it is most likely to be asked about. Both this and the case below
    closed with a bare `... (150 more lines)`, byte for byte identical, one
    having delivered exactly what was asked and the other having been cut by a
    bound the caller never set. Found by the auditor on the committed diff.
    """
    f = _lines(tmp_path, 200)
    out = supertool.dispatch(f"read:{f}:1-50")
    assert "nothing was cut" in out, out[-500:]


def test_the_default_cap_at_offset_0_says_it_was_the_op_that_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    """The positive control, and the other half of the ambiguity: a plain
    `read:PATH` stopped by `read.max_lines` must NOT read as a satisfied
    window, and must name the bound that actually stopped it."""
    monkeypatch.setenv("SUPERTOOL_READ_MAX_LINES", "50")
    f = _lines(tmp_path, 200)
    out = supertool.dispatch(f"read:{f}")
    assert "nothing was cut" not in out, out[-500:]
    assert "read.max_lines" in out, out[-500:]


def test_a_typed_window_at_offset_0_is_not_offered_narrowing_advice(
    tmp_path: Path,
) -> None:
    """A caller who typed `read:PATH:1-50` has demonstrated they know the
    forms; #1811's advice is for the caller who typed no window at all."""
    f = _lines(tmp_path, 200)
    out = supertool.dispatch(f"read:{f}:1-50")
    assert "START-END" not in out, out[-500:]


def test_glob_auto_read_does_not_claim_a_window_nobody_asked_for(
    tmp_path: Path,
) -> None:
    """`glob:PATH` auto-reads through `render_file` with the op's own default
    pre-resolved into the LIMIT slot, so `limit <= 0` never fires and the
    default read as a bound the caller had typed. The footer then claimed
    `lines 1-300 are the whole window asked for, nothing was cut` while
    `read.max_lines` had cut 100 lines and nobody had asked for any window at
    all — this fix's own defect, inverted. Found by the reviewer on the
    working tree, and not by the suite, because nothing here drove the
    auto-read paths.
    """
    f = _lines(tmp_path, 400, name="big.txt")
    out = supertool.dispatch(f"glob:{f}")
    assert "more lines" in out, out[-400:]
    assert "nothing was cut" not in out, out[-500:]
    assert "read.max_lines" in out, out[-500:]


def test_render_file_declines_when_nobody_said_whether_a_window_was_asked_for(
    tmp_path: Path,
) -> None:
    """The third state, for a caller that says nothing.

    `limit_defaulted` defaults to `None` — *unknown* — rather than to `False`.
    `False` is a positive claim that the caller typed this bound, and a future
    call site that pre-resolves the default (as all three auto-read sites did)
    would silently inherit it and print the same false verdict. Unknown prints
    neither verdict, which is honest, rather than the flattering one.
    """
    f = _lines(tmp_path, 400, name="big.txt")
    out = supertool.render_file(str(f), 0, 300)
    assert "more lines" in out, out[-400:]
    assert "nothing was cut" not in out, out[-400:]
    assert "read.max_lines" not in out, out[-400:]


# --- #1811: the way out, named where the caller is standing -----------------


def test_a_line_capped_whole_file_read_names_the_narrowing_forms(
    tmp_path: Path,
) -> None:
    """`read:PATH` on a long file stops at `read.max_lines` and closes with
    `... (N more lines)` — a disclosure with no remedy attached."""
    f = _lines(tmp_path, 1000)
    out = supertool.dispatch(f"read:{f}")
    assert "more lines" in out, out[-400:]
    assert f"read:{f}:START-END" in out, out[-600:]
    assert "grep=PATTERN" in out, out[-600:]


def test_a_byte_capped_read_names_the_narrowing_forms(tmp_path: Path) -> None:
    """The byte-cap footer had a remedy, but only the OFFSET:LIMIT form — the
    spelling #382/#1417/#1489 record callers reading as START:END."""
    f = tmp_path / "wide.txt"
    f.write_text("".join("W" * 400 + "\n" for _ in range(200)))
    out = supertool.dispatch(f"read:{f}")
    assert "truncated at" in out, out[-400:]
    assert f"read:{f}:START-END" in out, out[-600:]
    assert "grep=PATTERN" in out, out[-600:]


def test_a_whole_small_file_gets_no_narrowing_advice(tmp_path: Path) -> None:
    """Positive control for both assertions above. A read that returned the
    file has nothing to narrow, and advice printed on every read is advice
    nobody reads."""
    f = _lines(tmp_path, 5)
    out = supertool.dispatch(f"read:{f}")
    assert "L5" in out, out
    assert "START-END" not in out, out
    assert "grep=PATTERN" not in out, out


def test_the_rtk_delegated_read_also_names_the_narrowing_forms(
    tmp_path: Path, monkeypatch
) -> None:
    """The branch that actually fires where this was reported.

    `read:PATH` with no offset, limit or filter delegates to rtk, which renders
    its own head preview and its own footer and knows nothing about supertool's
    call forms. The delegation returned rtk's output unchanged, so the fix
    above would not have reached the caller who filed #1811.
    """
    f = _lines(tmp_path, 1000)
    monkeypatch.setattr(supertool, "_rtk_enabled", lambda: True)
    monkeypatch.setattr(supertool, "_has_rtk", lambda: True)
    monkeypatch.setattr(
        supertool, "_rtk_run",
        lambda args, timeout=30: "  1 | L1\n// ... 999 more lines\n")
    out = supertool.dispatch(f"read:{f}")
    assert "L1" in out, out
    assert f"read:{f}:START-END" in out, out
    assert "grep=PATTERN" in out, out


def test_the_rtk_delegated_read_of_a_small_file_gets_no_advice(
    tmp_path: Path, monkeypatch
) -> None:
    """Positive control for the delegated branch."""
    f = _lines(tmp_path, 5)
    monkeypatch.setattr(supertool, "_rtk_enabled", lambda: True)
    monkeypatch.setattr(supertool, "_has_rtk", lambda: True)
    monkeypatch.setattr(
        supertool, "_rtk_run",
        lambda args, timeout=30: "  1 | L1\n  2 | L2\n")
    out = supertool.dispatch(f"read:{f}")
    assert "START-END" not in out, out
