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

import difflib
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


def test_a_long_anchor_still_reaches_the_at_least_disclosure(
        tmp_path: Path) -> None:
    """The tie floor has to fire when the anchor is TALLER than the sample.

    `_nearest_block_candidates` char-scores at most `_EDIT_NEAR_WINDOWS` (20)
    of the windows that tied on the line pass, in file order. `tie_floor` says
    "more tied than were scored" — and until #1614 it was only consulted
    inside `if rivals:`, where `rivals` discards every candidate within `n`
    lines of the leader as one neighbourhood. For any anchor over 20 lines all
    20 sampled starts are necessarily within `n` of each other, so `rivals` is
    always empty and the floor could never be reached: the tool named the
    first sampled window at 98% and said nothing about the 14 it had not
    scored. That is exactly the confident single answer #1489 removed, coming
    back through the case #1489 was filed about — a line number roughly 800
    lines from the real one.
    """
    f = tmp_path / "app.py"
    f.write_text("\n".join(
        ["    pass"] * 60
        + [f"filler {i}" for i in range(61, 500)]
        + ["    pass"] * 29 + ["    return z"]
        + ["tail"] * 10) + "\n")
    old = "\n".join(["    pass"] * 29 + ["    return y"])
    out = supertool.op_edit(old, "x", str(f))
    assert "ERROR: old string not found" in out
    assert "cannot suggest" in out
    assert "at least 34 places" in out
    # The window it would have named, and the count it would have hidden.
    assert "nearest match at lines" not in out
    assert "and 33 more" in out


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


def test_a_minified_file_bounds_the_scan_by_work_not_by_the_clock(
        tmp_path: Path, monkeypatch) -> None:
    """`_EDIT_DIAG_MAX_LINES` bounds the scan by LINE COUNT, which is the one
    dimension a minified file is small in: 60 lines of 40 KB scored a
    character-level ratio of 40000x39000 cells and took 220s on this machine.
    Bounded per comparison and in total, and running out is a decline rather
    than a best-so-far reported as a best.

    Measured in CELLS HANDED TO `difflib`, not in seconds (#1623). The first
    spelling asserted `elapsed < 15`, which measures the runner: it went red
    three times in one hour on a laptop running five suites, once blocking the
    v0.41.0 release push, while the same commit was green on all 18 CI legs. A
    wall clock in a test renders an environment limit as a product verdict --
    this repo's own defect class (#1143), relocated into the thing meant to
    detect it.

    It still fails if either bound goes. Without `_EDIT_NEAR_BUDGET` the scan
    runs all 60 comparisons and reports `60 places score the same (100%)`, so
    both the call count and the wording move; without `_EDIT_NEAR_MAX_CHARS`
    the first comparison asks for 39000x40000 cells and the counter refuses it
    on the spot rather than waiting out the 220s it would take.
    """
    cells: list[int] = []
    real_ratio = difflib.SequenceMatcher.ratio

    def counted(self):  # type: ignore[no-untyped-def]
        n = len(self.a) * len(self.b)
        if n > supertool._EDIT_NEAR_MAX_CHARS ** 2:
            raise AssertionError(
                f"one comparison asked difflib for {n} cells, over the "
                f"{supertool._EDIT_NEAR_MAX_CHARS ** 2}-cell clip")
        cells.append(n)
        return real_ratio(self)

    monkeypatch.setattr(difflib.SequenceMatcher, "ratio", counted)

    f = tmp_path / "bundle.js"
    f.write_text("".join(f"var a{i}=" + "x" * 40000 + ";\n" for i in range(60)))
    out = supertool.op_edit("var a7=" + "x" * 39000 + "!;", "q", str(f))
    assert "ERROR: old string not found" in out
    # The decline has to be the BUDGET one. `cannot suggest` alone is not a
    # pin: with the budget lifted this same input declines for a tie instead,
    # so the loose wording passed against an unbounded scan.
    assert "cost more than the scan's budget" in out
    per_call = supertool._EDIT_NEAR_MAX_CHARS ** 2
    assert sum(cells) <= supertool._EDIT_NEAR_BUDGET, sum(cells)
    assert len(cells) <= supertool._EDIT_NEAR_BUDGET // per_call + 1, len(cells)


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
