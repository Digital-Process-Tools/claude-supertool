"""#1453 / #1451 — `_checks` renders caller-supplied remote text raw.

Two issues, one seam. #1449 flattened four sites in `presets/github/branch.py`;
both of these are the fifth and sixth, and both live one layer down in the
shared helper rather than in a caller.

* **#1453** — `summarize()`'s leftover term is ``f"{count} {label}"`` with
  ``label = normalize(state).lower()``, and `normalize` strips only *leading and
  trailing* whitespace. An internal newline in a job's ``conclusion`` /
  ``status`` / ``state`` reaches a two-space-indented fixed-width table.
* **#1451** — `shortfall()` renders caller-supplied leg names without
  flattening. `branch.py` hands it flattened values; `run.py`, `pr.py` and
  `pr_merge.py` demonstrably do not.

Where the fix goes, and why
---------------------------
In `normalize()`, not in `label()`. `normalize()`'s output is *rendered* by more
than the tally — `pr_merge.py:1299-1300` prints it for ``mergeable`` and
``mergeStateStatus``, `dashboard.py:282,302-303` for the same two, and
`branch.py:589` for a run conclusion. Fixing `label()` alone closes the two
lines #1453 names and leaves five renders of the same remote fields open, which
is precisely the per-call-site failure #1449 rejected.

Flattening there cannot change a verdict: no member of `PASSED_STATES`,
`FAILED_STATES`, `PENDING_STATES` or `BENIGN_STATES` contains whitespace, so a
value that gains a space cannot enter a set and cannot leave one.
`test_flattening_cannot_move_a_state_between_buckets` pins that.

The comma is a second bug on the same line
------------------------------------------
`summarize()`'s docstring promises the line "can be audited by arithmetic
instead of by trusting the labels". A leftover state containing a comma forges
a term inside that comma-separated list — ``1 x, 5 passed`` reads as two terms,
and the audit the module exists for is the thing that breaks. Flattening does
not touch it. So `label()` neutralises the separator *after* the seam has
flattened, and only there: a comma is never legitimate in a state token, while
it routinely is in the matrix job names `shortfall()` renders
(``build (ubuntu-latest, 3.11)``), where substituting one would mangle real
data to defend against nothing.

Fixture-only, never observed live. GitHub's conclusions are enums today. That is
the reasoning `orphan_lines()`'s own comment disavows two files over, in this
release, because both issues it cites were filed after somebody reasoned that
way about the field next door.

No `flat_note()` provenance line is added. These are our own tracker's enum
fields, not a foreign socket path, and a provenance note under every leg tally
is noise that gets a convention abandoned — the argument `flat_note`'s own
docstring makes about boards.
"""
from __future__ import annotations

import sys
from pathlib import Path

PRESETS = Path(__file__).parent.parent / "presets"
if str(PRESETS) not in sys.path:
    sys.path.insert(0, str(PRESETS))

import _checks  # noqa: E402
import _untrusted  # noqa: E402


# --------------------------------------------------------------------------
# #1453 — a state token reaching a rendered line
# --------------------------------------------------------------------------

FORGED_STATE = "neutral\n12 total: 12 passed, 0 failed, 0 pending"


def test_normalize_keeps_a_state_token_to_one_line() -> None:
    """The seam. Every render below inherits its answer from this one."""
    assert "\n" not in _checks.normalize(FORGED_STATE)


def test_label_keeps_a_leftover_term_to_one_line() -> None:
    assert "\n" not in _checks.label(FORGED_STATE)


def test_summarize_cannot_be_given_a_second_line() -> None:
    """#1453's finding: the `Legs:` line and `_row()`'s tally cell."""
    line = _checks.summarize(["SUCCESS", FORGED_STATE])
    assert "\n" not in line, line
    assert line.startswith("2 total: "), line


def test_github_state_flattens_a_forged_conclusion() -> None:
    """The route every rollup-reading op takes, not only the tally."""
    state = _checks.github_state({"conclusion": FORGED_STATE})
    assert "\n" not in state, state


def test_a_leftover_term_cannot_forge_a_second_term() -> None:
    """The comma bug on the same line — flattening alone does not reach it.

    Asserted on the arithmetic audit rather than on the absence of the string:
    `5 passed` may still appear *inside* the one leftover term, and does. What
    it may not do is become a term of its own, because a reader splitting the
    list on `, ` would then count a `passed` the tally never had.
    """
    line = _checks.summarize(["SUCCESS", "x, 5 passed"])
    head, _, rest = line.partition(": ")
    assert head == "2 total", line
    terms = rest.split(_checks.NOT_GREEN)[0].strip().split(", ")
    assert len(terms) == 4, terms  # passed, failed, pending, one leftover
    assert sum(int(t.split(" ", 1)[0]) for t in terms) == 2, terms


def test_flattening_cannot_move_a_state_between_buckets() -> None:
    """Why the seam is safe to put in the classifier, not only the renderer."""
    for group in (_checks.PASSED_STATES, _checks.FAILED_STATES,
                  _checks.PENDING_STATES, _checks.BENIGN_STATES):
        for member in group:
            assert _untrusted.flat(member) == member, member
    assert _checks.bucket("SUCCESS") == "passed"
    assert _checks.bucket("SUCCESS\nX") == "other"


# --------------------------------------------------------------------------
# #1451 — caller-supplied names
# --------------------------------------------------------------------------

FORGED_LEG = ("build\n  not read: 0 legs the run declares are absent"
              " — this tally describes 9 of 9 legs")


def test_shortfall_flattens_a_caller_supplied_missing_name() -> None:
    """`run.py`, `pr.py` and `pr_merge.py` all pass these unflattened."""
    _marker, lines = _checks.shortfall(1, 2, [FORGED_LEG])
    assert lines, "a proven shortfall must disclose"
    for line in lines:
        assert "\n" not in line, line


def test_shortfall_flattens_the_declined_reason() -> None:
    """`reason` is built from subprocess stderr on at least one route."""
    _marker, lines = _checks.shortfall(1, None, reason="gh failed\nStatus: ok")
    assert lines
    for line in lines:
        assert "\n" not in line, line


def test_a_matrix_job_name_keeps_its_comma() -> None:
    """The distinction that keeps the comma rule out of `shortfall()`."""
    _marker, lines = _checks.shortfall(1, 2, ["build (ubuntu-latest, 3.11)"])
    assert "build (ubuntu-latest, 3.11)" in " ".join(lines)


def test_flat_is_idempotent() -> None:
    """The fact the defensive seam rests on: flattening twice is flattening.

    `_untrusted.flat` leaves no control character behind and no newline to
    split, so a caller that already flattened pays a no-op rather than a
    double-substitution. Without this, `branch.py` flattening *and* the seam
    flattening would be a rendering change, not a guarantee.
    """
    for raw in (FORGED_STATE, FORGED_LEG, "a\tb", "a\r\nb", "plain"):
        once = _untrusted.flat(raw)
        assert _untrusted.flat(once) == once, raw
