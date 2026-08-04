"""How long a test may wait on a validator adapter subprocess (#702, #658, #650).

Three tests in this suite have now gone red on a loaded Windows runner because
a number written on a fast unloaded laptop stopped holding: `gofmt-check` at
15s (#702), `phplint` twice at 10s (#658), and `git rev-list` at 5s (#650).
Three numbers, three incidents, one pattern — so this file decides the number
once instead of a fourth site guessing it again.

**The rule: an outer budget is a hang-guard on the adapter, so it must exceed
the adapter's own budget.** Every adapter under `validators/` already owns an
internal `subprocess.run(..., timeout=N)` around the real tool and already has
a stated decline for blowing it (`code: "adapter", msg: "timeout"`). So a test
that spawns the adapter is not waiting on the tool — it is waiting on the
adapter, which is contractually obliged to answer within its own N plus the
cost of starting Python twice.

An outer budget *below* that inner N cannot ever fire for the thing a timeout
exists to catch. If the adapter hangs, the adapter's own timeout fires first
and the test gets a JSON decline, not a `TimeoutExpired`. The only way the
outer budget fires is that the machine was slow. That is the repo's own
benchmark-versus-hang-guard test (#504/#505) failing in the benchmark
direction: multiply it by ten and the assertion catches nothing it did not
already catch, so the number *was* the assertion. And a benchmark does not
belong in a correctness test.

Every site was below its adapter's inner budget: phplint 10 < 30,
gofmt-check 15 < 30, cargo-check 120 == 120 (a tie is a race). None of them
was guarding a hang. They were all asserting speed by accident.

**The inner budget is read from the adapter, not copied here.** A table of
"phplint: 30, gofmt-check: 30, …" is a fourth place to write a number down and
a fourth place for it to drift. `inner_budget()` reads the largest `timeout=`
literal out of the adapter's own source, so raising an adapter's internal
budget raises every test budget over it automatically and nobody has to
remember this file exists.

**Windows gets a multiplier, not a special case.** Process spawn on the GH
Windows runners is materially slower than on ubuntu/macos — two Python
interpreter starts plus antivirus interposition on every file touched under
a temp dir — and the runner is shared. That is a property of the platform,
not of any one test, so it scales the whole budget rather than being argued
per site. All three incidents were Windows-only.

**There is no skip-on-budget-blown here, deliberately.** #702 asks whether
these tests should decline rather than fail when the budget blows, per
`docs/validators.md` §"Declining instead of guessing". The answer is no, and
the reason is that the fix above removes the condition that made a skip
tempting. A skip is right for a check that *cannot* tell "the tool hung" from
"the runner was busy" — which is exactly what a 10s outer budget over a 30s
inner budget could not do. Once the outer budget is derived from the inner
one, a blown budget means the adapter did not honour its own timeout, and
that is a hang: a real defect, in the tool the suite exists to test. It must
stay loud. Skipping it is how a genuine hang becomes invisible.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Two Python interpreter starts (the test's spawn of the adapter, and any tool
# the adapter spawns), plus import time, plus reading and writing a temp file.
# Measured in tens of milliseconds on a developer machine; the headroom is set
# where it is because the thing being bought is scheduler jitter on a shared
# runner, not compute.
SPAWN_HEADROOM_S = 10

# Applied to the whole budget on Windows. Chosen from the observed evidence
# rather than from theory: the #658 leg ran 258s against ~115s for comparable
# legs, ~2.2x, and every one of the three incidents was Windows-only. 3 sits
# above the worst multiple actually seen.
WINDOWS_FACTOR = 3

# Used when an adapter declares no internal timeout of its own. Matches the
# value `validators/SCHEMA.md` documents as the framework default for
# formatters, and every adapter that does declare one is at or above it.
DEFAULT_INNER_S = 30

# Same shape and same reasoning as SUPERTOOL_LINT_TIMEOUT (#553) and
# SUPERTOOL_GIT_TIMEOUT (#650): a runner occasionally needs room without a code
# change, and what the suite ships with does not move for it. A positive
# integer wins outright; anything else is ignored rather than crashing the
# collection of every test in the suite (#654).
ENV_OVERRIDE = "SUPERTOOL_TEST_ADAPTER_TIMEOUT"

# Both spellings of "this adapter's own budget": the literal in the
# `subprocess.run` call, and the module constant an adapter hoists it into so
# that its own decline message can name the number (`TIMEOUT_S = 30`). Reading
# only the first would make hoisting a literal — a strictly good change —
# silently drop the outer budget back to the default.
_TIMEOUT_LITERAL_RE = re.compile(r"\btimeout\s*=\s*(\d+)")
_TIMEOUT_CONST_RE = re.compile(r"^[A-Z0-9_]*TIMEOUT[A-Z0-9_]*\s*=\s*(\d+)", re.M)


def platform_factor() -> int:
    """Budget multiplier for the platform this leg is running on."""
    return WINDOWS_FACTOR if sys.platform.startswith("win") else 1


def inner_budget(adapter: str | Path) -> int:
    """The largest internal subprocess budget the adapter grants itself.

    Read from source rather than tabulated, so the outer budget tracks the
    inner one without a human keeping two numbers in step. An adapter that is
    unreadable or declares none falls back to ``DEFAULT_INNER_S`` — biased
    towards a budget that is too generous, because a budget that is too tight
    is the defect this module exists to remove.
    """
    try:
        src = Path(adapter).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return DEFAULT_INNER_S
    found = [int(n) for n in _TIMEOUT_LITERAL_RE.findall(src)]
    found += [int(n) for n in _TIMEOUT_CONST_RE.findall(src)]
    return max(found) if found else DEFAULT_INNER_S


def _override() -> int | None:
    try:
        val = int(os.environ.get(ENV_OVERRIDE, ""))
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def adapter_budget(adapter: str | Path, inner: int | None = None) -> int:
    """Seconds a test may wait on ``adapter`` before calling it a hang.

    Strictly greater than the adapter's own internal budget, so a fired
    budget means the adapter failed to honour its own timeout — the one thing
    a test out here can learn that the adapter cannot report on itself.

    ``inner`` overrides what is read from source, for the rare caller that
    knows the tool's real cost is elsewhere (a `cargo check` that compiles a
    crate, say). It is still a floor to build on, never the budget itself.
    """
    forced = _override()
    if forced is not None:
        return forced
    base = inner_budget(adapter) if inner is None else inner
    return (base + SPAWN_HEADROOM_S) * platform_factor()
