"""`git-worktrees` names each tree's open PR — in four states (#941).

The board answers *is anyone working here?* and never answered *is this tree's
work published, and is it green?* — so the orchestrator joined branch → path →
PR number → check tally by hand, across four ops, and carried the map in its
head.

The join is the easy part. The part these assertions exist for is that the
tracker column has **four** answers that must not collapse:

1. an open PR exists,
2. no open PR for a branch that *is* pushed,
3. the branch was never pushed at all,
4. the lookup did not run — offline, unauthenticated, rate-limited, not a
   GitHub remote.

4 rendered as 2 is this repository's most-filed defect: an absence produced by
the tool, read as an absence in the world. Here it would land in the op used to
decide which worktree to act in, and it would read as "this work is
unpublished, go ahead and take the tree".
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees_941", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

_gc_spec = importlib.util.spec_from_file_location(
    "git_common_941", ROOT / "presets" / "git" / "_git_common.py")
assert _gc_spec is not None and _gc_spec.loader is not None
gc = importlib.util.module_from_spec(_gc_spec)
_gc_spec.loader.exec_module(gc)


def _pr(number: int, head: str, *, base: str = "master", states=("SUCCESS",),
        mergeable: str = "MERGEABLE", draft: bool = False) -> dict:
    return {
        "number": number,
        "headRefName": head,
        "baseRefName": base,
        "isDraft": draft,
        "mergeable": mergeable,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": f"leg{i}", "status": "COMPLETED",
             "conclusion": s}
            for i, s in enumerate(states)
        ],
    }


def _answered(*prs, truncated: bool = False, limit: int = 100) -> "gc.PrIndex":
    return gc.PrIndex({p["headRefName"]: p for p in prs},
                      truncated=truncated, limit=limit)


def _declined(reason: str) -> "gc.PrIndex":
    return gc.PrIndex(None, reason=reason)


# ── the four states, one assertion each ──────────────────────────────────

def test_open_pr_is_named_with_its_tally() -> None:
    index = _answered(_pr(941, "fix/941", states=("SUCCESS",) * 3))
    got = wt.tracker_for("fix/941", index, {"fix/941"})
    assert got.state == wt.TRACKER_PR, got
    assert "#941" in got.token
    assert "3 total: 3 passed" in got.detail, got.detail


def test_pushed_branch_with_no_pr_says_so() -> None:
    index = _answered(_pr(940, "fix/864-875"))
    got = wt.tracker_for("fix/941", index, {"fix/941"})
    assert got.state == wt.TRACKER_NONE, got
    assert "no open PR" in got.token


def test_branch_with_no_remote_ref_is_not_the_same_answer_as_no_pr() -> None:
    """State 3 has a different remedy from state 2: push, versus open a PR."""
    index = _answered(_pr(940, "fix/864-875"))
    pushed = wt.tracker_for("fix/941", index, {"fix/941"})
    absent = wt.tracker_for("fix/941", index, set())
    assert absent.state == wt.TRACKER_NO_REMOTE, absent
    assert absent.state != pushed.state
    assert absent.token != pushed.token
    assert "no remote-tracking ref" in absent.detail, absent.detail


def test_a_missing_remote_ref_is_not_reported_as_never_pushed() -> None:
    """A merged-and-deleted branch leaves the same local trace as an unpushed one.

    Live against the fleet, four `[merged]` worktrees were rendered "the branch
    has never been pushed" — all four had been pushed, merged and deleted on the
    remote. The op may report what it observed; it may not pick one of the two
    histories that produce that observation.
    """
    absent = wt.tracker_for("fix/859", _answered(), set())
    assert "the branch has never been pushed" not in absent.detail
    assert "either never pushed, or" in absent.detail, absent.detail
    assert "deleted" in absent.detail, absent.detail


def test_failed_lookup_is_unknown_and_never_no_pr() -> None:
    """The regression that will otherwise ship silently.

    A lookup that could not run must not render as the *world* having no PR.
    """
    index = _declined("gh exited 4: authentication required")
    got = wt.tracker_for("fix/941", index, {"fix/941"})
    assert got.state == wt.TRACKER_UNKNOWN, got
    assert "unknown" in got.token.lower()
    assert "no open PR" not in got.token
    assert "no open PR" not in got.detail
    assert "authentication required" in got.detail, got.detail


def test_failed_lookup_stays_unknown_even_for_an_unpushed_branch() -> None:
    """Local knowledge must not be promoted into an answer about the tracker."""
    got = wt.tracker_for("fix/941", _declined("network is unreachable"), set())
    assert got.state == wt.TRACKER_UNKNOWN, got


def test_detached_worktree_has_no_branch_to_look_up() -> None:
    got = wt.tracker_for("", _answered(), {"fix/941"})
    assert got.state == wt.TRACKER_NA, got
    assert "no open PR" not in got.token


# ── the partially-answered batch ─────────────────────────────────────────

def test_a_truncated_pr_page_is_unknown_not_no_pr() -> None:
    """One call for N trees — but a capped page did not establish absence.

    A branch missing from a page that hit its own limit is exactly state 4
    wearing state 2's clothes.
    """
    index = _answered(_pr(940, "fix/864-875"), truncated=True, limit=1)
    got = wt.tracker_for("fix/941", index, {"fix/941"})
    assert got.state == wt.TRACKER_UNKNOWN, got
    assert "cap" in got.detail or "limit" in got.detail, got.detail


def test_a_truncated_page_still_answers_for_a_branch_it_did_name() -> None:
    index = _answered(_pr(940, "fix/864-875"), truncated=True, limit=1)
    got = wt.tracker_for("fix/864-875", index, {"fix/864-875"})
    assert got.state == wt.TRACKER_PR, got


def test_unknown_pushed_state_does_not_destroy_an_established_no_pr() -> None:
    """`git for-each-ref` failing costs the 2-vs-3 split, not the answer."""
    got = wt.tracker_for("fix/941", _answered(), None,
                         remote_why="git for-each-ref exited 128")
    assert got.state == wt.TRACKER_NONE, got
    assert "UNKNOWN" in got.detail, got.detail


# ── N trees, one network call ────────────────────────────────────────────

def test_the_lookup_is_one_call_regardless_of_tree_count() -> None:
    calls: list = []

    def runner(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(
            [_pr(940, "fix/864-875"), _pr(937, "fix/910-879")]), "")

    index = gc.query_open_prs_by_branch(runner=runner)
    assert len(calls) == 1, calls
    for branch in ("fix/864-875", "fix/910-879", "fix/941", "master"):
        wt.tracker_for(branch, index, {branch})
    assert len(calls) == 1, calls
    assert index.answered


def test_a_nonzero_gh_exit_declines_with_its_stderr() -> None:
    def runner(args):
        return subprocess.CompletedProcess(
            args, 1, "", "could not determine base repository")

    index = gc.query_open_prs_by_branch(runner=runner)
    assert not index.answered
    assert index.by_branch is None
    assert "could not determine base repository" in index.reason


def test_unparseable_gh_output_declines_rather_than_reading_as_empty() -> None:
    def runner(args):
        return subprocess.CompletedProcess(args, 0, "not json at all", "")

    index = gc.query_open_prs_by_branch(runner=runner)
    assert not index.answered, index.by_branch


def test_a_missing_gh_binary_declines() -> None:
    def runner(args):
        raise FileNotFoundError(2, "No such file or directory: 'gh'")

    index = gc.query_open_prs_by_branch(runner=runner)
    assert not index.answered
    assert "gh" in index.reason


def test_a_timed_out_gh_declines_rather_than_reporting_an_empty_tracker() -> None:
    def runner(args):
        raise subprocess.TimeoutExpired(args, 6)

    index = gc.query_open_prs_by_branch(runner=runner)
    assert not index.answered
    assert index.reason


def test_an_empty_open_pr_list_is_an_answer_not_a_failure() -> None:
    def runner(args):
        return subprocess.CompletedProcess(args, 0, "[]", "")

    index = gc.query_open_prs_by_branch(runner=runner)
    assert index.answered
    assert index.by_branch == {}


# ── the board ────────────────────────────────────────────────────────────

def _row(branch: str, tracker, path: str = "/tmp/st-wt/941"):
    entry = {"path": path, "branch": branch, "detached": False, "bare": False,
             "locked": None, "prunable": None, "gitdir": None}
    return (entry, wt.Assessment(wt.STATE_UNKNOWN, ["no positive signal"]), tracker)


def test_the_board_prints_the_pr_on_the_row() -> None:
    tracker = wt.tracker_for("fix/941", _answered(_pr(941, "fix/941")), {"fix/941"})
    out = wt.render([_row("fix/941", tracker)], merged=set())
    assert "#941" in out
    assert any("#941" in line for line in out.splitlines()
               if line.startswith("cannot tell")), out


def test_the_board_footer_counts_the_rows_whose_tracker_did_not_answer() -> None:
    """The tally that survives `| tail -1` must not hide a missing answer."""
    declined = _declined("network is unreachable")
    rows = [_row("fix/941", wt.tracker_for("fix/941", declined, set())),
            _row("fix/936", wt.tracker_for("fix/936", declined, set()))]
    out = wt.render(rows, merged=set())
    result = [l for l in out.splitlines() if l.startswith("[result]")]
    assert result, out
    assert "2 tracker unknown" in result[0], result[0]


def test_a_board_with_every_tracker_answered_says_nothing_about_unknowns() -> None:
    tracker = wt.tracker_for("fix/941", _answered(_pr(941, "fix/941")), {"fix/941"})
    out = wt.render([_row("fix/941", tracker)], merged=set())
    result = [l for l in out.splitlines() if l.startswith("[result]")][0]
    assert "tracker unknown" not in result, result


def test_a_newline_in_the_lookup_reason_cannot_forge_a_row() -> None:
    """gh's stderr is not this tool's text — #876, on a new input.

    A reason carrying a newline plus a fabricated `idle` row must stay on the
    one line it was given.
    """
    forged = "idle         evil          /tmp/evil"
    index = _declined("gh exploded\n" + forged)
    tracker = wt.tracker_for("fix/941", index, {"fix/941"})
    out = wt.render([_row("fix/941", tracker)], merged=set())
    assert not any(line.startswith("idle ") for line in out.splitlines()), out
    assert "evil" in out


# ── opting out ───────────────────────────────────────────────────────────

def test_nopr_flag_is_parsed_off_the_argument_list() -> None:
    path, want_pr = wt.parse_args(["/tmp/st-wt/941", "nopr"])
    assert path == "/tmp/st-wt/941"
    assert want_pr is False
    path, want_pr = wt.parse_args(["/tmp/st-wt/941"])
    assert path == "/tmp/st-wt/941"
    assert want_pr is True


def test_the_env_knob_turns_the_lookup_off(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_WORKTREE_PR", "0")
    _path, want_pr = wt.parse_args([])
    assert want_pr is False


def test_suppressed_tracker_is_absent_from_the_row_not_rendered_as_no_pr() -> None:
    out = wt.render([_row("fix/941", None)], merged=set())
    assert "no open PR" not in out
    assert "PR unknown" not in out


# ── shipped means findable ───────────────────────────────────────────────

def test_the_op_syntax_advertises_the_flag() -> None:
    manifest = json.loads((ROOT / "presets" / "git.json").read_text(encoding="utf-8"))
    entry = manifest["ops"]["git-worktrees"]
    assert "nopr" in entry["syntax"], entry["syntax"]
    assert "PR" in entry["description"]


def test_the_four_states_are_documented() -> None:
    doc = (ROOT / "docs" / "presets" / "git.md").read_text(encoding="utf-8")
    assert "no open PR" in doc
    assert "nopr" in doc
    assert "SUPERTOOL_WORKTREE_PR" in doc


def test_a_changelog_fragment_exists() -> None:
    """#941 is documented — as a pending fragment, or as a released entry.

    The fragment form only holds between the merge and the release that
    consumes it: `assemble_changelog.py` deletes every fragment it folds into
    a version heading. Asserting the fragment alone made this test unable to
    survive its own release, and it reddened five legs on the v0.26.0 release
    commit — a guard that fails on the one event it should be indifferent to.

    What the section heading above actually claims is that the change is
    findable. Both states satisfy that, and exactly one of them is true at any
    moment, so accepting either loses no coverage.
    """
    fragments = list((ROOT / "changelog.d").glob("941.*.md"))
    if fragments:
        return
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "941" in changelog, (
        "#941 is neither a pending changelog.d/941.<section>.md fragment nor "
        "an entry in CHANGELOG.md — the change is not findable in either place"
    )
