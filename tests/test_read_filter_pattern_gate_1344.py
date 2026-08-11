"""#1344 — `read:PATH:::grep=` reached neither the BRE rewrite nor the
saturation refusal, so one pattern meant three things across four routes.

The harm is the usual class inverted: not an absence produced by the tool and
read as an absence in the world, but a *saturation* read as a filter that
matched. `read:probe.txt:::grep=^|x` returned every line of the file under the
ordinary `(N lines, M bytes)` header, and a caller can only conclude that every
one of those lines matched the pattern they typed.

The decision #1344 names as the task: a filter on a bounded window is refused on
the same terms as a search, and every route reaches that decision through ONE
chokepoint, so a fifth route cannot inherit a third behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

import supertool


def _probe(tmp_path: Path) -> Path:
    f = tmp_path / "probe.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    return f


# ---------------------------------------------------------------------------
# The filed call
# ---------------------------------------------------------------------------

def test_saturating_read_filter_is_refused(tmp_path: Path) -> None:
    """`^|x` has a `^` branch that matches at position 0 of every line, so the
    filter matched everything. Returning the file under a plain header lets the
    caller conclude every line matched their pattern."""
    f = _probe(tmp_path)
    out = supertool.op_read(str(f), grep_filter="^|x")
    assert out.startswith("ERROR:"), (
        "a filter that matches every line is not a filter the caller meant, "
        "and `read:PATH` is already the spelling for the whole file: "
        + repr(out))
    assert "alpha" not in out, (
        "refusing means no content, not content plus a note: " + repr(out))


def test_the_read_refusal_names_the_saturating_branch(tmp_path: Path) -> None:
    f = _probe(tmp_path)
    out = supertool.op_read(str(f), grep_filter="^|x")
    assert "`^`" in out, (
        "which half saturated is the whole diagnosis: " + repr(out))


def test_escaped_pipe_in_a_read_filter_is_refused_too(tmp_path: Path) -> None:
    r"""`\|x` is what a caller's fingers type for bash-grep BRE alternation.
    Once rewritten it is a bare `|` with an empty left branch — the #1120
    shape, arriving on the one route that had neither the rewrite nor the
    guard."""
    f = _probe(tmp_path)
    out = supertool.op_read(str(f), grep_filter=r"\|x")
    assert out.startswith("ERROR:"), repr(out)
    assert "[|]" in out, (
        "the refusal has to name the spelling that works: " + repr(out))


# ---------------------------------------------------------------------------
# A non-saturating rewrite runs, and says it ran
# ---------------------------------------------------------------------------

def test_bre_alternation_filters_on_both_branches(tmp_path: Path) -> None:
    r"""`alpha\|gamma` meant a literal `alpha|gamma` on this route and matched
    nothing, while every other route read it as an alternation."""
    f = _probe(tmp_path)
    out = supertool.op_read(str(f), grep_filter=r"alpha\|gamma")
    # The emitted, line-numbered rows -- not the word anywhere in the output.
    # A zero quotes the pattern back at the caller, so a substring assertion
    # on "alpha" passes while the filter has matched nothing at all.
    emitted = [ln.split("→", 1)[1] for ln in out.splitlines() if "→" in ln]
    assert emitted == ["alpha", "gamma"], (
        "the rewrite makes `read`'s filter agree with `grep` on syntax: "
        + repr(out))


def test_the_rewrite_is_disclosed_not_silent(tmp_path: Path) -> None:
    """Rewriting without saying so is the trap #1344 points at: it makes the
    routes agree on syntax while the caller still cannot see which pattern
    ran."""
    f = _probe(tmp_path)
    out = supertool.op_read(str(f), grep_filter=r"alpha\|gamma")
    assert "pattern rewritten to" in out, repr(out)
    assert "`alpha|gamma`" in out, repr(out)


def test_an_ordinary_filter_is_untouched(tmp_path: Path) -> None:
    f = _probe(tmp_path)
    out = supertool.op_read(str(f), grep_filter="beta")
    assert "beta" in out and "alpha" not in out, repr(out)
    assert not out.startswith("ERROR:"), repr(out)
    assert "pattern rewritten" not in out, (
        "no rewrite fired, so there is nothing to disclose: " + repr(out))


def test_a_real_alternation_still_filters(tmp_path: Path) -> None:
    """`^$|alpha` matches blank lines or alpha — a real search, not a
    saturation, and refusing it would remove the filter for a legitimate
    caller."""
    f = _probe(tmp_path)
    out = supertool.op_read(str(f), grep_filter="^$|alpha")
    assert not out.startswith("ERROR:"), repr(out)
    assert "alpha" in out and "beta" not in out, repr(out)


# ---------------------------------------------------------------------------
# The durable half: one decision, one place
# ---------------------------------------------------------------------------

def test_every_pattern_route_refuses_the_same_pattern(tmp_path: Path) -> None:
    """The table in #1344, as an assertion. Four routes, one pattern, one
    answer."""
    f = _probe(tmp_path)
    pattern = r"^\|alpha"
    outs = {
        "grep": supertool.op_grep(pattern, str(f), limit=5),
        "grep_around": supertool.op_grep(pattern, str(f), limit=5, context=2),
        "around": supertool.op_around(pattern, str(f), 2),
        "read": supertool.op_read(str(f), grep_filter=pattern),
    }
    disagree = {k: v for k, v in outs.items() if not v.startswith("ERROR:")}
    assert not disagree, (
        "a pattern that saturates saturates on every route; these did not "
        "refuse: " + repr(disagree))


def test_the_refusal_is_reached_through_one_chokepoint() -> None:
    """The half that outlives this fix. `_saturating_pattern_refusal` and
    `_bre_alternation_rewrite` were wired route by route, which is why the
    fourth route arrived with neither. Both now sit behind `_pattern_gate`,
    and a fifth route that does not call the gate is what this pins.

    The rewrite keeps a call inside each private body on purpose — a direct
    call to `_op_grep`/`_op_around` must still run the pattern the caller
    meant, and the helper is idempotent — so only the refusal is counted.
    """
    src = Path(supertool.__file__).with_name("_supertool.py").read_text(
        encoding="utf-8")
    enclosing = None
    callers = []
    for line in src.splitlines():
        m = re.match(r"def (\w+)", line)
        if m:
            enclosing = m.group(1)
        elif "_saturating_pattern_refusal(" in line:
            callers.append(enclosing)
    assert callers == ["_pattern_gate"], (
        "the refusal is reached through one chokepoint so no route can "
        "half-adopt it; called from: " + repr(callers))
