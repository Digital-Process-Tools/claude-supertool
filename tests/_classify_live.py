"""One reachability decision for the suite's live `classify` model tests
(#2073).

`tests/test_classify_live_2046.py` holds the only controls that prove the
`classify` spawn actually denies tools and actually isolates hooks and
CLAUDE.md, against the real `claude` binary. Its own docstring used to argue
against building this module -- "one live test, not a whole tier", and
"`claude` is expected to be on PATH and authenticated in this environment
already". Both premises are now false: it is four tests, and nothing
installs or authenticates `claude` on any runner this repo schedules --
`.github/workflows/slow-tests.yml` selects `slow` daily and has no step that
puts the binary on PATH. So every one of those four skips on every CI run,
and until this module existed that skip was a hand-rolled `pytest.skip()`
with no token: the terminal census enumerated five known skip reasons and
reported zero against every one of them while these four went unaccounted
for (#2073).

Same shape as `tests/_live_gh.py` and `tests/_git_decline.py`. `require_claude`
sits in front of the one thing that can decline and does one of two things:

  * **returns**, when `claude` is on PATH. The caller runs as before -- the
    only green, because a call that always "passed" without ever reaching the
    binary would be worse than a red.
  * **skips**, carrying `TOKEN`, when it is not. `conftest` prints the count,
    its denominator and its population every run, zero included.

**What "expected" means for this tier, decided here rather than left to the
reader.** Unlike `_live_gh`'s `UNCONFIGURED`, there is no second, transient
failure mode to break out -- the only thing this gate can observe is whether
the binary is on PATH, and that is either true for the whole run or false for
it, uniformly across all four tests (they all call this same function). So the
expected count is one of exactly two values: 0, on a machine with `claude`
installed and authenticated (a maintainer's laptop, this very session), or the
full population, on every runner this schedule reaches today, because nothing
installs the binary there. Neither value is a finding; `verdict_line` says so
rather than defaulting to "expect 0" the way the git/GitHub gates do, because
for THIS tier a non-zero count is the norm, not an anomaly, until a future
change decides to install `claude` on the scheduled runner (the issue's own
second, separable question -- not answered here).

A run that skips some but not all four would be the one worth a second look:
it would mean `shutil.which` disagreed with itself mid-session, which nothing
in this codebase does deliberately. That is not asserted here as a gate --
there is no product boundary to raise past, only a count to report -- but it
is worth naming as the shape that would actually be news.

**The count is a subset and says so** (#1274). Only a call routed through
`require_claude` can produce a token skip; `test_classify_live_census_2073.py`
pins that all four call sites in `test_classify_live_2046.py` do.
"""
from __future__ import annotations

import shutil

import pytest

#: Grep handle. Appears in every skip this module produces, so `N skipped` in
#: a CI log can be resolved to `N did not reach the real claude binary here`.
TOKEN = "classify-live(#2073)"


def require_claude() -> None:
    """Skip, countably, when `claude` is not on PATH. Never tolerate."""
    if shutil.which("claude") is not None:
        return
    pytest.skip(
        "{0}: `claude` is not on PATH in this environment, so the real "
        "spawn was NOT exercised -- the tool-denial and hook/CLAUDE.md "
        "isolation controls in this file only prove anything where this "
        "binary runs. Standing on every scheduled CI runner today: nothing "
        "installs or authenticates `claude` there (see #2073). Not a "
        "finding about the diff; install and authenticate `claude` to "
        "exercise this locally, or on a future runner built for it."
        .format(TOKEN))


#: One line for the terminal summary, printed whether the count is zero or
#: not, and phrased for a tier whose expected value is not always 0 (see the
#: module docstring).
POPULATION = (
    "  ^ counts skips carrying that token only. Full population, derived "
    "from the AST: tests/test_classify_live_2046.py -- exactly 4 tests, "
    "each gated through this module's require_claude(), pinned by "
    "tests/test_classify_live_census_2073.py so a hand-rolled skip at any "
    "of the four cannot fall out of this count unnoticed.")


def verdict_line(n: int, total: int) -> str:
    """``N of M skipped``, never a bare ``N`` (#1274).

    No "expect 0" hedge, unlike the git/GitHub gates: for this tier 0 and
    the full population (4) are BOTH the normal state, depending on whether
    the runner has `claude` installed, and this module cannot tell a
    maintainer's laptop from a CI runner. A count strictly between 0 and
    the population would be the one worth a second look, and the line says
    so rather than implying either extreme is the anomaly.
    """
    return (
        "{0}: {1} of {2} skipped tests did NOT reach the real `claude` "
        "binary (expect 0 where `claude` is on PATH and authenticated, or "
        "the full population where it is not -- both are the normal state "
        "for this tier today; anything strictly in between would mean "
        "`claude`'s presence disagreed with itself mid-run, which is worth "
        "investigating)".format(TOKEN, n, total))
