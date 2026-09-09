"""#2358 -- vim's `:s` substitutions silently double-apply on a re-run.

Split from #938/#701, which gave `edit` and `replace` a `K re-applied` footer
counter via a POSITIONAL CONTAINMENT check: is the literal `old` about to be
replaced already sitting inside a copy of the literal `new` some earlier run
wrote. `vim`'s `:s` goes through a regex engine, where `old` is a pattern
rather than a fixed string, so "is old contained in new" has no fixed meaning
once `old` can hold capture groups, backreferences, anchors or classes --
UNLESS the comparison is made per-match, using the literal text a match
actually consumed (`m.group(0)`) and the literal text this run is about to
write in its place (`m.expand(repl)`, which resolves any backreference using
that same match). That reduces every match to exactly the check #938 already
proved, so detection is NOT limited to a backreference-free subset -- the
fixture below includes a `\\1\\1`-style backreference case for that reason.

The reproduction, verbatim from the issue: `%s/foo/foo-bar/` run twice
silently doubles the suffix, with byte-identical receipts both times
(`1 subs`, `[result] 1 op run, 1 write`).

Every test here is written to fail if the code did nothing: each asserts a
token the pre-fix output does not contain, or an absence against a case where
the naive `new in content` (or `old in new`, on the pattern SOURCE rather than
the match) heuristic would have fired.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def _result_line_of(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("[result] "):
            return line
    return ""


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------

def test_second_run_of_the_same_substitution_is_disclosed(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo\n", encoding="utf-8")

    first = supertool.dispatch(f"vim:{f}::%s/foo/foo-bar/")
    assert "re-applied" not in first
    assert f.read_text(encoding="utf-8") == "foo-bar\n"

    second = supertool.dispatch(f"vim:{f}::%s/foo/foo-bar/")
    assert f.read_text(encoding="utf-8") == "foo-bar-bar\n"
    assert "re-applied" in second, second
    assert "re-applied" in _result_line_of(second), second


def test_a_first_application_says_nothing(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo\n", encoding="utf-8")
    out = supertool.dispatch(f"vim:{f}::%s/foo/foo-bar/")
    assert "re-applied" not in out
    assert _result_line_of(out) == "[result] 1 op run, 1 write"


# ---------------------------------------------------------------------------
# Tractability: backreferences resolve per match, so detection is not limited
# to the backreference-free subset
# ---------------------------------------------------------------------------

def test_backreference_replacement_is_still_detected(tmp_path: Path) -> None:
    """`\\1-\\2-EXTRA` re-expresses the two captured groups verbatim plus a
    fixed suffix -- the literal repl text is the SAME on every match (no
    self-referential growth), so only resolving it via `m.expand()` (rather
    than comparing the pattern SOURCE `(foo)-(bar)` against the repl source
    `\\1-\\2-EXTRA`, which is nonsense) can catch the second run."""
    f = tmp_path / "x.txt"
    f.write_text("foo-bar\n", encoding="utf-8")

    first = supertool.op_vim(str(f), ":s/(foo)-(bar)/\\1-\\2-EXTRA/")
    assert "re-applied" not in first
    assert f.read_text(encoding="utf-8") == "foo-bar-EXTRA\n"

    second = supertool.op_vim(str(f), ":s/(foo)-(bar)/\\1-\\2-EXTRA/")
    assert "re-applied" in second, second


def test_a_backreference_that_reorders_text_is_not_a_false_positive(
    tmp_path: Path,
) -> None:
    """`\\2-\\1` swaps two captured groups. After the first run the buffer no
    longer matches the pattern at all (the literal order flipped), so a
    second run finds nothing to substitute -- this must not be misread as a
    re-application."""
    f = tmp_path / "x.txt"
    f.write_text("foo-bar\n", encoding="utf-8")

    first = supertool.op_vim(str(f), ":s/(foo)-(bar)/\\2-\\1/")
    assert "re-applied" not in first
    assert f.read_text(encoding="utf-8") == "bar-foo\n"

    second = supertool.op_vim(str(f), ":s/(foo)-(bar)/\\2-\\1/")
    assert "ERROR" in second
    assert "re-applied" not in second


# ---------------------------------------------------------------------------
# Global substitution: count every re-applied occurrence, not just the first
# ---------------------------------------------------------------------------

def test_global_flag_counts_every_reapplied_occurrence(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar foo\n", encoding="utf-8")

    supertool.op_vim(str(f), ":s/foo/foo-X/g")
    assert f.read_text(encoding="utf-8") == "foo-X bar foo-X\n"

    second = supertool.op_vim(str(f), ":s/foo/foo-X/g")
    assert f.read_text(encoding="utf-8") == "foo-X-X bar foo-X-X\n"
    assert "2 subs" in second
    assert "re-applied" in second


# ---------------------------------------------------------------------------
# Legitimate second application must stay expressible
# ---------------------------------------------------------------------------

def test_intended_second_application_in_a_different_location_still_lands(
    tmp_path: Path,
) -> None:
    """The same substitution applied twice at genuinely different sites is not
    a re-run of the first -- it is two distinct, intended edits. Disclosure
    must not misfire just because `new`-shaped text exists somewhere else."""
    f = tmp_path / "x.txt"
    f.write_text("<list>\n</list>\n", encoding="utf-8")
    script = r":s/<\/list>/  <item\/>" + chr(10) + r"<\/list>/"

    first = supertool.op_vim(str(f), script)
    assert "re-applied" not in first

    second = supertool.op_vim(str(f), script)
    assert (
        f.read_text(encoding="utf-8")
        == "<list>\n  <item/>\n  <item/>\n</list>\n"
    )
    assert "re-applied" in second


def test_new_text_existing_elsewhere_is_not_a_re_application(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("keep return-none\nreplace this\n", encoding="utf-8")
    out = supertool.op_vim(str(f), ":s/replace this/return-none other/")
    assert "re-applied" not in out


# ---------------------------------------------------------------------------
# The literal-fallback autocorrect paths (regex parse error, or zero matches)
# share the same bug and must be covered too
# ---------------------------------------------------------------------------

def test_multiline_non_global_caps_the_reapplied_count_at_one(tmp_path: Path) -> None:
    """A multiline PAT (one containing a literal newline) with no `g` flag
    only substitutes the FIRST match in the whole buffer -- `_run_sub`'s
    own multiline branch calls `rx.subn(..., count=n_max)` with `n_max=1`.
    The reapply count must be capped the same way: reporting more
    re-applied occurrences than substitutions actually made is an
    internally contradictory receipt (#2358 self-review)."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar-X\nfoo\nbar-X\nfoo\nbar\n", encoding="utf-8")

    out = supertool.op_vim(str(f), ":s/foo\\nbar/foo\\nbar-X/")
    assert "1 subs" in out, out
    assert "[2 re-applied]" not in out, out
    assert "[1 re-applied]" in out, out


def test_literal_fallback_after_regex_parse_error_is_covered(tmp_path: Path) -> None:
    """`(` with no closing paren is an invalid regex; the handler falls back
    to a literal match. That path built its own replacement string by hand
    and must be checked the same way."""
    f = tmp_path / "x.txt"
    f.write_text("assertEquals(1, 2)\n", encoding="utf-8")
    script = ":s/assertEquals(/assertEquals(EXTRA "

    first = supertool.op_vim(str(f), script)
    assert "autocorrect" in first
    assert "re-applied" not in first

    second = supertool.op_vim(str(f), script)
    assert "re-applied" in second, second
