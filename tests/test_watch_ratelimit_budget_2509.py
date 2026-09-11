"""Fleet-wide GitHub API budget arithmetic and rendering (#2509).

Two things the issue asks `watches`/`channel:health` to print: the projected
requests/hour a fleet of gh-backed pollers commits a shared token to, and a
WARN when that projection exceeds the real budget `gh api rate_limit`
reports. Both are pure functions here — no `gh` spawned — so the arithmetic
and the WARN's own on/off boundary are each pinned without a live token.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE = Path(__file__).parent.parent / "presets" / "watch" / "ratelimit.py"
_spec = importlib.util.spec_from_file_location("watch_ratelimit", MODULE)
assert _spec is not None and _spec.loader is not None
ratelimit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ratelimit)


# ---------------------------------------------------------------------------
# the issue's own formula: pollers x calls-per-tick x 3600/interval
# ---------------------------------------------------------------------------

def test_projected_requests_matches_the_issues_own_arithmetic() -> None:
    """18 pollers x 2 calls/tick x 120 ticks/hour (30s interval) = 4320 req/h,
    inside the issue's own 4300-6500 observed range."""
    projected = ratelimit.projected_requests_per_hour(
        pollers=18, calls_per_tick=2, interval=30)
    assert projected == 18 * 2 * 120
    assert 4300 <= projected <= 6500


def test_a_single_poller_at_30s_and_one_call_projects_120_per_hour() -> None:
    assert ratelimit.projected_requests_per_hour(
        pollers=1, calls_per_tick=1, interval=30) == 120.0


def test_zero_or_negative_interval_projects_zero_not_infinity() -> None:
    """`3600 / 0` would raise; "no interval recorded" must read as "nothing
    to project", never as an infinite rate."""
    assert ratelimit.projected_requests_per_hour(
        pollers=5, calls_per_tick=2, interval=0) == 0.0
    assert ratelimit.projected_requests_per_hour(
        pollers=5, calls_per_tick=2, interval=-30) == 0.0


def test_zero_pollers_projects_zero() -> None:
    assert ratelimit.projected_requests_per_hour(
        pollers=0, calls_per_tick=3, interval=30) == 0.0


# ---------------------------------------------------------------------------
# fleet_gh_poller_counts / fleet_projected_requests_per_hour: cross-channel
# ---------------------------------------------------------------------------

def _census(mine=None, other=None, unknown=None):
    return {"mine": mine or {}, "other": other or {}, "unknown": unknown or {},
            "scan_ok": True}


def test_fleet_counts_sum_across_mine_other_and_unknown_channels() -> None:
    census = _census(
        mine={("gh-branch", "main"): [111]},
        other={"channelA": {("github-pr", "1"): [222, 223]}},
        unknown={("gh-run", "9"): [333]},
    )
    counts = ratelimit.fleet_gh_poller_counts(census)
    assert counts == {"gh-branch": 1, "github-pr": 2, "gh-run": 1}


def test_fleet_counts_ignore_non_gh_sources() -> None:
    """A GitLab, Slack or Bluesky poller draws on a different budget, or
    none — it must not inflate the GitHub projection."""
    census = _census(mine={("gitlab-mr", "5"): [111], ("slack", "x"): [222]})
    assert ratelimit.fleet_gh_poller_counts(census) == {}


def test_fleet_projected_requests_per_hour_uses_default_interval() -> None:
    census = _census(mine={("github-pr", "1"): [111]})
    total, counts = ratelimit.fleet_projected_requests_per_hour(census)
    assert counts == {"github-pr": 1}
    # github-pr: 1 poller x 1 call/tick x 3600/30 = 120
    assert total == 120.0


# ---------------------------------------------------------------------------
# render_budget_lines: the WARN is a positive control, paired must-fire /
# must-not-fire in the same fixture
# ---------------------------------------------------------------------------

def _rate_limit(remaining=100, limit=5000, reset_iso="2026-09-11T17:00:00Z"):
    return {"core": {"remaining": remaining, "limit": limit, "reset_iso": reset_iso}}


def test_warn_fires_when_projected_exceeds_the_core_limit() -> None:
    """Must-fire half: projected (6000) > limit (5000)."""
    lines = ratelimit.render_budget_lines(
        _rate_limit(limit=5000), "", projected=6000.0, counts={"gh-branch": 18})
    assert any("WARN" in line for line in lines), lines


def test_warn_does_not_fire_when_projected_is_within_the_core_limit() -> None:
    """Must-not-fire half, same fixture shape: projected (2000) < limit
    (5000). A broken harness that always prints WARN must fail this."""
    lines = ratelimit.render_budget_lines(
        _rate_limit(limit=5000), "", projected=2000.0, counts={"gh-branch": 5})
    assert not any("WARN" in line for line in lines), lines


def test_a_failed_rate_limit_read_is_disclosed_not_silently_dropped() -> None:
    """The third state: `gh api rate_limit` itself could not be read. This
    must say so, never render as though the projection had been checked."""
    lines = ratelimit.render_budget_lines(
        None, "gh not found", projected=1000.0, counts={"gh-branch": 3})
    assert any("could not be read" in line for line in lines), lines
    assert not any("WARN" in line for line in lines), lines


def test_no_gh_pollers_seen_says_so_rather_than_printing_zero_silently() -> None:
    lines = ratelimit.render_budget_lines(_rate_limit(), "", projected=0.0, counts={})
    assert any("no gh-backed pollers" in line for line in lines), lines


# ---------------------------------------------------------------------------
# is_rate_limit_error: the marker check every poller's back-off gates on
# ---------------------------------------------------------------------------

def test_is_rate_limit_error_matches_the_shared_error_sentence() -> None:
    assert ratelimit.is_rate_limit_error(
        "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry.")
    assert ratelimit.is_rate_limit_error(
        "ERROR: GitHub API rate limit exceeded. Wait a few minutes.")


def test_is_rate_limit_error_is_false_for_an_unrelated_failure() -> None:
    """Must-not-fire twin: a timeout or an auth failure is not a rate limit,
    and must not be mistaken for one."""
    assert not ratelimit.is_rate_limit_error("ERROR: gh timed out")
    assert not ratelimit.is_rate_limit_error(
        "ERROR: gh CLI not authenticated. Run: gh auth login")
    assert not ratelimit.is_rate_limit_error("")


def test_reset_iso_formats_a_real_epoch_and_rejects_garbage() -> None:
    assert ratelimit.reset_iso(1600000000) == "2020-09-13T12:26:40Z"
    assert ratelimit.reset_iso("not-a-number") == ""
    assert ratelimit.reset_iso(None) == ""

