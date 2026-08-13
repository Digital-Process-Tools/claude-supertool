"""A floor under the #1501 skip, so a slow runner cannot green the population.

#1501 converted 42 unit-test call sites to `run_one_or_skip`: when the *core's*
own spawn wall fires, the payload is not a verdict about the file, so the site
declines instead of asserting on a machine limit. That is right, and it is also
a mute switch with no floor under it. A uniformly loaded runner makes every one
of the 42 decline, the run reports `N skipped, 0 failed`, and the suite has said
nothing at all about the validator core while reading as a pass (#1523).

The AST guard in `tests/test_core_timeout_is_not_a_verdict_1501.py` catches the
opposite failure -- a site that never converted. It cannot see this one: every
site is converted and every site is silent.

**Three states, not two.** Like every other counter in this repo
(`docs/validators.md` -- "Declining instead of guessing"), and unlike the two
census lines that preceded it, this one can also decline:

    calls == 0                 NOT CHECKED -- no gated call ran in this selection
    declines <  calls          ok, with the ratio printed
    declines == calls > 0      a finding, and the session exits non-zero

**Why the bar is categorical and not a percentage.** Measured on this repo at
`ee1e248` (macOS, 2026-08-13, `-n0`): the eight files holding the 42 sites ran
`175 passed`, **0 skipped** -- the expected decline count on a healthy leg is
zero, not "a few". A percentage floor over that baseline would be a number
nobody measured, and getting it wrong reds a slow Windows leg for being slow,
which is the exact publication #1501 exists to prevent. `declines == calls` is
not a threshold: it is the statement *this run asserted zero adapter verdicts*,
which is true or false on every platform for the same reason. A leg that
declined 41 of 42 still prints `41 of 42` and is still visible in the log.

**Why the count is per call and not per skip.** The two existing census lines
count skipped *reports* against `len(skipped)` -- every skip in the session,
including ones about symlinks and interpreters. That denominator moves with the
platform. This one counts the calls themselves, so the denominator is exactly
the gated population that was selected and nothing else, and a platform that
skips more for unrelated reasons does not move it.

The counts travel to the controller in `report.user_properties`, which xdist
serialises, so `-n auto` aggregates rather than reporting per worker.

This module is a pytest plugin in its own right (`-p _core_timeout_census` with
`tests/` importable), which is how `tests/test_core_timeout_skip_floor_1523.py`
runs whole sessions against it without needing the repo own conftest.
"""
from __future__ import annotations

from typing import Any

import pytest

#: Grep handle. Appears in every skip `run_one_or_skip` produces, so `N skipped`
#: in a Windows leg resolves to `N declined because the core wall fired`.
TOKEN = "core-timeout(#1501)"

#: `report.user_properties` keys. Ints, because xdist serialises these through
#: JSON and a tuple would come back a list.
CALLS_KEY = "core_timeout_calls"
DECLINES_KEY = "core_timeout_declines"

#: Per-test accumulator, drained onto the report at the end of each test.
_PENDING = {"calls": 0, "declines": 0}

#: Session totals on whichever process runs the terminal summary.
_TOTALS = {"calls": 0, "declines": 0}


def record(declined: bool) -> None:
    """One gated call happened; `declined` says whether it produced a verdict.

    Called from `_adapter_verdict.run_one_or_skip` and nowhere else. Deliberately
    not called from `skip_if_core_timed_out`, which tests also call directly on
    hand-built payloads -- those are asserting *on* the arm, and counting them
    would put fabricated declines into the denominator of a measurement about
    real spawns.

    The same trap one step out, measured while writing #1523: a test that
    monkeypatches `supertool._validator_run_one` into a wall payload and then
    routes it through `run_one_or_skip` records a real decline, because from
    here the two are indistinguishable -- that is the point of the key. A run of
    one such test read `11 passed` and exited 1. A test asserting on the arm
    calls `core_timed_out` or `skip_if_core_timed_out` directly; none of the 42
    production sites fakes the core.
    """
    _PENDING["calls"] += 1
    if declined:
        _PENDING["declines"] += 1


def drain() -> "tuple[int, int]":
    """Take the pending counts and reset. Returns `(calls, declines)`."""
    counts = (_PENDING["calls"], _PENDING["declines"])
    _PENDING["calls"] = 0
    _PENDING["declines"] = 0
    return counts


def reset_totals() -> None:
    _TOTALS["calls"] = 0
    _TOTALS["declines"] = 0


def add_totals(calls: int, declines: int) -> None:
    _TOTALS["calls"] += int(calls)
    _TOTALS["declines"] += int(declines)


def totals() -> "tuple[int, int]":
    return (_TOTALS["calls"], _TOTALS["declines"])


POPULATION = (
    "  ^ counts gated calls, not skipped tests: the denominator is how many "
    "times `run_one_or_skip` actually reached the core in this selection, so a "
    "runner that skips more for unrelated reasons does not move it, and a "
    "direct `skip_if_core_timed_out` on a hand-built payload is deliberately "
    "not counted. Full population, derived from the AST: "
    "tests/test_core_timeout_is_not_a_verdict_1501.py")

NOT_CHECKED = "NOT CHECKED"
FINDING = "FINDING"


def verdict(calls: int, declines: int) -> "tuple[str, bool]":
    """`(line, is_finding)` for the terminal summary.

    Printed whether the count is zero or not: a line that appears only on
    trouble is indistinguishable from one that was never evaluated, which is the
    defect this whole file is about.
    """
    if calls <= 0:
        return (
            "{0}: {1} -- no gated call ran in this selection, so nothing is "
            "claimed about the core spawn wall. This is not a pass.".format(
                TOKEN, NOT_CHECKED),
            False,
        )
    if declines >= calls:
        return (
            "{0}: {1} -- {2} of {3} gated calls declined, so this run asserted "
            "ZERO adapter verdicts and its green says nothing about the "
            "validator core (#1523). Either this runner cannot spawn an "
            "interpreter inside the core wall, or the wall is mis-plumbed. Do "
            "NOT widen the wall to clear this -- that is what #553 and #1360 "
            "already bought.".format(TOKEN, FINDING, declines, calls),
            True,
        )
    return (
        "{0}: {1} of {2} gated calls declined -- {3} adapter verdicts were "
        "actually asserted on (expect 0 declines on a healthy runner; the bar "
        "is that not ALL of them decline)".format(
            TOKEN, declines, calls, calls - declines),
        False,
    )


# ---------------------------------------------------------------------------
# pytest plugin
# ---------------------------------------------------------------------------

def pytest_configure(config: Any) -> None:
    reset_totals()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):
    """Drain the per-test counts onto the report, before xdist serialises it."""
    outcome = yield
    if call.when != "call":
        return
    calls, declines = drain()
    if not calls:
        return
    report = outcome.get_result()
    report.user_properties.append((CALLS_KEY, calls))
    report.user_properties.append((DECLINES_KEY, declines))


def pytest_runtest_logreport(report: Any) -> None:
    calls = declines = 0
    for key, value in getattr(report, "user_properties", ()) or ():
        if key == CALLS_KEY:
            calls = int(value)
        elif key == DECLINES_KEY:
            declines = int(value)
    if calls:
        add_totals(calls, declines)


def _is_worker(config: Any) -> bool:
    """An xdist worker process, whose totals are one slice of the run.

    Its exit status is not the run's, and a worker whose slice happened to hold
    only declined calls would otherwise red itself over a population it never
    saw. The controller receives every worker's report through
    `pytest_runtest_logreport`, so it holds the whole denominator.
    """
    return hasattr(config, "workerinput")


def pytest_terminal_summary(terminalreporter: Any, exitstatus: Any = None,
                            config: Any = None) -> None:
    if _is_worker(getattr(terminalreporter, "config", None)):
        return
    line, _finding = verdict(*totals())
    terminalreporter.write_line(line)
    terminalreporter.write_line(POPULATION)


def pytest_sessionfinish(session: Any, exitstatus: Any = None) -> None:
    if _is_worker(getattr(session, "config", None)):
        return
    _line, finding = verdict(*totals())
    if finding:
        session.exitstatus = 1
