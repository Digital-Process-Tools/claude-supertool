"""Three hints that named something other than the thing they were about (#1489).

* `edit`'s nearest-match scored only the FIRST non-blank line of a multi-line
  anchor, so a block anchored on boilerplate scored the same on every copy of
  that boilerplate — 68% twice, ~800 lines apart, in the run that filed this.
  The number was not too low, it was measuring the wrong thing.
* `grep` truncates a long line with `... (+N chars)` and names no way to get
  the rest, which is exactly what a caller needs when the line is an `edit`
  anchor. `read:PATH:N-N` returns it byte-exactly.
* `read:PATH:A:B` disclosed OFFSET:LIMIT twice: once above the body (#1432)
  and once in a trailing `note:` under it.
"""

from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# edit: the score has to be about the block, not about its first line
# ---------------------------------------------------------------------------

def test_block_anchor_scores_the_block_not_its_first_line(tmp_path: Path) -> None:
    """Both blocks open on the same line, so a first-line score cannot tell them
    apart and picks whichever came first. The rest of the anchor can."""
    f = tmp_path / "app.py"
    f.write_text(
        "def handler(request):\n"
        "    log('first')\n"
        "    return alpha(request)\n"
        + "# filler\n" * 800
        + "def handler(request):\n"
        "    log('second')\n"
        "    return beta(request)\n"
    )
    second = 3 + 800 + 1
    out = supertool.op_edit(
        "def handler(request):\n    log('second')\n    return beta(req)",
        "x", str(f))
    assert "ERROR: old string not found" in out
    assert f"nearest match at lines {second}-{second + 2}" in out
    assert "lines 1-3" not in out


def test_two_identical_blocks_withhold_the_line_number(tmp_path: Path) -> None:
    """Two copies of the anchor's neighbourhood score identically. Naming one of
    them is a coin flip reported as a fact — the third state is `cannot
    suggest`, and it names the rivals so the caller can pick."""
    f = tmp_path / "app.py"
    f.write_text(
        "def handler(request):\n"
        "    log('same')\n"
        "    return alpha(request)\n"
        + "# filler\n" * 40
        + "def handler(request):\n"
        "    log('same')\n"
        "    return alpha(request)\n"
    )
    out = supertool.op_edit(
        "def handler(request):\n    log('same')\n    return alpha(req)",
        "x", str(f))
    assert "ERROR: old string not found" in out
    assert "cannot suggest" in out
    assert "nearest match at line" not in out
    assert "44" in out


def test_more_ties_than_windows_scored_says_at_least(tmp_path: Path) -> None:
    """Only `_EDIT_NEAR_WINDOWS` windows are char-scored, so with more ties than
    that the count is a floor and has to read as one. It also has to be a count:
    the first spelling carried it as a negative line number in the candidate
    list, which sorted ahead of every real line and got printed as one."""
    f = tmp_path / "app.py"
    block = "def handler(request):\n    log('same')\n    return alpha(request)\n"
    # The anchor's MIDDLE line is the one that differs, so exactly one window
    # per block carries both surviving lines. With an end line changed instead,
    # the window one line over carries the same two and the tie is real rather
    # than truncated — which is what the first version of this test measured.
    f.write_text(("# filler\n# filler\n" + block) * 25)
    out = supertool.op_edit(
        "def handler(request):\n    log('changed')\n    return alpha(request)",
        "x", str(f))
    assert "cannot suggest" in out
    assert "at least 25 places" in out
    assert "line -" not in out
    assert "-19" not in out


def test_repeated_single_line_withholds_too(tmp_path: Path) -> None:
    """The same argument one line wide: a boilerplate line that occurs six times
    scores identically six times."""
    f = tmp_path / "app.py"
    f.write_text("".join(
        f"def f{i}():\n    return None\n" for i in range(6)))
    out = supertool.op_edit("    return Nine", "x", str(f))
    assert "ERROR: old string not found" in out
    assert "cannot suggest" in out
    assert "nearest match at line" not in out


def test_an_unambiguous_block_is_still_named(tmp_path: Path) -> None:
    """The gate withholds on a tie, not on multi-line anchors generally."""
    f = tmp_path / "app.py"
    f.write_text("alpha = 1\nbeta = 2\ngamma = 3\n")
    out = supertool.op_edit("alpha = 1\nbeta = 22\n", "x", str(f))
    assert "nearest match at lines 1-2" in out


def test_a_minified_file_does_not_hang_the_diagnostic(tmp_path: Path) -> None:
    """`_EDIT_DIAG_MAX_LINES` bounds the scan by LINE COUNT, which is the one
    dimension a minified file is small in: 60 lines of 40 KB scored a
    character-level ratio of 40000x39000 cells and took 220s on this machine.
    Bounded per comparison and in total, and running out is a decline rather
    than a best-so-far reported as a best."""
    import time

    f = tmp_path / "bundle.js"
    f.write_text("".join(f"var a{i}=" + "x" * 40000 + ";\n" for i in range(60)))
    started = time.monotonic()
    out = supertool.op_edit("var a7=" + "x" * 39000 + "!;", "q", str(f))
    elapsed = time.monotonic() - started
    assert "ERROR: old string not found" in out
    assert elapsed < 15, f"diagnostic took {elapsed:.1f}s"
    assert "cannot suggest" in out


def test_a_clipped_score_says_what_it_scored(tmp_path: Path) -> None:
    """100% on the first 1000 characters of a 1.5 KB line is not 100% on the
    line, and the difference is the whole reason the anchor missed."""
    f = tmp_path / "wide.txt"
    f.write_text("a" * 1500 + "tail\nzzz\n")
    out = supertool.op_edit("a" * 1400 + "q", "x", str(f))
    assert "nearest match at line 1" in out
    assert "scored on the first 1000 characters" in out
    assert len(out.splitlines()) == 2


# ---------------------------------------------------------------------------
# grep: a cut line has to name the way back to the bytes
# ---------------------------------------------------------------------------

def test_grep_truncation_names_the_read_that_recovers_the_line(tmp_path: Path) -> None:
    """Two files, so the auto-read that would have shown the whole line does
    not fire — which is the case the note exists for."""
    f = tmp_path / "wide.md"
    f.write_text("short\n" + "| needle " + "x" * 900 + " |\n")
    (tmp_path / "other.md").write_text("needle\n")
    out = supertool.dispatch(f"grep:needle:{tmp_path}")
    assert "chars)" in out
    assert "wide.md:2-2" in out
    assert "byte-exact" in out


def test_grep_says_nothing_when_no_line_was_cut(tmp_path: Path) -> None:
    f = tmp_path / "narrow.md"
    f.write_text("needle here\n")
    out = supertool.dispatch(f"grep:needle:{f}")
    assert "byte-exact" not in out


# ---------------------------------------------------------------------------
# read: one disclosure, above the body
# ---------------------------------------------------------------------------

def test_offset_limit_disclosure_is_not_printed_twice(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_text("".join(f"line{i}\n" for i in range(1, 101)))
    out = supertool.dispatch(f"read:{f}:52:72")
    assert out.count("OFFSET:LIMIT, not START:END") == 1
    assert "note: this asked for" not in out


def test_offset_limit_disclosure_arrives_before_the_body(tmp_path: Path) -> None:
    """A correction under the output it would have avoided is a round-trip late."""
    f = tmp_path / "many.txt"
    f.write_text("".join(f"line{i}\n" for i in range(1, 101)))
    out = supertool.dispatch(f"read:{f}:52:72")
    assert out.index("OFFSET:LIMIT, not START:END") < out.index("line53")
    assert f"read:{f}:52-72" in out
