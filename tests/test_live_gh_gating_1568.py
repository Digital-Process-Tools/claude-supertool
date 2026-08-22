"""A live `gh` call on the per-push critical path (#1568).

`tests/test_watch_radar_gh_prs_859.py::test_live_board_over_this_repo` shells
out to `gh pr list`. On 2026-08-12 it was the single failure of a full run whose
diff touched only `hooks/`, and it passed in isolation seconds later. The red
said nothing about the change; it said the socket was busy.

**The venue and the classifier are two separate fixes and the first pass only
did the second.** That pass argued the test should stay in the default
selection because moving it would cost the coverage it exists for -- a sentence
resting on a premise nobody checked. The test was guarded by a `gh auth status`
probe, which exits non-zero on a runner with no credentials, so it had skipped
on all twelve legs of every CI run since it was written. It was not buying CI
coverage; deleting the probe did not add any, it converted a permanent skip into
a red (PR #1586, four legs, on gh's Actions-only "set the GH_TOKEN environment
variable" message -- which carries no 401, no rate limit and no Go net error, so
no prose predicate could have caught it).

So the test is now `slow`, and in this repo that means it runs on a schedule
rather than nowhere: `.github/workflows/slow-tests.yml` selects that marker
daily and now carries `GH_TOKEN` and `pull-requests: read`, so the live path is
genuinely exercised there -- one leg, once a day, off the path where a rate
limit can red somebody's diff. That is more coverage than it had, not less.

On top of that venue, the classifier keeps it honest wherever it runs:

* the tier separates "I could not reach the API" from "the board says X",
  as a `RadarUnreachable` subclass of `RadarError` rather than as a prose match
  the caller re-derives -- `tests/_lint_budget.py` argues that predicate at
  length and this is the same one;
* `RadarUnconfigured` narrows that to the standing case -- `gh` has no
  credentials, so it refused before asking -- keyed on gh's own exit code 4
  rather than on its message. Measured on gh 2.50.0: both spellings of "no
  credentials" exit 4 and nothing else does, a *rejected* token exits 1 with
  `HTTP 401`, and every product failure exits 1;
* an unreachable API skips **countably**, carrying `_live_gh.TOKEN`, and
  `conftest` prints the count, its denominator and its population every run
  including when it is zero -- breaking out the unconfigured share, because one
  fixes itself and the other does not. A silently-skipped live test is the
  absence-read-as-clean shape this repo files against itself, so the skip is
  not silent.

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

ROOT = REPO = Path(__file__).parent.parent
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


@pytest.mark.parametrize("stderr", [
    # The two spellings `gh` uses for the same fact. The second is the one CI
    # emits, and the one the classifier missed: PR #1586 went red on four legs
    # with it, because it carries no marker any prose predicate would catch --
    # no 401, no rate limit, no Go net error.
    "To get started with GitHub CLI, please run:  gh auth login",
    "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN "
    "environment variable.",
])
def test_gh_refusing_for_want_of_credentials_is_unconfigured(
    monkeypatch, stderr
) -> None:
    """Exit 4 is `gh`'s own auth-configuration code, and nothing else uses it.

    Measured on gh 2.50.0: both spellings of "no credentials" exit 4, a
    *rejected* token exits 1 with `HTTP 401`, and every product failure --
    unknown flag, unknown subcommand -- exits 1. So the predicate is the exit
    code, not the message, which is what `tests/_lint_budget.py` argues for and
    what a prose match here could never have been.
    """
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda *a, **k: _Result(4, "", stderr))
    with pytest.raises(tier.RadarUnconfigured):
        tier.live_open_prs({"state": "open"})


def test_unconfigured_is_a_kind_of_unreachable_not_a_rival_to_it() -> None:
    """Everything that treats `RadarUnreachable` as "not a product verdict"
    must keep doing so without being taught a second name -- the same reason
    `RadarUnreachable` is itself a `RadarError`."""
    assert issubclass(tier.RadarUnconfigured, tier.RadarUnreachable)


def test_a_rejected_token_is_not_unconfigured(monkeypatch) -> None:
    """Credentials were present and the API refused them. That request landed.

    It is still `RadarUnreachable` -- nothing about the board was learned --
    but it is not the standing, configurational absence, and conflating the two
    would make the unconfigured count unreadable.
    """
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda *a, **k: _Result(1, "", "HTTP 401: Bad credentials"))
    with pytest.raises(tier.RadarUnreachable) as caught:
        tier.live_open_prs({"state": "open"})
    assert not isinstance(caught.value, tier.RadarUnconfigured), caught.value


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
    # The returncode, not the mechanism. This assertion used to read
    # `"signal 9" in ...`, which contradicted the docstring directly above it:
    # the predicate IS the sign of the return code, and "signal" is the one
    # part of the old wording #1871 established was never observed on Windows.
    # A test may not pin a claim the code deliberately stopped making.
    #
    # The returncode is true on every platform, and it is also what an
    # operator acts on, so it is the right thing to hold. The *wording* --
    # that the hedge is present and the old unconditional claim is gone, on
    # both tiers, in both directions -- is owned by
    # tests/test_watch_signal_wording_1871.py. Do not re-add a prose
    # assertion here: two files pinning one string is how the next wording
    # change breaks a test that was never about wording.
    assert "-9" in str(caught.value), caught.value


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
        with _live_gh.reachable(tier.RadarUnreachable, tier.RadarUnconfigured):
            raise tier.RadarUnreachable("gh refused the query (rate limit)")
    assert _live_gh.TOKEN in str(caught.value), caught.value
    assert "rate limit" in str(caught.value), caught.value


def test_the_guard_does_not_swallow_a_product_error() -> None:
    """The arm that makes this a gate rather than a tolerance."""
    with pytest.raises(tier.RadarError):
        with _live_gh.reachable(tier.RadarUnreachable, tier.RadarUnconfigured):
            raise tier.RadarError("radar: gh-prs tier cannot honour 'nope'")


def test_the_guard_lets_an_assertion_through() -> None:
    with pytest.raises(AssertionError):
        with _live_gh.reachable(tier.RadarUnreachable, tier.RadarUnconfigured):
            raise AssertionError("the board was wrong")


def test_the_guard_skips_an_unconfigured_runner_with_its_own_reason() -> None:
    """Same skip, different verdict. The reader's action differs: a blip fixes
    itself and an unset token does not."""
    with pytest.raises(pytest.skip.Exception) as caught:
        with _live_gh.reachable(tier.RadarUnreachable, tier.RadarUnconfigured):
            raise tier.RadarUnconfigured("gh has no credentials here")
    text = str(caught.value)
    assert _live_gh.TOKEN in text, text
    assert _live_gh.UNCONFIGURED in text, text


def test_an_unconfigured_skip_is_also_counted_as_a_run_that_missed_the_api(
) -> None:
    """The nesting is deliberate: the total must not lose the unconfigured
    ones, or `N of M did not reach the API` becomes false."""
    assert _live_gh.TOKEN in _live_gh.UNCONFIGURED


def test_the_verdict_line_names_its_denominator() -> None:
    """`N of M`, never a bare `N` (#1274)."""
    line = _live_gh.verdict_line(2, 0, 97)
    assert _live_gh.TOKEN in line
    assert "2 of 97" in line


def test_the_verdict_line_breaks_out_the_unconfigured_share() -> None:
    """One line, two numbers. Sharing a single count would make the second
    unreadable: after a token is set, its expected value is 0 and a non-zero
    one is a finding about the workflow -- but only if it is not summed with a
    transient blip whose expected value is not 0."""
    line = _live_gh.verdict_line(3, 3, 97)
    assert "3 of 97" in line, line
    assert "3 of them" in line or "3 because" in line, line
    assert "will not fix itself" in line, line


def test_the_population_line_says_what_the_count_is_not() -> None:
    assert "tests/test_live_gh_gating_1568.py" in _live_gh.POPULATION


# --- the judgment call, pinned so it cannot be undone by accident ----------


def _live_test_node() -> ast.FunctionDef:
    tree = ast.parse(LIVE_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == LIVE_TEST:
            return node
    raise AssertionError(LIVE_TEST + " is no longer in " + LIVE_MODULE.name)


def test_the_live_test_runs_somewhere() -> None:
    """It must run in CI. WHERE is the decision; "nowhere" is the failure.

    This replaces an earlier assertion that the test carried no marker at all,
    which was written on a premise nobody checked: that being in the default
    selection meant being exercised in CI. It was not. `_gh_ready()` ran
    `gh auth status`, which exits non-zero on a runner with no credentials, so
    the test skipped on all twelve legs of every run since it was written --
    and the old assertion could not see that, because "in the default selection
    and skipping forever" satisfied it exactly.

    So the pin is the stronger property. The test is marked `slow`, and
    `.github/workflows/slow-tests.yml` -- which exists so that a test excluded
    from the per-push legs runs *less often* rather than *nowhere* -- gives its
    job the token that lets it reach the API. Either half missing puts the live
    shapes back to being exercised on nobody's machine but a maintainer's.
    """
    decorators = [ast.unparse(d) for d in _live_test_node().decorator_list]
    assert "pytest.mark.slow" in decorators, (
        LIVE_TEST + " is not marked `slow`, so it sits on the per-push "
        "critical path where a live API call reds somebody's unrelated diff -- "
        "which is the whole of #1568. Decorators: " + repr(decorators))

    workflow = (REPO / ".github" / "workflows" / "slow-tests.yml").read_text(
        encoding="utf-8")
    assert "GH_TOKEN" in workflow, (
        "slow-tests.yml does not set GH_TOKEN, so the one job that selects "
        "this test cannot authenticate and it skips there too -- `slow` would "
        "then mean `nowhere`, which that workflow's own header exists to "
        "refuse.")
    assert "pull-requests: read" in workflow, (
        "slow-tests.yml does not grant `pull-requests: read`, so `gh pr list` "
        "cannot answer with the token it is given")


def test_the_live_test_routes_its_failure_through_the_guard() -> None:
    """A bare `radar_report` call there is the defect back, silently."""
    source = ast.unparse(_live_test_node())
    assert "_live_gh.reachable" in source, (
        LIVE_TEST + " no longer wraps its live call in the reachability "
        "guard, so an unreachable API reds the run again with a verdict "
        "about the network")
