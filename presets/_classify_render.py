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
`NOT_RUN_BUDGET` rather than silently doing nothing. This is a stated,
real cost, not a solved one -- #2054 (a verdict cache, concurrent lane) is
the fix that removes the need for a budget at all.
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
                  timeout: int = model.DEFAULT_TIMEOUT) -> str:
    """One line for the fence banner, ignoring any call budget -- see
    `Budget` for the stateful wrapper every op call site actually uses.

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
    v = model.classify(text, spawn=spawn_fn, timeout=timeout)
    if v.state == "safe":
        return "classify: safe"
    if v.state == "suspect":
        return f"classify: suspect ({', '.join(v.axes)})"
    return f"classify: could-not-classify ({v.reason})"


class Budget:
    """Caps how many model-stage spawns one op call will pay for (#2049).

    Only a `LEVEL_FULL` unit with a clean scan ever consumes it -- `off`
    and `scanner` never spawn, so they never touch the budget regardless
    of how many units a call renders.
    """

    def __init__(self, n: int = CLASSIFY_BUDGET) -> None:
        self.remaining = n

    def line(self, text: str, *, level: str = LEVEL_FULL, spawn=None,
              timeout: int = model.DEFAULT_TIMEOUT) -> str:
        if level == LEVEL_FULL and text.strip() and not scanner.scan(text):
            if self.remaining <= 0:
                return NOT_RUN_BUDGET
            self.remaining -= 1
        return verdict_line(text, level=level, spawn=spawn, timeout=timeout)
