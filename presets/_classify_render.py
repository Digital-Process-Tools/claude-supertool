#!/usr/bin/env python3
"""One verdict line beside the fence banner (#2049, #2056).

`classify` (#2046) is a labeller with a real cost: its model stage is a
`claude -p` spawn, and #2046 itself says almost every ordinary call reaches
it -- the scanner only ever promotes toward `suspect`, never settles `safe`
on its own. This module is the one place that cost is paid on behalf of
every read op that fences untrusted text -- `gh-issue`, `gh-pr`, `gl-issue`,
`gl-mr` today -- so the four call sites agree on one rendering instead of
drifting into four.

**Never edits `presets/classify/`.** That directory is a concurrent lane's
(#2053/#2054/#2055) -- this module only imports `scanner` and `model` from
it, which is a read like any other caller's.

## Three levels, not one switch

A plain on/off leaves "what does off mean" implicit -- does it skip the
spawn only, or refuse to look at all? #2049 shipped with that ambiguity and
it was corrected before this module was final: `classify` is now a
**level**, one of `LEVEL_OFF` / `LEVEL_SCANNER` / `LEVEL_FULL`, read from a
per-op `classify` key in the op's own `presets/*.json` block (`docs/
contributing.md`, "Extra config keys as environment variables" -- a
non-reserved key reaches the subprocess as `SUPERTOOL_CLASSIFY`).

- `off` -- no classification at all. Not even the scanner runs. Zero cost.
- `scanner` -- the deterministic stage only (`scanner.scan`): unsteerable,
  no spawn, catches known credential and fence-forgery shapes. The
  concrete case this exists for: a closed, 2FA-enforced GitLab instance
  where the model stage's threat model does not apply but a scanner that
  costs nothing to keep still might as well run.
- `full` -- both stages, the ordinary case: scanner first, model spawn for
  whatever it did not already decide.

**Shipped default is `full`, uniformly, on every op this module renders
for.** GitHub is the caller #2049 was filed to close: a live, public
tracker already reads bodies from strangers into brief composition with
nothing mechanical between them. GitLab gets the same default rather than a
weaker one, on purpose: "our GitLab is a closed, trusted instance" is a
claim about *one deployment*, not a fact this repository can bake into an
artifact it ships to strangers -- the same failure the maintainer skill's
ranking table calls `ships-local-state`. A deployment that knows its own
GitLab is different says so for itself, per-op, in its own `.supertool.json`
(`docs/contributing.md`: "a project override merges key-by-key"), e.g.
`{"ops": {"gl-issue": {"classify": "scanner"}}}` -- narrowed exactly where
the operator has the standing to narrow it, never assumed here.

**Per-op, not per-forge, and not a group wildcard.** `.supertool.json`'s
`ops` section is already keyed by op name, so a per-op key introduces no new
config shape. A group toggle (all `gl-*` at once) was considered and
rejected: it would silently narrow an op added to this repo later that
nobody has configured, which is the wrong failure direction -- an
unconfigured op must default to `full`, never inherit somebody else's
narrowing meant for the ops that existed when they wrote it. Each op file
below therefore reads its own key and falls back to `LEVEL_FULL` on
anything it does not recognise, including an absent key and a typo'd value
-- failing toward classifying is the same judgment call as the shipped
default, extended to a config value nobody meant to disable anything with.

## `scanner`-clean is its own state, never `safe`

At `LEVEL_SCANNER`, a clean scan is exactly what #2046 says a clean scan
always is: not a verdict. Rendering it as `classify: safe` would be this
repo's own governing defect class -- "no known shape matched" read as "this
text is harmless" -- one layer up, on a switch that exists specifically to
make that distinction visible rather than papering over it. It is also kept
distinct from `could-not-classify`: a level configured not to run the model
stage and a model stage that tried and failed are different facts, and
collapsing them would make "we chose not to" indistinguishable from "it
broke" -- the same defect once removed. So `LEVEL_SCANNER` with nothing
found renders as its own fourth line, `classify: scanner-clean (...)`,
never as either of the other two.

## Budget

`model.DEFAULT_TIMEOUT` is 45s **per unit**, and a rendered issue is many
units -- a body plus one authorship event per comment (#2049's "per unit of
authorship, not per issue"). Left unbounded, `gh-issue:N:full` on a
ten-comment thread has an 11-unit worst case, several minutes of wall
clock, for a single read. `Budget` caps how many units of one call will
actually spawn the model stage; everything past the cap renders
`NOT_RUN_BUDGET` rather than silently doing nothing. This used to be the
whole story -- #2054 shipped a file-backed verdict cache
(`presets/classify/cache.py`) naming this module as its actual target, and
#2097 is that wiring landing.

## Cache (#2097)

`Budget` now owns a cache instance, not a bare spawn count. Two questions
#2097 posed, answered here rather than left open:

- **One instance per call, not a process-wide singleton.** `Budget()` is
  constructed once per op invocation (see `github/issue.py`,
  `github/pr.py`, `gitlab/issue.py`, `gitlab/mr.py` -- each builds exactly
  one `Budget` for the whole call and threads it through every unit). A
  fresh `Cache` per `Budget` already saves within a call, because every
  unit of that call shares the one instance; it also saves across calls,
  because `Cache` is file-backed (`presets/classify/cache.py`'s own
  argument) -- a fresh instance next tick still reads the same file. A
  process-wide singleton would buy nothing beyond that and would keep a
  `Cache` alive (and its directory-verification cost paid) for the whole
  process's lifetime, including callers that never render a single unit.
- **A cache hit must not spend the budget.** Checking the cache only
  inside `verdict_line` (after the budget already decided to decrement)
  would let a run of cached repeats exhaust the cap on units that never
  actually spawn -- the exact waste #2054 exists to remove, reintroduced
  one layer up. `Budget.line` checks the cache itself, before touching
  `self.remaining`, so only a genuine miss ever counts against the cap.
- **The cache's third state (`expired`/`unreadable`, as opposed to
  `miss`) is not surfaced as a fourth rendered state.** `model.classify`
  already collapses `cache.get`'s three non-hit statuses to "spawn as if
  cold" -- correct at that seam, per `cache.py`'s own docstring -- and
  this module's contract with a reader is `could-not-classify` for every
  way the model stage did not produce a verdict. Splitting "no entry"
  from "an entry existed but could not be read" here would read as a
  fifth classify state next to `safe`/`suspect`/`could-not-classify`/
  `scanner-clean`/`off`, which is exactly what this module's own
  governing rule (see "scanner-clean is its own state, never safe"
  above) says not to invent casually. A cache malfunction is a fact
  about this machine, not about the text; it belongs in a log or a
  diagnostic, not folded into the one line a reader uses to decide
  whether to trust a body.
- **`CLASSIFY_BUDGET` is left at 6.** No measurement backs a different
  number yet -- the cap was sized against unbounded spawns on a long
  comment thread, and now that a repeat is usually free the pressure it
  was sized for changes shape (fewer first-time misses per call, not a
  fixed 6 regardless of caching). Changing it needs a measurement this
  module does not have.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_CLASSIFY_DIR = Path(__file__).resolve().parent / "classify"
if str(_CLASSIFY_DIR) not in sys.path:
    sys.path.insert(0, str(_CLASSIFY_DIR))
import scanner  # noqa: E402
import model  # noqa: E402
import cache as classify_cache  # noqa: E402  (#2097 -- aliased so `Budget`'s
# own `cache` parameter never shadows the module it is passed instances of,
# the same convention `presets/classify/check.py` already uses)

LEVEL_OFF = "off"
LEVEL_SCANNER = "scanner"
LEVEL_FULL = "full"
_LEVELS = (LEVEL_OFF, LEVEL_SCANNER, LEVEL_FULL)

#: How many units (a body, a comment) one op call will spawn the model
#: stage for. See the module docstring's "Budget" section.
CLASSIFY_BUDGET = 6

#: Printed once `CLASSIFY_BUDGET` is exhausted for a call -- distinct from
#: every other line so a reader can tell "not classified because the call
#: ran out of budget" from every other reason a line might not say `safe`.
NOT_RUN_BUDGET = "classify: not-run (call budget reached, see #2049)"

# ASCII throughout, on purpose (#863's own argument, one level up):
# `presets/_untrusted.py` routes its own em dashes and ellipses through a
# codepage-detecting `_stream()` before printing them, because those glyphs
# do not encode in cp437/cp850 -- every legacy Windows console codepage --
# and a `UnicodeEncodeError` mid-`print` kills the process after the work
# it was reporting already happened. These two lines are printed directly
# by four call sites with no such routing, so the fix here is simpler:
# never spell a character that needs it.
_OFF_LINE = "classify: off (not configured to classify - see #2049)"
_SCANNER_CLEAN_LINE = (
    "classify: scanner-clean (model stage not configured to run - see #2049)"
)


def level_from_env(var: str = "SUPERTOOL_CLASSIFY",
                    default: str = LEVEL_FULL) -> str:
    """Read the per-op `classify` level. Unset, empty, or anything other
    than the three declared spellings falls back to `default` (`full`) --
    failing toward classifying, never toward silently doing less than the
    caller thinks it configured."""
    raw = os.environ.get(var)
    if raw is None:
        return default
    raw = raw.strip().lower()
    return raw if raw in _LEVELS else default


def _scanner_line(findings) -> str:
    axes = ", ".join(f.axis for f in findings)
    return f"classify: suspect ({axes})"


def verdict_line(text: str, *, level: str = LEVEL_FULL, spawn=None,
                  timeout: int = model.DEFAULT_TIMEOUT, cache=None) -> str:
    """One line for the fence banner, ignoring any call budget -- see
    `Budget` for the stateful wrapper every op call site actually uses.

    `cache` is forwarded verbatim to `model.classify` and defaults to
    `None` (no cache lookup, no cache write) -- the same opt-in-only
    convention `model.classify` itself declares, so a caller reaching this
    function directly (`gitlab/mr.py`'s uncapped test path, `_render_note`
    with `budget=None`) keeps today's behaviour unless it asks otherwise.
    `Budget.line` below is what actually opts real op calls in.

    `could-not-classify` never renders as `safe`, by construction: this
    function only ever reaches `model.classify`'s own three states, a
    scanner hit, or one of this module's own two states (`off`,
    `scanner-clean`) -- never a state that claims more happened than did.
    """
    if not text.strip():
        return "classify: safe (empty)"
    if level == LEVEL_OFF:
        return _OFF_LINE
    findings = scanner.scan(text)
    if findings:
        return _scanner_line(findings)
    if level == LEVEL_SCANNER:
        return _SCANNER_CLEAN_LINE
    spawn_fn = spawn if spawn is not None else model._default_spawn
    v = model.classify(text, spawn=spawn_fn, timeout=timeout, cache=cache)
    return _render_verdict(v)


def _render_verdict(v: "model.Verdict") -> str:
    """`verdict_line`'s tail, factored out so `Budget.line` can render a
    cache hit it has already fetched without a second `model.classify`
    call -- which would mean a second `cache.get` (#2097 review finding:
    checking the cache once in `Budget.line` and then unconditionally
    calling `verdict_line`, which checks it again inside `model.classify`,
    read the same on-disk entry twice on every hit)."""
    if v.state == "safe":
        return "classify: safe"
    if v.state == "suspect":
        return f"classify: suspect ({', '.join(v.axes)})"
    return f"classify: could-not-classify ({v.reason})"


class Budget:
    """Caps how many model-stage spawns one op call will pay for (#2049),
    and (#2097) is the one place a real op call wires in the verdict cache
    #2054 built -- see the module docstring's "Cache" section for why the
    wiring lives here rather than at each of the four call sites.

    Only a `LEVEL_FULL` unit with a clean scan ever consumes it -- `off`
    and `scanner` never spawn, so they never touch the budget regardless
    of how many units a call renders. A cache hit is the third thing that
    never touches it: `line` below checks the cache before it decides
    whether to decrement, so a repeat costs nothing towards the cap even
    though it is a `LEVEL_FULL` unit with a clean scan.
    """

    def __init__(self, n: int = CLASSIFY_BUDGET, cache=None) -> None:
        self.remaining = n
        #: One instance per `Budget`, i.e. per op call -- see the module
        #: docstring's "Cache" section for why that is also enough to
        #: share across calls (the cache is file-backed). `cache=None`
        #: (the default every real call site uses) reaches for the real
        #: on-disk cache; a test passes its own fake or a `Cache` rooted
        #: at `tmp_path` instead.
        self.cache = cache if cache is not None else classify_cache.default_cache()

    def line(self, text: str, *, level: str = LEVEL_FULL, spawn=None,
              timeout: int = model.DEFAULT_TIMEOUT) -> str:
        if level == LEVEL_FULL and text.strip() and not scanner.scan(text):
            cached, _status = self.cache.get(text)
            if cached is not None:
                # Answer from the fetch already made, rather than handing
                # `cache=self.cache` to `verdict_line` and letting
                # `model.classify` repeat the exact same `cache.get` a
                # second time for every hit -- the common case once a call
                # has any repeats at all.
                return _render_verdict(cached)
            if self.remaining <= 0:
                return NOT_RUN_BUDGET
            self.remaining -= 1
        return verdict_line(text, level=level, spawn=spawn, timeout=timeout,
                             cache=self.cache)
