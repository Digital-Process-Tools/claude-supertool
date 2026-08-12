"""One lint-budget decision for the whole suite, read off the receipt (#1360).

A post-edit lint that shells out has a wall-clock budget
(``SUPERTOOL_LINT_TIMEOUT``, 30s in this suite). On a loaded GitHub Windows
runner it gets blown -- twice now: #553 at 5s, #1360 at 30s. Each time the
product behaved correctly, declined, and named the knob; each time the suite
converted that decline into a red leg, which is this repo's own defect class
relocated into the thing meant to detect it.

Raising the number a third time is the move #1360 argues against: it buys an
interval of quiet against an unbounded tail (antivirus scanning a freshly
written temp file, a contended runner), and nobody has measured what a cold
``xmllint`` costs there. So the fix is the third state instead.

``require_lint_verdict(out)`` sits in front of a verdict assertion and does one
of three things:

  * **returns**, when the receipt carries a verdict. The caller asserts as
    before. This is the only green, and it is why this is not a tolerance: a
    test that passes when the checker did nothing is worse than a flake.
  * **skips**, carrying ``TOKEN``, when the receipt says the checker timed out.
    The verdict path was not exercised on this runner and the suite says so,
    countably -- ``conftest`` prints the count, its denominator and its
    population every run, including when it is zero.
  * **fails**, when the receipt declined for any *other* reason, or timed out
    against a budget that is not the configured one.

That last arm is the load-bearing one. ``POST-EDIT LINT DECLINED -- could not
start the checker (FileNotFoundError)`` is the #997 class -- a spawn failure
that only happens on Windows -- and a gate that swallowed it would report an
absence of coverage exactly where the tool had found a bug. And a *timeout*
naming a budget nobody configured is the knob mis-plumbed, i.e. a product bug
wearing the flake's clothes; it is distinguishable without guessing, because the
receipt states the budget it used and ``_lint_timeout()`` states the budget that
was configured.

``PREFIX`` is imported from the product, never retyped. The predicate that
decides between "environment limit" and "finding" must not be a prose match on
a message somebody can reword.

**The count is a subset and says so (#1274).** Only a site that calls this
helper can produce a token skip. A lint-verdict site held off a runner by an
unrelated marker skips without one, and a non-timeout decline does not skip at
all. ``tests/test_lint_budget_gating_1360.py`` derives the full population from
the AST and asserts every member is gated.
"""
from __future__ import annotations

import re

import pytest

import supertool

#: Grep handle. Appears in every skip this module produces, so `N skipped` in a
#: Windows leg can be resolved to `N did not reach a lint verdict here`.
TOKEN = "lint-budget(#1360)"

#: The product's own declaration of the one skippable decline.
PREFIX = supertool._LINT_TIMEOUT_PREFIX

#: `... — xmllint (30s) ---`. The budget the product says it used.
_BUDGET = re.compile(r"\((\d+)s\)")


def timeout_decline(out: str) -> str:
    """The timeout header line in ``out``, or ``""``.

    Line-wise rather than ``startswith``: the lint section sits inside a longer
    ``op_vim`` receipt, after the cursor line and the diff.
    """
    for line in out.splitlines():
        if line.startswith(PREFIX):
            return line
    return ""


def require_lint_verdict(out: str) -> None:
    """Skip, countably, if the checker declined on the budget. Never tolerate."""
    header = timeout_decline(out)
    if not header:
        return
    configured = supertool._lint_timeout()
    found = _BUDGET.search(header)
    if not found:
        pytest.fail(
            "the lint timed out and the receipt does not state the budget it "
            "used, so this cannot be told apart from the knob being "
            "mis-plumbed (#1360): " + header)
    stated = int(found.group(1))
    if stated != configured:
        pytest.fail(
            "the lint timed out against a {0}s budget while "
            "SUPERTOOL_LINT_TIMEOUT resolves to {1}s. That is the knob not "
            "reaching the subprocess -- a product bug, not a slow runner, so "
            "it stays red (#1360): {2}".format(stated, configured, header))
    pytest.skip(
        "{0}: {1} -- the checker was there and the {2}s budget ran out, so the "
        "verdict path was NOT exercised on this runner. The decline itself is "
        "pinned by test_vim_receipt_reports_a_lint_decline_not_a_verdict; do "
        "NOT raise SUPERTOOL_LINT_TIMEOUT, that is what #553 and #1360 both "
        "already bought.".format(TOKEN, header.strip(), configured))


#: One line for the terminal summary, printed whether the count is zero or not.
POPULATION = (
    "  ^ counts skips carrying that token only, not every test that needs a "
    "lint verdict: a checker that DECLINED for any reason other than the "
    "budget (could not start, no interpreter) is deliberately not counted and "
    "stays red, and a site held off this runner by an unrelated marker skips "
    "without the token. Full population, derived from the AST: "
    "tests/test_lint_budget_gating_1360.py")


def verdict_line(n: int, total: int) -> str:
    """``N of M skipped``, never a bare ``N`` (#1274)."""
    return (
        "{0}: {1} of {2} skipped tests did NOT reach a lint verdict -- the "
        "checker timed out on the {3}s budget (expect 0; a non-zero count is "
        "a runner too slow to exercise the verdict path, not a finding)".format(
            TOKEN, n, total, supertool._lint_timeout()))
