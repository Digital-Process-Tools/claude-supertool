"""gh-pr-merge refuses what it cannot verify, and never renders a partial as a pass (#950).

The two cases pinned hardest are the two that regress silently:

* a PR that is **not fully green** must not merge, and "green" is #454's
  arithmetic — a `CANCELLED` leg is neither a pass nor a pending;
* a merge that **landed while its linked issue stayed open** must not render as
  a success, because that is the state PR #908 shipped in on 2026-08-07 (body
  said `Closes #899`, GitHub bound nothing, no error anywhere).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location("github_pr_merge", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _leg(name: str, conclusion: str) -> dict:
    return {
        "name": name,
        "conclusion": conclusion,
        "status": "COMPLETED",
        "detailsUrl": f"https://github.com/o/r/actions/runs/1/job/{abs(hash(name)) % 9999}",
    }


def _pr(**over) -> dict:
    base = {
        "number": 951,
        "title": "a change",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "baseRefName": "master",
        "headRefName": "fix/950",
        "headRefOid": "a" * 40,
        "url": "https://github.com/o/r/pull/951",
        "body": "Closes #950",
        "statusCheckRollup": [_leg("tests", "SUCCESS"), _leg("lint", "SUCCESS")],
    }
    base.update(over)
    return base


def _text(lines) -> str:
    return "\n".join(lines)


# ===========================================================================
# The gate — what it refuses
# ===========================================================================

def test_all_green_pr_is_allowed():
    allowed, lines = m.gate(_pr(), declared=2)
    assert allowed is True, _text(lines)


def test_cancelled_leg_is_not_a_pass_and_blocks_the_merge():
    """#454's leg, in the merge path this time."""
    pr = _pr(statusCheckRollup=[
        _leg("tests", "SUCCESS"),
        _leg("e2e", "CANCELLED"),
    ])
    allowed, lines = m.gate(pr, declared=2)
    assert allowed is False
    body = _text(lines)
    assert "2 total" in body and "1 cancelled" in body, body
    assert "e2e" in body, body


def test_skipped_neutral_timed_out_action_required_all_block():
    for state in ("SKIPPED", "NEUTRAL", "TIMED_OUT", "ACTION_REQUIRED"):
        pr = _pr(statusCheckRollup=[_leg("tests", "SUCCESS"), _leg("x", state)])
        allowed, lines = m.gate(pr, declared=2)
        assert allowed is False, f"{state} was treated as a pass"
        assert state.lower() in _text(lines).lower(), state


def test_pending_leg_blocks_the_merge():
    pr = _pr(statusCheckRollup=[
        _leg("tests", "SUCCESS"),
        {"name": "e2e", "status": "IN_PROGRESS", "detailsUrl": ""},
    ])
    allowed, lines = m.gate(pr, declared=2)
    assert allowed is False
    assert "1 pending" in _text(lines)


def test_pending_legs_are_named_even_though_the_shared_helper_drops_them():
    """`named_disclosure` excludes pending on purpose — on a gate that is wrong.

    Found on PR #951 against the live API: `3 pending` was refused with no leg
    named at all, which sends the reader back to the web UI to find out which.
    """
    pr = _pr(statusCheckRollup=[
        _leg("tests", "SUCCESS"),
        {"name": "e2e (3.11)", "status": "IN_PROGRESS", "detailsUrl": ""},
        {"name": "e2e (3.12)", "status": "QUEUED", "detailsUrl": ""},
    ])
    allowed, lines = m.gate(pr, declared=3)
    assert allowed is False
    body = _text(lines)
    assert "e2e (3.11)" in body and "e2e (3.12)" in body, body


def test_a_purely_pending_refusal_does_not_talk_about_cancelled_legs():
    pr = _pr(statusCheckRollup=[
        {"name": "tests", "status": "IN_PROGRESS", "detailsUrl": ""}])
    _, lines = m.gate(pr, declared=1)
    body = _text(lines)
    assert "cancelled" not in body.lower(), body
    assert "not finished" in body.lower() or "waiting" in body.lower(), body


def test_a_settled_red_still_gets_the_not_a_pass_sentence():
    pr = _pr(statusCheckRollup=[_leg("e2e", "CANCELLED")])
    _, lines = m.gate(pr, declared=1)
    assert "#454" in _text(lines)


def test_zero_checks_is_refused_not_read_as_nothing_failed():
    allowed, lines = m.gate(_pr(statusCheckRollup=[]), declared=None)
    assert allowed is False
    body = _text(lines)
    assert "zero" in body.lower() or "none reported" in body.lower(), body


def test_unreadable_rollup_is_refused_and_says_unknown():
    allowed, lines = m.gate(_pr(statusCheckRollup=None), declared=None)
    assert allowed is False
    assert "UNKNOWN" in _text(lines)


def test_tally_that_cannot_be_reconciled_is_refused():
    """`declared is None` is a doubt, and a doubt is not permission on a gate.

    Same call gh-branch's `verdict()` makes with `unreconciled`: every leg read
    passed, but whether those are all of the legs is UNKNOWN.
    """
    allowed, lines = m.gate(_pr(), declared=None)
    assert allowed is False
    assert "UNKNOWN" in _text(lines)


def test_declared_shortfall_is_refused_and_names_the_numbers():
    allowed, lines = m.gate(_pr(), declared=14, missing=["e2e (3.11)"])
    assert allowed is False
    body = _text(lines)
    assert "2 of 14" in body, body
    assert "e2e (3.11)" in body, body


def test_conflicting_pr_is_refused():
    allowed, lines = m.gate(_pr(mergeable="CONFLICTING"), declared=2)
    assert allowed is False
    assert "conflict" in _text(lines).lower()


def test_unknown_mergeability_is_refused_not_assumed_mergeable():
    allowed, lines = m.gate(_pr(mergeable="UNKNOWN"), declared=2)
    assert allowed is False
    assert "UNKNOWN" in _text(lines)


def test_draft_is_refused():
    allowed, lines = m.gate(_pr(isDraft=True), declared=2)
    assert allowed is False
    assert "draft" in _text(lines).lower()


def test_already_merged_pr_is_refused_and_names_its_state():
    allowed, lines = m.gate(_pr(state="MERGED"), declared=2)
    assert allowed is False
    assert "MERGED" in _text(lines)


def test_changes_requested_is_refused():
    allowed, lines = m.gate(_pr(reviewDecision="CHANGES_REQUESTED"), declared=2)
    assert allowed is False
    assert "CHANGES_REQUESTED" in _text(lines)


def test_blocked_merge_state_is_refused():
    allowed, lines = m.gate(_pr(mergeStateStatus="BLOCKED"), declared=2)
    assert allowed is False
    assert "BLOCKED" in _text(lines)


def test_a_refusal_names_a_way_forward():
    _, lines = m.gate(_pr(statusCheckRollup=[_leg("e2e", "FAILURE")]), declared=1)
    body = _text(lines)
    assert "gh-pr:" in body or "gh-job:" in body, body


# ===========================================================================
# Linked issues — the reason this op exists
# ===========================================================================

def test_body_ref_github_never_bound_is_named():
    """PR #908's exact shape: body says `Closes #899`, GitHub bound nothing."""
    refs, unbound, note = m.reconcile_links(["#899"], [])
    assert refs == ["#899"]
    assert unbound == ["#899"]
    assert note == ""


def test_bound_and_declared_agree_leaves_nothing_unbound():
    refs, unbound, note = m.reconcile_links(["#950"], ["#950"])
    assert refs == ["#950"]
    assert unbound == []


def test_bound_list_unreadable_is_a_note_not_an_empty_list():
    refs, unbound, note = m.reconcile_links(["#950"], None)
    assert refs == ["#950"]
    assert unbound == []
    assert "UNKNOWN" in note, note


def test_an_issue_bound_but_not_in_the_body_is_still_checked():
    refs, _, _ = m.reconcile_links([], ["#42"])
    assert refs == ["#42"]


def test_issue_lookup_failure_is_unknown_not_closed():
    verdicts = m.issue_verdicts(["#950"], lambda r: ("", "rate limited"))
    assert verdicts == [("#950", m.UNKNOWN, "rate limited")]


def test_issue_states_are_carried_verbatim():
    def lookup(ref):
        return ({"#1": "CLOSED", "#2": "OPEN"}[ref], "")
    assert m.issue_verdicts(["#1", "#2"], lookup) == [
        ("#1", "CLOSED", ""), ("#2", "OPEN", "")]


def test_open_issue_after_merge_is_NOT_CLOSED_and_names_the_fix_command():
    lines, overall = m.render_issue_section(
        [("#950", "OPEN", "")], [], "", "Digital-Process-Tools/claude-supertool")
    assert overall == m.NOT_CLOSED
    body = _text(lines)
    assert "#950" in body
    assert "gh issue close 950" in body, body


def test_unknown_issue_state_is_UNKNOWN_never_closed_never_open():
    lines, overall = m.render_issue_section(
        [("#950", m.UNKNOWN, "rate limited")], [], "", "o/r")
    assert overall == m.UNKNOWN
    body = _text(lines)
    assert "unknown" in body.lower() and "rate limited" in body


def test_all_closed_is_only_claimed_when_every_state_was_read():
    lines, overall = m.render_issue_section(
        [("#950", "CLOSED", "")], [], "", "o/r")
    assert overall == m.ALL_CLOSED


def test_no_declared_refs_is_its_own_state():
    lines, overall = m.render_issue_section([], [], "", "o/r")
    assert overall == m.NONE_DECLARED


def test_unbound_ref_is_named_even_when_the_issue_is_closed_anyway():
    lines, overall = m.render_issue_section(
        [("#899", "CLOSED", "")], ["#899"], "", "o/r")
    body = _text(lines)
    assert "#899" in body
    assert "not bound" in body.lower(), body


def test_a_bound_list_note_downgrades_the_overall_to_unknown():
    _, overall = m.render_issue_section(
        [("#950", "CLOSED", "")], [], "could not read — UNKNOWN", "o/r")
    assert overall == m.UNKNOWN


# ===========================================================================
# Merge verification — never print a result that was not read
# ===========================================================================

def test_merge_is_only_verified_from_a_state_read_back():
    state, lines = m.merge_verdict(
        {"state": "MERGED", "mergedAt": "2026-08-07T10:00:00Z",
         "mergeCommit": {"oid": "b" * 40}}, "")
    assert state == m.MERGED
    body = _text(lines)
    assert "b" * 7 in body
    assert "2026-08-07T10:00:00Z" in body


def test_zero_exit_from_gh_is_not_a_merge():
    state, lines = m.merge_verdict({"state": "OPEN", "mergedAt": None,
                                    "mergeCommit": None}, "")
    assert state == m.UNVERIFIED
    assert "OPEN" in _text(lines)


def test_readback_failure_is_unverified_with_its_reason():
    state, lines = m.merge_verdict(None, "gh timed out")
    assert state == m.UNVERIFIED
    assert "gh timed out" in _text(lines)


def test_merged_without_a_merge_commit_is_not_fully_verified():
    state, _ = m.merge_verdict(
        {"state": "MERGED", "mergedAt": "2026-08-07T10:00:00Z",
         "mergeCommit": None}, "")
    assert state == m.UNVERIFIED


# ===========================================================================
# The receipt — the partial success is the case that matters
# ===========================================================================

def test_merged_but_issue_open_is_not_a_success_line():
    line = m.result_line(m.MERGED, m.NOT_CLOSED, "GREEN")
    assert line.startswith("[result]")
    low = line.lower()
    assert "merged" in low
    assert "not closed" in low or "still open" in low, line
    assert not low.startswith("[result] ok"), line


def test_a_partial_never_claims_a_rollback():
    line = m.result_line(m.MERGED, m.NOT_CLOSED, "GREEN")
    assert "roll" not in line.lower() and "revert" not in line.lower()


def test_unknown_issue_state_is_not_folded_into_success():
    line = m.result_line(m.MERGED, m.UNKNOWN, "GREEN")
    assert "unknown" in line.lower()


def test_full_success_says_merged_and_closed():
    line = m.result_line(m.MERGED, m.ALL_CLOSED, "GREEN")
    low = line.lower()
    assert "merged" in low and "closed" in low


def test_default_branch_state_reaches_the_verdict_line():
    line = m.result_line(m.MERGED, m.ALL_CLOSED, "NOT GREEN")
    assert "NOT GREEN" in line


def test_unverified_merge_leads_the_verdict():
    line = m.result_line(m.UNVERIFIED, m.NONE_DECLARED, "GREEN")
    assert "unverified" in line.lower()


def test_result_line_is_one_line():
    for merge in (m.MERGED, m.UNVERIFIED):
        for issues in (m.ALL_CLOSED, m.NOT_CLOSED, m.UNKNOWN, m.NONE_DECLARED):
            line = m.result_line(merge, issues, "GREEN")
            assert "\n" not in line, (merge, issues)
