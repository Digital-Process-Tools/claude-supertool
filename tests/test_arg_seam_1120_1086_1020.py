r"""One seam, three issues: what an op's positional arguments were read as.

#1120, #1086, #1020. Every case here is the same defect wearing a different op:
the caller's intent and the parse disagreed, the op answered anyway, and the
answer was shaped like a successful one.

* #1120 — `grep:\| \{:PATH` matched every line and reported it as a normal
  search. The mechanism is NOT the `:`-tokenizer (the filed hypothesis): it is
  the unconditional bash-BRE rewrite of `\|` to `|`, which turns an escaped
  literal pipe into a top-level alternation with an EMPTY first branch. An empty
  branch matches the empty string, so the pattern matches every line.
* #1086 — `around:PATH:LINE:N` reads LINE as the path. `around_line` is the same
  op with the same output; the split is in the names, not in the answer.
* #1020 — `read:PATH:A:B` is OFFSET:LIMIT. #382 added a note for the misread but
  gated it on the window overrunning EOF — which the reported case does not do,
  so the note never fired on the very call that was filed.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _numbered(tmp_path: Path, name: str, count: int) -> Path:
    f = tmp_path / name
    f.write_bytes(("\n".join(f"line{i}" for i in range(1, count + 1)) + "\n").encode())
    return f


# ---------------------------------------------------------------------------
# #1120 — a pattern that matches every line is refused, not reported
# ---------------------------------------------------------------------------

def test_escaped_pipe_does_not_silently_match_every_line(tmp_path: Path) -> None:
    """The reported call: a literal pipe-space-brace, whose BRE rewrite leaves an
    empty first alternation branch that matches everything."""
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta | { gamma\ndelta\n")
    out = supertool.op_grep(r"\| \{", str(f), limit=10)
    assert out.startswith("ERROR:"), (
        "a pattern that matches every line is a saturation, not a search, and "
        "its report is indistinguishable from a large result set: " + repr(out))
    assert "alpha" not in out, (
        "refusing means returning no results, not results plus a warning: "
        + repr(out))


def test_saturating_pattern_refusal_names_the_rewrite(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\n")
    out = supertool.op_grep(r"\| \{", str(f), limit=10)
    assert "empty alternation branch" in out, out
    assert "[|]" in out, (
        "the refusal has to carry the spelling that works, or it is a "
        "diagnosis without a next move: " + repr(out))


def test_bare_leading_pipe_is_refused_without_any_rewrite(tmp_path: Path) -> None:
    """The saturation is a property of the pattern, not of the rewrite — a
    caller who types the empty branch directly gets the same refusal."""
    f = tmp_path / "code.py"
    f.write_text("alpha\n")
    out = supertool.op_grep("|alpha", str(f), limit=10)
    assert out.startswith("ERROR:") and "empty alternation branch" in out, out


def test_bre_alternation_still_works_and_says_it_rewrote(tmp_path: Path) -> None:
    """The muscle-memory case the rewrite exists for keeps working — but the
    caller is told the pattern that ran is not the one that was typed."""
    f = tmp_path / "code.py"
    f.write_text("alpha\ngamma\n")
    out = supertool.op_grep(r"alpha\|gamma", str(f), limit=10)
    assert not out.startswith("ERROR:"), out
    assert "alpha" in out and "gamma" in out, out
    assert "alpha|gamma" in out, (
        "the rewritten pattern must be echoed, or an escape being eaten is "
        "invisible: " + repr(out))


def test_empty_branch_inside_a_group_is_not_saturating(tmp_path: Path) -> None:
    """`colo(u|)r` has an empty branch, matches color and colour, and matches
    nothing else. Only a TOP-LEVEL empty branch saturates."""
    f = tmp_path / "code.py"
    f.write_text("color\ncolour\nbanana\n")
    out = supertool.op_grep("colo(u|)r", str(f), limit=10)
    assert not out.startswith("ERROR:"), out
    assert "banana" not in out.split("[auto-read")[0], out


def test_empty_branch_in_a_character_class_is_not_saturating(tmp_path: Path) -> None:
    """A pipe inside a character class is an ordinary character and starts no
    alternation."""
    f = tmp_path / "code.py"
    f.write_text("a|b\nzzz\n")
    out = supertool.op_grep("[|]b", str(f), limit=10)
    assert not out.startswith("ERROR:"), out


def test_around_discloses_the_rewrite_too(tmp_path: Path) -> None:
    """`around` applies the same rewrite, so it owes the same disclosure. The
    refusal alone covers only the saturating case; a rewrite that merely changes
    the pattern is exactly as invisible in `around` as it was in `grep`."""
    f = tmp_path / "code.py"
    f.write_text("alpha\ngamma\n")
    out = supertool.op_around(r"alpha\|gamma", str(f), 2)
    assert not out.startswith("ERROR:"), out
    assert "alpha|gamma" in out, (
        "the rewritten pattern must be echoed by `around` as well: " + repr(out))


def test_around_refuses_the_saturating_pattern_too(tmp_path: Path) -> None:
    """`around` carries its own copy of the BRE rewrite, so it carries the bug."""
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta | { gamma\n")
    out = supertool.op_around(r"\| \{", str(f), 2)
    assert out.startswith("ERROR:") and "empty alternation branch" in out, out


# ---------------------------------------------------------------------------
# #1086 — `around` answers its own sibling's argument form
# ---------------------------------------------------------------------------

def test_around_with_a_numeric_path_answers_instead_of_erroring(tmp_path: Path) -> None:
    """The live report: `around:PATH:1160:20` — a numeric path is never a real
    file, so the only reading that is not an error is around_line's."""
    f = _numbered(tmp_path, "many.txt", 60)
    out = supertool.dispatch(f"around:{f}:30:4")
    assert not out.startswith("ERROR:") and "file not found" not in out, out
    assert "line30" in out, out


def test_around_line_delegation_discloses_the_reinterpretation(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 60)
    out = supertool.dispatch(f"around:{f}:30:4")
    assert not out.startswith("ERROR:"), (
        "the old error ALSO named around_line — naming it while refusing to "
        "answer is what this test must not accept: " + repr(out))
    assert "around_line" in out, (
        "answering a call the caller did not write means saying so, or the "
        "lesson is lost and the next call repeats it: " + repr(out))


def test_around_delegation_honours_the_n_the_caller_typed(tmp_path: Path) -> None:
    """The old error said `around_line:PATH:LINE[:N]` — a placeholder for an N
    the caller had already typed, so the retype was mandatory."""
    f = _numbered(tmp_path, "many.txt", 60)
    out = supertool.dispatch(f"around:{f}:30:2")
    assert "line28" in out and "line32" in out, out
    assert "line27" not in out, "N=2 must mean 2, not the default 10: " + out


def test_around_with_a_real_numeric_pattern_is_untouched(tmp_path: Path) -> None:
    """A pattern that is genuinely digits, with a real path, still greps."""
    f = tmp_path / "code.py"
    f.write_text("x = 30\ny = 1\n")
    out = supertool.dispatch(f"around:30:{f}:1")
    assert "x = 30" in out, out


def test_around_with_a_nonexistent_nonnumeric_path_still_errors(tmp_path: Path) -> None:
    out = supertool.dispatch("around:alpha:no/such/file.txt")
    assert out.startswith("ERROR:") or "not found" in out, out


# ---------------------------------------------------------------------------
# #1020 — the OFFSET:LIMIT note fires on the case that was actually filed
# ---------------------------------------------------------------------------

def test_read_colon_form_note_fires_when_the_window_stays_inside_the_file(
        tmp_path: Path) -> None:
    """#382's note was gated on OFFSET+LIMIT overrunning EOF. The filed call
    (offset 5370, limit 5460 on a 19571-line file) does not overrun, so the note
    stayed silent on the exact shape it was written for."""
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:100:150")
    assert f"read:{f}:100-150" in out, (
        "the reading that was NOT taken has to be named, or the caller cannot "
        "tell an intended window from an accidental one: " + repr(out))


def test_read_colon_form_note_names_both_line_counts(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:100:150")
    assert "51 lines" in out, (
        "the range reading's size is the number that makes the mistake "
        "obvious: " + repr(out))


def test_read_range_form_gets_no_note(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:100-200")
    assert "OFFSET:LIMIT" not in out, out


def test_read_plain_limit_gets_no_note(tmp_path: Path) -> None:
    """No OFFSET typed at all — there is no second reading to disclose."""
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:0:200")
    assert "OFFSET:LIMIT" not in out, out


def test_read_limit_smaller_than_offset_gets_no_note(tmp_path: Path) -> None:
    """A LIMIT below the OFFSET cannot be an END line, so nothing is ambiguous."""
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:200:50")
    assert "OFFSET:LIMIT" not in out, out


def test_read_limit_far_above_offset_stays_quiet(tmp_path: Path) -> None:
    """#382's counter-example, and the reason the gate is a ratio rather than a
    blanket widening: `10:20` is an ordinary skip-then-read, and a note on every
    LIMIT > OFFSET call would fire on it. An END line lands near its START; an
    independent LIMIT does not."""
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:10:200")
    assert "OFFSET:LIMIT" not in out, out
