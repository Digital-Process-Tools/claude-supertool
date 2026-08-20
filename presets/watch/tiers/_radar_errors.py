"""What kind of failure a radar tier had, as a type a caller can dispatch on.

One copy for every tier, for `_auth_probe.py`'s reason and with the same split.
`_auth_probe` lifted the *predicate* "did the probe establish there is no usable
credential?" and left in each tier what to do about a failure it declines,
because the exit codes and the transport vocabulary are platform-specific. This
module is the other half of that sentence: the three answers are shared, the
predicates that choose between them stay in the tier.

That is why lifting these does not bend one platform's semantics to fit the
other's, which is the objection `gh_prs.py`'s own docstring raises against
generalising the tiers. Nothing here knows what `gh` or `glab` is. "The board
could not be built", "the forge was not reached" and "there is no credential
configured here" are the same three facts on any forge; `gh`'s exit 4 and
`glab`'s stderr are not, and neither appears below.

**Why here and not in `presets/`, where #1846 had to move `_auth_probe.py`.**
That module moved because 23 call sites under `presets/github/` and
`presets/gitlab/` needed the predicate and could not reach a module nested under
`watch/`. These classes have the opposite reach: `RadarError` is radar
vocabulary, the op-level twins raise nothing of the sort, and the only importers
are the tiers themselves and the tests. Moving it up would widen a name that has
no meaning outside radar.

**Reached by `import _radar_errors`, never through a tier's own `_load`, and
that is the whole mechanism rather than a style note.** `_load` calls
`importlib.util.module_from_spec` and execs, which builds a *new module object
every call* and registers nothing. Two tiers loading this file that way get two
unrelated `RadarError` classes with identical names and identical docstrings;
every per-tier test still passes, and a caller holding one tier's class sees an
`except` that never fires against the other's exception -- a silent absence,
which is the shape this repository keeps paying for. A plain `import` off
`sys.path` gets the interpreter's own module cache and therefore one class
object, which is the entire content of this file.

That is also why the tiers reach it the way they reach `_filter_tokens` (a
`sys.path` insert and an `import`) rather than the way they reach `_snapshot.py`
and `mrs.py` (`_load`). The split is not arbitrary: `_load` is fine for a helper
whose *values* are shared and whose identity is not, and wrong for anything
whose classes cross a tier boundary, because class identity is what `except` and
`isinstance` compare. `tests/test_radar_error_classes_1847.py` pins the identity
rather than the names, so a future move back to `_load` goes red instead of
quietly restoring the defect.
"""
from __future__ import annotations


class RadarError(RuntimeError):
    """The board could not be built. Never degrade to 'all green'."""


class RadarUnreachable(RadarError):
    """The board could not be built because the forge was not reached (#1568).

    A subclass, so every existing `except RadarError` -- radar's tier isolation,
    `radar_state`'s filter arm -- keeps behaving exactly as it did. What it adds
    is a state a *caller* can branch on: "the API did not answer" is not the
    same fact as "the board says X", and until #1568 they arrived as one type
    carrying different prose. A loop told the credential is gone stops, where a
    loop told the tier could not answer retries and continues.

    The consumer that needed the distinction is the suite. A live `gh` call in
    the default test selection turned a busy socket into a red leg that read as
    a verdict about the diff (#1568); `tests/_live_gh.py` skips countably on
    this class and re-raises everything else. A caller that matched on the
    message instead would drift the day one of these strings is reworded, which
    is the predicate `tests/_lint_budget.py` argues against at length.

    Deliberately NOT raised for a reply that arrived and was wrong -- an
    unparseable body, a JSON object where a list belongs. The forge answered
    there, and what it said is a finding about the boundary.
    """


class RadarUnconfigured(RadarUnreachable):
    """The CLI has no credentials here, so it refused before asking (#1568).

    A subclass of `RadarUnreachable` rather than a sibling, because every
    consumer that already treats "not reached" as "not a verdict about the
    board" must keep doing so without being taught a second name -- the same
    argument that makes `RadarUnreachable` a `RadarError`.

    It is still a distinct state, and the difference is what the reader should
    do. An unreachable API is transient: the socket blipped, try again, and the
    next run is green. An UNCONFIGURED one is standing -- it will produce the
    same result on every run, forever, until somebody sets a token. Summing the
    two would make the second unreadable in both directions: a permanently
    non-zero count is wallpaper, and once a token IS set, a non-zero count can
    no longer be read as "somebody deleted the env line", which is a real
    finding about the workflow. #1274's argument, one level over.

    **A tier that cannot tell this state from the one above must not raise it.**
    `gh_prs` can: `gh` exit 4 is its auth-configuration code and nothing else
    produces it, measured on gh 2.50.0. `gl_mrs` cannot, and says so where it
    would otherwise guess -- `glab` publishes no equivalent exit code, and
    splitting the two on prose is precisely the bare-`401` collapse
    `_auth_probe.py` exists to prevent. The class is exported from both tiers
    anyway so a caller can write one `except` for the pair; what a tier must not
    do is manufacture the narrower claim.
    """
