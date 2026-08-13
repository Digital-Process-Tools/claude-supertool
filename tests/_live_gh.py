"""One reachability decision for the suite's live GitHub call (#1568).

`tests/test_watch_radar_gh_prs_859.py::test_live_board_over_this_repo` is the
only test in the default selection that reaches the real GitHub API. It exists
because the fixtures in that module cannot produce the shapes real GitHub does,
and it is worth keeping for exactly that reason. What it must not do is convert
a busy socket into a red leg: on 2026-08-12 it was the single failure of a
~12,000-test run whose diff touched only `hooks/`, and it passed in isolation
seconds later. That red was a statement about the network, and a red leg is
triaged by asking what the change broke.

`reachable(RadarUnreachable)` sits around the live call and does one of three
things:

  * **returns**, when the board was built. The caller asserts as before. This
    is the only green, and it is why this is not a tolerance: a test that
    passes when nothing was fetched is worse than a flake.
  * **skips**, carrying ``TOKEN``, when the tier says it could not reach the
    API -- `gh` missing, unauthenticated, rate-limited, or the socket. The live
    path was not exercised on this runner and the suite says so, countably:
    `conftest` prints the count, its denominator and its population every run,
    including when it is zero.
  * **re-raises** everything else. A filter the tier refuses, a reply that is
    not a PR list, a failed assertion about the board -- all statements about
    the product, and all still red.

**The predicate is the product's own type, never a prose match.** `gh_prs.py`
raises `RadarUnreachable` (a `RadarError` subclass) from the arms that mean the
API was not reached, so the class that decides "environment limit" from
"finding" cannot drift out of step with a message somebody reworded. Same
argument as `tests/_lint_budget.py`, which imports `PREFIX` from the product
rather than retyping it.

**The count is a subset and says so (#1274).** Only a call site wrapped in
`reachable` can produce a token skip. `tests/test_live_gh_gating_1568.py` holds
the population and pins the wrapping.
"""
from __future__ import annotations

import contextlib

import pytest

#: Grep handle. Appears in every skip this module produces, so `N skipped` in a
#: CI leg can be resolved to `N did not reach the live GitHub API here`.
TOKEN = "live-gh(#1568)"

#: The standing half. Contains `TOKEN` on purpose, so a skip carrying it is
#: counted in BOTH numbers: the total says how many runs missed the API, and
#: this one says how many of those will still be missing it tomorrow.
UNCONFIGURED = TOKEN + ":unconfigured"


@contextlib.contextmanager
def reachable(unreachable_error: type, unconfigured_error: type):
    """Skip, countably, if the API was not reached. Never tolerate.

    Two verdicts, one skip. Both mean the live path went unexercised and
    neither is a statement about the diff, so both skip rather than fail --
    but a reader does different things with them. A transient unreachable is
    news that means nothing: try again. An UNCONFIGURED runner produces the
    same skip on every run until somebody sets a token, which is a standing
    hole in coverage rather than a blip.

    The classes are passed in rather than imported: the tier is loaded by
    `importlib.util.spec_from_file_location` in the test modules that use it,
    so there is no import path to name here, and a second copy of that loader
    in this file would be a second thing to keep in step. `unconfigured_error`
    is caught first because it is a SUBCLASS of `unreachable_error`, and an
    `except` on the parent would swallow it.
    """
    try:
        yield
    except unconfigured_error as exc:
        pytest.skip(
            "{0}: {1} -- `gh` has no credentials on this runner, so it refused "
            "before making a request. The live path was NOT exercised and will "
            "not be on the next run either: this one does not fix itself. If "
            "this is CI, the job needs `GH_TOKEN` and `pull-requests: read`; "
            "if it is a laptop, `gh auth login`.".format(UNCONFIGURED, exc))
    except unreachable_error as exc:
        pytest.skip(
            "{0}: {1} -- the live GitHub API was NOT reached, so this run "
            "says nothing about the shapes it exercises. Not a finding about "
            "the board and not a finding about the diff. Transient: nothing to "
            "do but run it again.".format(TOKEN, exc))


#: One line for the terminal summary, printed whether the count is zero or not.
POPULATION = (
    "  ^ counts skips carrying that token only, not every test that touches "
    "GitHub: a tier failure that is NOT a transport failure is deliberately "
    "not counted and stays red, and a site held off this runner by an "
    "unrelated marker skips without the token. The live test is `slow`, so on "
    "a default selection this is 0 because it was never selected -- which is "
    "not the same as reaching the API, and `.github/workflows/slow-tests.yml` "
    "is where it is. Full population, derived from the AST: "
    "tests/test_live_gh_gating_1568.py")


def verdict_line(n: int, unconfigured: int, total: int) -> str:
    """``N of M skipped``, never a bare ``N`` (#1274), and the standing share.

    Two numbers on one line rather than two lines or one number. One number
    would make the second unreadable: after a token is set its expected value
    is 0, so a non-zero one is a finding about the workflow -- but only if it
    is not summed with a transient blip whose expected value is not 0.
    """
    line = (
        "{0}: {1} of {2} skipped tests did NOT reach the live GitHub API -- "
        "`gh` absent, unauthenticated, rate-limited or unreachable (expect 0 "
        "where a working `gh` is on PATH; a non-zero count means the live "
        "shapes went unexercised on this runner, not that they are "
        "wrong)".format(TOKEN, n, total))
    if unconfigured:
        line += (
            "; {0} of them because `gh` has no credentials here, which will "
            "not fix itself -- set GH_TOKEN on the job, or `gh auth login`"
            .format(unconfigured))
    return line
