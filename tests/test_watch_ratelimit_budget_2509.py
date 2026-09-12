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


def test_fleet_projected_requests_per_hour_takes_a_calls_per_tick_override() -> None:
    """#2525: a measured `gh-branch` call count (3 fixed + 2 selected runs =
    5, via `gh_branch_calls_per_tick`) overrides the flat `CALLS_PER_TICK`
    entry (3) that this module's own comment already admits undercounts."""
    census = _census(mine={("gh-branch", "1"): [111]})
    flat_total, _ = ratelimit.fleet_projected_requests_per_hour(census)
    measured_total, counts = ratelimit.fleet_projected_requests_per_hour(
        census, calls_per_tick_by_source={"gh-branch": ratelimit.gh_branch_calls_per_tick(2)})
    assert counts == {"gh-branch": 1}
    assert measured_total > flat_total
    # 1 poller x 5 calls/tick x 3600/30 = 600
    assert measured_total == 600.0


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




# ---------------------------------------------------------------------------
# #2525: /rate_limit reporting a full core bucket while gh-backed pollers are
# actively failing with a rate-limit-shaped error -- GitHub's *secondary*
# rate limit (abuse-detection throttling) is not reflected in `/rate_limit`
# or in any response header at all (per GitHub's own REST API rate-limit
# docs), so a healthy-looking core reading here is not evidence that the
# fleet is not being throttled. The two instruments must render side by
# side when they disagree.
# ---------------------------------------------------------------------------

def test_active_gh_rate_limit_errors_keeps_only_gh_sources_with_a_matching_error() -> None:
    states = {
        ("gh-branch", "42"): {"error": "ERROR: GitHub API rate limit exceeded. Wait a few minutes."},
        ("gh-run", "99"): {"error": "ERROR: gh timed out"},
        ("gl-mr", "7"): {"error": "ERROR: GitHub API rate limit exceeded. Wait a few minutes."},
    }
    out = ratelimit.active_gh_rate_limit_errors(states)
    assert out == [("gh-branch", "42",
                     "ERROR: GitHub API rate limit exceeded. Wait a few minutes.")]


def test_active_gh_rate_limit_errors_is_empty_when_nothing_matches() -> None:
    """Must-not-fire twin: a GH_SOURCES poller with a clean or unrelated
    error contributes nothing."""
    states = {
        ("gh-branch", "42"): {"error": ""},
        ("gh-run", "99"): {"error": "ERROR: gh CLI not authenticated. Run: gh auth login"},
    }
    assert ratelimit.active_gh_rate_limit_errors(states) == []


def test_render_budget_lines_flags_a_disagreement_when_core_looks_healthy() -> None:
    """Must-fire half: core shows 4800/5000 free while a gh-backed poller is
    presently failing with a rate-limit-shaped error -- exactly the shape
    #2525 measured five times in nine hours."""
    lines = ratelimit.render_budget_lines(
        _rate_limit(remaining=4800, limit=5000), "", projected=200.0,
        counts={"gh-branch": 1},
        active_errors=[("gh-branch", "42",
                        "ERROR: GitHub API rate limit exceeded. Wait a few minutes.")])
    assert any("DISAGREEMENT" in line for line in lines), lines
    assert any("secondary rate limit" in line for line in lines), lines


def test_render_budget_lines_no_disagreement_when_core_is_actually_exhausted() -> None:
    """Must-not-fire twin, same fixture shape: remaining is 0, so the
    poller's failure is explained by the primary budget itself -- no
    disagreement to report."""
    lines = ratelimit.render_budget_lines(
        _rate_limit(remaining=0, limit=5000), "", projected=200.0,
        counts={"gh-branch": 1},
        active_errors=[("gh-branch", "42",
                        "ERROR: GitHub API rate limit exceeded. Wait a few minutes.")])
    assert not any("DISAGREEMENT" in line for line in lines), lines


def test_render_budget_lines_no_disagreement_when_no_active_errors() -> None:
    """Must-not-fire twin: a healthy core and no active_errors argument at
    all (the default) must never manufacture a disagreement."""
    lines = ratelimit.render_budget_lines(
        _rate_limit(remaining=4800, limit=5000), "", projected=200.0,
        counts={"gh-branch": 1})
    assert not any("DISAGREEMENT" in line for line in lines), lines


# ---------------------------------------------------------------------------
# #2525: gh-branch's real per-tick cost -- the flat "3" in CALLS_PER_TICK
# undercounts by one `_jobs_for` call per run selected on the watched sha,
# and this measures that instead of assuming a fixed number.
# ---------------------------------------------------------------------------

def test_gh_branch_calls_per_tick_adds_one_call_per_selected_workflow() -> None:
    assert ratelimit.gh_branch_calls_per_tick(0) == 3
    assert ratelimit.gh_branch_calls_per_tick(1) == 4
    assert ratelimit.gh_branch_calls_per_tick(3) == 6


def test_gh_branch_calls_per_tick_never_goes_below_the_three_fixed_calls() -> None:
    """Must-not-fire twin: a negative or garbage workflow count must not
    project fewer than the three calls `_snapshot` always makes."""
    assert ratelimit.gh_branch_calls_per_tick(-5) == 3


def test_workflow_file_count_reads_the_watched_repos_own_workflows(tmp_path) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "tests.yml").write_text("name: tests\n")
    (wf_dir / "changelog.yml").write_text("name: changelog\n")
    (wf_dir / "notes.md").write_text("not a workflow\n")
    count, why = ratelimit.workflow_file_count(str(tmp_path))
    assert count == 2, why
    assert why == ""


def test_workflow_file_count_says_why_not_when_the_directory_is_absent(tmp_path) -> None:
    """Third state: no `.github/workflows` at all must not read as zero
    workflows silently -- it is "could not tell", not "confirmed none"."""
    count, why = ratelimit.workflow_file_count(str(tmp_path))
    assert count is None
    assert why
