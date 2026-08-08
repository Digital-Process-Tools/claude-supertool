"""`dashboard` — the join, and the two inferences that must never guess (#953).

`dashboard` answers "what do I do next" by joining four reads a maintainer was doing
by hand. Two of its columns are assertions a human acts on immediately, and both
fail silently when they get it wrong:

* **the verdict.** `MERGE` is acted on without a second look. It is derived from
  the #454 arithmetic, so a tally that does not sum to the legs the run declares
  is `UNKNOWN` — never `WAITING`, and certainly never `MERGE`. `CANCELLED`,
  `SKIPPED`, `TIMED_OUT`, `NEUTRAL` and `ACTION_REQUIRED` are none of them
  passes.
* **lane occupancy.** A lane reported free while an agent works in it is how two
  agents end up editing one file. Worktree liveness is already three-state
  (`git-worktrees` declines with `cannot tell`), so a lane whose only signal is
  an undecidable worktree is `unknown`, not `free`.

The third pin is this repo's most-filed defect: a section that could not be
fetched says so and is not omitted, because a dashboard missing its board reads
exactly like a dashboard with an empty board.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _preset_loader import load_preset_module  # noqa: E402

dashboard = load_preset_module("dashboard", "dashboard", prefix="dashboard_")


def _pr(number=951, branch="fix/1", states=("SUCCESS",), marker="",
        mergeable="MERGEABLE", merge_state="CLEAN", draft=False):
    return dashboard.PullRequest(
        number=number, branch=branch, title="t",
        states=list(states) if states is not None else None,
        tally_marker=marker, mergeable=mergeable, merge_state=merge_state,
        draft=draft, lanes=[], red_ref=None,
    )


# ── the verdict ──────────────────────────────────────────────────────────

def test_all_green_and_mergeable_is_the_only_route_to_merge():
    word, why = dashboard.pr_verdict(_pr())
    assert word == dashboard.MERGE, why


@pytest.mark.parametrize("marker", [
    "⚠ INCOMPLETE — 18 of 20 legs read",
    "⚠ TALLY UNVERIFIED",
])
def test_a_tally_that_does_not_sum_is_unknown_never_merge(marker):
    """The whole risk in one assertion.

    Every leg that *was* read passed and GitHub says the PR is mergeable — the
    exact input that produces `MERGE` when the second leg count is ignored. A
    non-summing tally has to override all of it.
    """
    word, why = dashboard.pr_verdict(_pr(states=["SUCCESS"] * 18, marker=marker))
    assert word == dashboard.UNKNOWN, why
    assert word != dashboard.MERGE
    assert word != dashboard.WAITING
    assert "sum" in why or "unverified" in why.lower() or "legs" in why


@pytest.mark.parametrize("state", [
    "CANCELLED", "SKIPPED", "TIMED_OUT", "NEUTRAL", "ACTION_REQUIRED",
])
def test_no_non_pass_state_is_ever_counted_as_a_pass(state):
    """None of the five is a pass. Which non-pass they are is `_checks`' call.

    `TIMED_OUT` and `ACTION_REQUIRED` are in `FAILED_STATES` there and belong
    nowhere near `SKIPPED` — a job that ran out of wall clock produced no
    verdict and one waiting on a human is blocking — so those two land on `RED`
    while the rest land on `UNKNOWN`. Both are "not a pass"; only `MERGE` is
    forbidden to all five.
    """
    word, why = dashboard.pr_verdict(_pr(states=["SUCCESS"] * 19 + [state]))
    assert word != dashboard.MERGE, f"{state} was treated as a pass: {why}"


@pytest.mark.parametrize("state", ["CANCELLED", "SKIPPED", "NEUTRAL"])
def test_a_leg_that_is_neither_pass_nor_failure_is_unknown_and_is_named(state):
    word, why = dashboard.pr_verdict(_pr(states=["SUCCESS"] * 19 + [state]))
    assert word == dashboard.UNKNOWN, why
    assert state.lower().replace("_", " ") in why.lower() or state in why


@pytest.mark.parametrize("state", ["TIMED_OUT", "ACTION_REQUIRED"])
def test_a_leg_with_no_verdict_of_its_own_is_red(state):
    assert dashboard.pr_verdict(_pr(states=["SUCCESS"] * 19 + [state]))[0] == dashboard.RED


def test_a_state_nobody_taught_this_module_about_is_unknown_not_merge():
    word, _ = dashboard.pr_verdict(_pr(states=["SUCCESS", "GRUE"]))
    assert word == dashboard.UNKNOWN


def test_a_rollup_that_did_not_come_back_is_unknown_not_an_empty_green():
    word, why = dashboard.pr_verdict(_pr(states=None))
    assert word == dashboard.UNKNOWN
    assert "did not" in why or "not established" in why


def test_zero_checks_is_unknown_rather_than_vacuously_green():
    word, _ = dashboard.pr_verdict(_pr(states=[]))
    assert word == dashboard.UNKNOWN


def test_a_failed_leg_is_red():
    assert dashboard.pr_verdict(_pr(states=["SUCCESS", "FAILURE"]))[0] == dashboard.RED


def test_a_pending_leg_is_waiting():
    assert dashboard.pr_verdict(_pr(states=["SUCCESS", "IN_PROGRESS"]))[0] == dashboard.WAITING


def test_green_but_conflicting_is_rebase():
    assert dashboard.pr_verdict(_pr(mergeable="CONFLICTING"))[0] == dashboard.REBASE


def test_green_but_behind_the_base_is_rebase():
    assert dashboard.pr_verdict(_pr(merge_state="BEHIND"))[0] == dashboard.REBASE


def test_green_but_draft_is_not_a_merge_signal():
    assert dashboard.pr_verdict(_pr(draft=True))[0] == dashboard.DRAFT


def test_an_unreadable_mergeability_is_unknown_not_merge():
    word, why = dashboard.pr_verdict(_pr(mergeable="UNKNOWN", merge_state=""))
    assert word == dashboard.UNKNOWN
    assert word != dashboard.MERGE


def test_blocked_is_not_a_merge_even_with_every_leg_green():
    assert dashboard.pr_verdict(_pr(merge_state="BLOCKED"))[0] != dashboard.MERGE


# ── lane occupancy ───────────────────────────────────────────────────────

LANES = ["lane:watch", "lane:release"]


def _wt(path="/w/859", branch="fix/859", state="occupied", lanes=("lane:watch",)):
    return dashboard.Worktree(path=path, branch=branch, state=state,
                         lanes=list(lanes))


def test_an_undecidable_worktree_makes_its_lane_unknown_never_free():
    """`cannot tell` is not `idle`, one layer up.

    The lane has no open PR and exactly one worktree, whose liveness nothing
    answered. Reporting it free is the two-agents-one-file failure.
    """
    states = dashboard.lane_states(LANES, prs=[], worktrees=[_wt(state="cannot tell")])
    state, evidence = states["lane:watch"]
    assert state == dashboard.LANE_UNKNOWN, evidence
    assert state != dashboard.LANE_FREE
    assert any("cannot tell" in e for e in evidence)


def test_an_occupied_worktree_makes_its_lane_occupied():
    states = dashboard.lane_states(LANES, prs=[], worktrees=[_wt()])
    assert states["lane:watch"][0] == dashboard.LANE_OCCUPIED


def test_an_open_pr_makes_its_lane_occupied():
    pr = _pr(number=954, branch="fix/859")
    pr.lanes = ["lane:watch"]
    states = dashboard.lane_states(LANES, prs=[pr], worktrees=[])
    assert states["lane:watch"][0] == dashboard.LANE_OCCUPIED


def test_a_lane_with_nothing_pointing_at_it_is_free():
    states = dashboard.lane_states(LANES, prs=[], worktrees=[_wt()])
    assert states["lane:release"][0] == dashboard.LANE_FREE


def test_an_unattributable_live_worktree_denies_free_to_every_lane():
    """The occupancy exists; which lane it belongs to does not.

    `feat/pr-ops` carries no issue number, so nothing maps it to a lane. A lane
    printed `free` next to an occupancy nobody could place is a claim the data
    does not support.
    """
    stray = _wt(path="/w/pr-ops", branch="feat/pr-ops", state="occupied", lanes=[])
    states = dashboard.lane_states(LANES, prs=[], worktrees=[stray])
    for lane in LANES:
        state, evidence = states[lane]
        assert state == dashboard.LANE_UNKNOWN, (lane, evidence)
        assert any("could not be placed" in e for e in evidence)
    # ...and the stray is named once, by the section, not seven times over.
    assert dashboard.stray_worktrees([stray]) == [stray]
    assert "pr-ops" in dashboard.render_lanes(states, [stray]).lines[0]


def test_the_clone_on_the_default_branch_is_not_a_stray_occupancy():
    """The one exclusion, and why it is not a hole.

    The main clone sits on `master` permanently. Counting it as an unplaced
    occupancy would deny `free` to every lane on every call forever, and an
    alarm that can never clear is the same as no alarm.
    """
    clone = _wt(path="/repo", branch="master", state="cannot tell", lanes=[])
    states = dashboard.lane_states(LANES, prs=[], worktrees=[clone],
                              default_branch="master")
    assert states["lane:release"][0] == dashboard.LANE_FREE

    # ...and it is an exclusion of one, not of the rule.
    states = dashboard.lane_states(LANES, prs=[], worktrees=[
        clone, _wt(path="/w/x", branch="feat/x", state="occupied", lanes=[])],
        default_branch="master")
    assert states["lane:release"][0] == dashboard.LANE_UNKNOWN


def test_an_idle_unattributed_worktree_does_not_deny_free():
    stray = _wt(path="/w/old", branch="feat/old", state="idle", lanes=[])
    states = dashboard.lane_states(LANES, prs=[], worktrees=[stray])
    assert states["lane:release"][0] == dashboard.LANE_FREE


def test_the_lane_universe_being_unreadable_declines_the_whole_section():
    assert dashboard.lane_states(None, prs=[], worktrees=[]) is None


# ── partial failure ──────────────────────────────────────────────────────

def test_a_section_that_could_not_be_fetched_says_so_and_is_not_omitted():
    """The defect class this repo files most.

    A dashboard whose board never arrived must not be readable as a dashboard with an
    empty board — so the heading is still printed, the failure is named in
    words, and the footer counts it.
    """
    report = dashboard.Report(repo="o/r", sections={
        "local": dashboard.Section("local", lines=["clone in sync"]),
        "default": dashboard.Section("default", lines=["GREEN"]),
        "board": dashboard.Section("board", error="gh pr list exited 1: rate limited"),
        "worktrees": dashboard.Section("worktrees", lines=["idle  /w/1  fix/1"]),
        "lanes": dashboard.Section("lanes", lines=["free  release"]),
    })
    out = dashboard.render(report)
    assert "board" in out
    assert "rate limited" in out
    assert "unread" in out
    assert "1 section unread" in out.splitlines()[-1]


def test_a_section_rendered_from_a_failed_read_is_counted_as_degraded():
    """`0 sections unread` beside a section built on a read that failed is the
    same sentence as an omitted section — so degraded is counted apart."""
    report = dashboard.Report(repo="o/r", sections={
        "lanes": dashboard.Section("lanes", lines=["unknown  release"],
                              warning="the issue labels could not be read"),
    })
    out = dashboard.render(report)
    assert "degraded" in out
    assert "the issue labels could not be read" in out
    assert "0 sections unread, 1 degraded" in out.splitlines()[-1]


def test_the_next_line_refuses_to_advise_when_the_board_is_unread():
    report = dashboard.Report(repo="o/r", sections={
        "board": dashboard.Section("board", error="gh pr list exited 1"),
    })
    line = dashboard.next_action(report)
    assert "UNKNOWN" in line or "could not" in line
    assert "merge" not in line.lower()


def test_the_result_line_is_last_and_survives_tail_one():
    report = dashboard.Report(repo="o/r", sections={
        "board": dashboard.Section("board", lines=["MERGE #1"]),
    })
    assert dashboard.render(report).splitlines()[-1].startswith("[result]")


# ── `next:` is one opinion ───────────────────────────────────────────────

def test_one_ready_pr_names_a_runnable_command():
    report = dashboard.Report(repo="o/r", sections={
        "board": dashboard.Section("board", lines=[]),
    }, prs=[_pr(number=951)])
    line = dashboard.next_action(report)
    assert "951" in line


def test_several_equally_ready_prs_say_so_instead_of_picking_one():
    report = dashboard.Report(repo="o/r", sections={
        "board": dashboard.Section("board", lines=[]),
    }, prs=[_pr(number=951), _pr(number=944)])
    line = dashboard.next_action(report)
    assert "951" in line and "944" in line
    assert "equally" in line or "no single" in line


def test_nothing_ready_is_said_plainly_rather_than_invented():
    report = dashboard.Report(repo="o/r", sections={
        "board": dashboard.Section("board", lines=[]),
    }, prs=[_pr(states=["IN_PROGRESS"])])
    line = dashboard.next_action(report)
    assert "nothing ready" in line.lower()


# ── read-only, permanently ───────────────────────────────────────────────

FORBIDDEN = (
    "gh pr merge", "gh pr create", "gh pr close", "gh issue close",
    "gh issue comment", "gh pr comment", "gh api -X", "--method POST",
    "git push", "git fetch", "git pull", "git commit", "git worktree add",
    "git worktree remove", "git worktree prune", "worktree unlock",
    "subprocess.Popen",
)


def test_dashboard_never_names_a_mutating_command():
    """Inspection fused to action is what kept a subsystem unobservable for
    hours. `dashboard` reads; it does not heal, spawn or fetch."""
    src = (Path(__file__).parent.parent / "presets" / "dashboard" / "dashboard.py").read_text(encoding="utf-8")
    for needle in FORBIDDEN:
        assert needle not in src, needle


def test_dashboard_is_registered_and_documented():
    import json
    root = Path(__file__).parent.parent
    preset = json.loads((root / "presets" / "dashboard.json").read_text(encoding="utf-8"))
    assert "dashboard" in preset["ops"]
    assert "dashboard" in json.loads((root / ".supertool.json").read_text(encoding="utf-8"))["presets"]
    assert (root / "docs" / "presets" / "dashboard.md").is_file()
    # A fragment is CONSUMED by the release that ships it, so asserting its
    # continued existence is a guard that cannot survive its own release — it
    # fails on the one event it should be indifferent to. #941 hit exactly this
    # on the v0.26.0 release commit and fixed its own instance; this is the same
    # bug, one test over, found on the v0.27.0 release commit.
    #
    # What this test claims is that the change is *findable*. A pending fragment
    # and a released CHANGELOG entry both satisfy that, and exactly one of them
    # is true at any moment, so accepting either loses no coverage.
    fragments = list((root / "changelog.d").glob("953.*.md"))
    if fragments:
        return
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "953" in changelog, (
        "#953 is neither a pending changelog.d/953.<section>.md fragment nor "
        "an entry in CHANGELOG.md — the change is not findable in either place"
    )
