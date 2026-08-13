"""A live `gh` call in the default selection reds on the network (#1568).

`tests/test_watch_radar_gh_prs_859.py::test_live_board_over_this_repo` shells
out to `gh pr list` and runs inside the ~12,000-test xdist selection. On
2026-08-12 it was the single failure of a full run whose diff touched only
`hooks/`, and it passed in isolation seconds later. The red said nothing about
the change; it said the socket was busy.

The route taken is the third of the three the issue lists, and it is the only
one that keeps the coverage on both sides:

* the test stays in the default selection, so the live GitHub shapes it exists
  to exercise are still exercised on every run that *can* reach them;
* the tier now separates "I could not reach the API" from "the board says X",
  as a `RadarUnreachable` subclass of `RadarError` rather than as a prose match
  the caller re-derives -- `tests/_lint_budget.py` argues that predicate at
  length and this is the same one;
* an unreachable API skips **countably**, carrying `_live_gh.TOKEN`, and
  `conftest` prints the count, its denominator and its population every run
  including when it is zero. A silently-skipped live test is the absence-read-
  as-clean shape this repo files against itself, so the skip is not silent.

What is deliberately NOT skipped: any other failure. A malformed argv, a
response that is not a JSON list, a filter the tier refuses -- all of those are
statements about the product and stay red. The gate would be worthless the day
it swallowed one, which is `_lint_budget`'s load-bearing third arm word for
word.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

import _live_gh

ROOT = Path(__file__).parent.parent
WATCH_DIR = ROOT / "presets" / "watch"
LIVE_MODULE = Path(__file__).parent / "test_watch_radar_gh_prs_859.py"
LIVE_TEST = "test_live_board_over_this_repo"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tier = _module("watch_radar_gh_prs_1568", WATCH_DIR / "tiers" / "gh_prs.py")


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_raising(exc: BaseException):
    def run(*a, **k):
        raise exc
    return run


# --- the product half: transport is a state, not a message -----------------


@pytest.mark.parametrize("exc", [
    FileNotFoundError(2, "No such file or directory: 'gh'"),
    subprocess.TimeoutExpired(cmd=["gh"], timeout=30),
    OSError("Connection reset by peer"),
])
def test_a_spawn_that_never_returned_is_unreachable(monkeypatch, exc) -> None:
    """`gh` absent, hung or killed. Nothing was asked of GitHub at all."""
    monkeypatch.setattr(tier.subprocess, "run", _run_raising(exc))
    with pytest.raises(tier.RadarUnreachable):
        tier.live_open_prs({"state": "open"})


@pytest.mark.parametrize("stderr", [
    "gh: Not logged in to any GitHub hosts. Run gh auth login",
    "HTTP 401: Bad credentials",
    "You have exceeded a secondary rate limit",
    "HTTP 403: API rate limit exceeded",
    "error connecting to api.github.com: dial tcp: lookup api.github.com: "
    "no such host",
    'Get "https://api.github.com/graphql": net/http: TLS handshake timeout',
])
def test_a_gh_failure_about_the_transport_is_unreachable(monkeypatch, stderr) -> None:
    """Credentials, throttling and the socket are all "not reached today"."""
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda *a, **k: _Result(1, "", stderr))
    with pytest.raises(tier.RadarUnreachable):
        tier.live_open_prs({"state": "open"})


@pytest.mark.parametrize("stderr", [
    "unknown flag: --frobnicate",
    "could not determine base repository",
])
def test_a_gh_failure_that_is_not_transport_stays_a_plain_radar_error(
    monkeypatch, stderr
) -> None:
    """The safe direction, and the whole reason the classifier is a whitelist.

    An argv this tier built wrongly is a product bug wearing the flake's
    clothes. An unrecognised failure is red, never skipped -- adding a marker
    to this list is a decision somebody makes on evidence.
    """
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda *a, **k: _Result(1, "", stderr))
    with pytest.raises(tier.RadarError) as caught:
        tier.live_open_prs({"state": "open"})
    assert not isinstance(caught.value, tier.RadarUnreachable), caught.value


def test_a_gh_killed_by_a_signal_is_unreachable(monkeypatch) -> None:
    """`subprocess` reports `-N`, and stderr is usually empty.

    Under `-n auto` on a contended runner an OOM kill lands exactly here. The
    arm below it would have called that `gh pr list: unknown error` — the shape
    of a verdict about the board, from a process that was killed before it had
    one. The predicate is the sign of the return code, not a message.
    """
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda *a, **k: _Result(-9, "", ""))
    with pytest.raises(tier.RadarUnreachable) as caught:
        tier.live_open_prs({"state": "open"})
    assert "signal 9" in str(caught.value), caught.value


def test_a_non_zero_exit_with_nothing_to_read_stays_red(monkeypatch) -> None:
    """The safe direction, stated so it is not mistaken for an oversight.

    A positive exit code with no stderr is unclassifiable — a `gh` that died
    without saying why is as likely a broken install as a broken socket — and
    an unclassifiable failure stays a finding.
    """
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda *a, **k: _Result(127, "", ""))
    with pytest.raises(tier.RadarError) as caught:
        tier.live_open_prs({"state": "open"})
    assert not isinstance(caught.value, tier.RadarUnreachable), caught.value


def test_a_reply_that_is_not_a_pr_list_stays_a_plain_radar_error(monkeypatch) -> None:
    """`gh` answered. What it said is a finding about the boundary, not a skip."""
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda *a, **k: _Result(0, '{"not": "a list"}', ""))
    with pytest.raises(tier.RadarError) as caught:
        tier.live_open_prs({"state": "open"})
    assert not isinstance(caught.value, tier.RadarUnreachable), caught.value


def test_the_subclass_still_reaches_every_existing_radar_error_handler() -> None:
    """`radar` catches `RadarError` and exits non-zero; nothing may fall through."""
    assert issubclass(tier.RadarUnreachable, tier.RadarError)


# --- the suite half: the skip is countable ---------------------------------


def test_the_guard_skips_an_unreachable_api_carrying_the_token() -> None:
    with pytest.raises(pytest.skip.Exception) as caught:
        with _live_gh.reachable(tier.RadarUnreachable):
            raise tier.RadarUnreachable("gh refused the query (rate limit)")
    assert _live_gh.TOKEN in str(caught.value), caught.value
    assert "rate limit" in str(caught.value), caught.value


def test_the_guard_does_not_swallow_a_product_error() -> None:
    """The arm that makes this a gate rather than a tolerance."""
    with pytest.raises(tier.RadarError):
        with _live_gh.reachable(tier.RadarUnreachable):
            raise tier.RadarError("radar: gh-prs tier cannot honour 'nope'")


def test_the_guard_lets_an_assertion_through() -> None:
    with pytest.raises(AssertionError):
        with _live_gh.reachable(tier.RadarUnreachable):
            raise AssertionError("the board was wrong")


def test_the_verdict_line_names_its_denominator() -> None:
    """`N of M`, never a bare `N` (#1274)."""
    line = _live_gh.verdict_line(2, 97)
    assert _live_gh.TOKEN in line
    assert "2 of 97" in line


def test_the_population_line_says_what_the_count_is_not() -> None:
    assert "tests/test_live_gh_gating_1568.py" in _live_gh.POPULATION


# --- the judgment call, pinned so it cannot be undone by accident ----------


def _live_test_node() -> ast.FunctionDef:
    tree = ast.parse(LIVE_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == LIVE_TEST:
            return node
    raise AssertionError(LIVE_TEST + " is no longer in " + LIVE_MODULE.name)


def test_the_live_test_is_still_in_the_default_selection() -> None:
    """Route 1 -- mark it out of the default run -- was considered and refused.

    A `slow`/`benchmark` marker or a `skip` decorator here costs the coverage
    the test was written for: the fixtures cannot reach the shapes real GitHub
    produces, which is what section 8 of that module exists to say. If somebody
    decides that trade is right after all, this test is the place to argue it.
    """
    decorators = [ast.unparse(d) for d in _live_test_node().decorator_list]
    assert not decorators, (
        LIVE_TEST + " grew " + repr(decorators) + ". Moving it out of the "
        "default selection buys quiet at the price of the live coverage -- "
        "#1568 chose the third route instead, and the skip is countable so "
        "nobody has to trade one for the other.")


def test_the_live_test_routes_its_failure_through_the_guard() -> None:
    """A bare `radar_report` call there is the defect back, silently."""
    source = ast.unparse(_live_test_node())
    assert "_live_gh.reachable" in source, (
        LIVE_TEST + " no longer wraps its live call in the reachability "
        "guard, so an unreachable API reds the run again with a verdict "
        "about the network")
