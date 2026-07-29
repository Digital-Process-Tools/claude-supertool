"""Unit tests for the gitlab-mr poller source — state diff logic."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gitlab-mr" / "poller.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_poller", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


# The genuine `_glab_api`, kept before the autouse stub below shadows it, for
# the one test that needs to exercise its own error handling rather than a mock.
_REAL_GLAB_API = poller._glab_api


@pytest.fixture(autouse=True)
def _no_real_glab():
    """Keep the suite hermetic now that a failure transition makes a second call.

    Tests patch `_fetch`, which is the *first* call; the #509 failing-job
    lookup goes through `_glab_api` directly and would otherwise shell out to
    a real `glab` on every `pipeline_failed` test. Stubbed to an empty job
    list — tests that care about the lookup patch over this with their own.
    """
    with mock.patch.object(poller, "_glab_api", return_value=[]):
        yield


def _mr(state="opened", pipeline_status="running", pipeline_id="9", conflicts=False,
        user_notes_count=0, merge_status=None, **kw):
    """A settled MR by default: `merge_status` agrees with `has_conflicts`.

    Pass `merge_status` explicitly to model GitLab mid-check, where
    `has_conflicts` is False because nothing has been computed yet.
    """
    if merge_status is None:
        merge_status = "cannot_be_merged" if conflicts else "can_be_merged"
    body = {
        "iid": 21803,
        "title": kw.get("title", "feat: do the thing"),
        "state": state,
        "has_conflicts": conflicts,
        "merge_status": merge_status,
        "head_pipeline": {"id": pipeline_id, "status": pipeline_status},
        "web_url": "https://example.com/mr/21803",
        "user_notes_count": user_notes_count,
    }
    if merge_status == "":
        del body["merge_status"]
    return body


def _rechecking():
    """GitLab re-running its mergeability check after a push to the target branch.

    Observed live: `cannot_be_merged_recheck` / `has_conflicts: false` on an MR
    whose conflict was never resolved. The False is "not computed", not "clean".
    """
    return _mr(conflicts=False, merge_status="cannot_be_merged_recheck")


def _drive(sequence):
    """Poll once per MR body in `sequence`, threading state. Returns all events."""
    state = {}
    emitted = []
    for body in sequence:
        with mock.patch.object(poller, "_fetch", return_value=body):
            events, state = poller.poll(state, {"id": "21803"})
        emitted.extend(events)
    return emitted, state


def test_no_change_emits_nothing() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running", "has_conflicts": False}
    with mock.patch.object(poller, "_fetch", return_value=_mr()):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert events == []
    assert new_state["mr_state"] == "opened"


def test_pipeline_running_to_failed_emits_failed() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(pipeline_status="failed")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert len(events) == 1
    assert events[0]["event"] == "pipeline_failed"
    assert events[0]["payload"]["url"] == "https://example.com/mr/21803"
    assert events[0]["notify_title"]


def test_pipeline_running_to_success_emits_succeeded() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(pipeline_status="success")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "pipeline_succeeded" for e in events)


def test_pipeline_pending_to_running_emits_running() -> None:
    state = {"mr_state": "opened", "pipeline_status": "pending"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(pipeline_status="running")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "pipeline_running" for e in events)


def test_merge_emits_merged() -> None:
    state = {"mr_state": "opened", "pipeline_status": "success"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(state="merged", pipeline_status="success")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "merged" for e in events)


def test_conflict_rising_edge_emits_once() -> None:
    state_before = {"has_conflicts": False, "mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(conflicts=True)):
        events1, new_state = poller.poll(state_before, {"id": "21803"})
    assert any(e["event"] == "conflicts_appeared" for e in events1)
    # Same data again — no new event
    with mock.patch.object(poller, "_fetch", return_value=_mr(conflicts=True)):
        events2, _ = poller.poll(new_state, {"id": "21803"})
    assert all(e["event"] != "conflicts_appeared" for e in events2)


# ---------------------------------------------------------------------------
# #463 — `conflicts_appeared` announced a standing conflict once per hour.
#
# The rising-edge guard was already there. What was not there is the knowledge
# that `has_conflicts` is only *computed* when GitLab's mergeability check has
# settled. Every push to the target branch flips the MR to
# `cannot_be_merged_recheck` with `has_conflicts: false` — the latch drops, and
# the next settled poll re-arms it. Four pushes to master, four "appeared".
# ---------------------------------------------------------------------------

def test_a_standing_conflict_is_announced_once_across_recheck_cycles() -> None:
    """The #463 repro: nothing was resolved, nothing was re-pushed to the MR."""
    events, _ = _drive([
        _mr(conflicts=True),
        _rechecking(),
        _mr(conflicts=True),
        _rechecking(),
        _mr(conflicts=True),
    ])
    assert [e["event"] for e in events].count("conflicts_appeared") == 1


def test_a_resolved_conflict_that_returns_is_announced_again() -> None:
    """The re-arm. Suppressing forever would trade a noisy report for a silent
    omission, which is the strictly worse defect."""
    events, _ = _drive([
        _mr(conflicts=True),                        # appears
        _mr(conflicts=False),                       # settled clean — resolved
        _rechecking(),                              # someone pushes to master
        _mr(conflicts=True),                        # genuinely conflicted again
    ])
    assert [e["event"] for e in events].count("conflicts_appeared") == 2


def test_an_unsettled_check_carries_the_known_conflict_forward() -> None:
    """State, not just emission: a poller that records "clean" mid-recheck has
    already forgotten the conflict, whatever it did or did not emit."""
    _, state = _drive([_mr(conflicts=True), _rechecking()])
    assert state["has_conflicts"] is True


def test_an_unsettled_check_does_not_invent_a_conflict() -> None:
    """The other direction: `unchecked` on a clean MR stays clean."""
    _, state = _drive([_mr(conflicts=False), _mr(conflicts=False, merge_status="unchecked")])
    assert state["has_conflicts"] is False


def test_a_missing_merge_status_still_trusts_has_conflicts() -> None:
    """An API response without the field is not evidence of an unsettled check.
    Absent means we have no reason to distrust `has_conflicts`, so it is used."""
    events, state = _drive([_mr(conflicts=True, merge_status="")])
    assert any(e["event"] == "conflicts_appeared" for e in events)
    assert state["has_conflicts"] is True


# ---------------------------------------------------------------------------
# #465 — `conflicts_appeared` on an MR with no diff, where nothing can conflict.
#
# `has_conflicts` is not a conflict field. GitLab's own API entity exposes it as
# an alias for `cannot_be_merged?` and documents the three things that sets:
#
#     # #cannot_be_merged? is generally indicative of conflicts, and is set via
#     #   MergeRequests::MergeabilityCheckService. However, it can also indicate
#     #   that either #has_no_commits? or #branch_missing? are true.
#     expose :cannot_be_merged?, as: :has_conflicts
#
# So the false-positive class is exactly "no diff", and the discriminator is the
# diff, not the block reason. `detailed_merge_status` cannot serve as the gate:
# it reports only the *first* failing check, and conflict is dead last in
# `MergeRequest.all_mergeability_checks` (draft is second), so a conflicted
# draft reports `draft_status`. Gating on it would drop real conflicts.
# ---------------------------------------------------------------------------

def _empty_draft_mr():
    """!33223 as observed in #465: opened seconds earlier, zero commits.

    `sha: None` and a `diff_refs` with no base/head — there is no diff at all.
    GitLab still reports `has_conflicts: True` because `merge_status` is
    `cannot_be_merged`, and names the block `draft_status`.
    """
    body = _mr(conflicts=True, merge_status="cannot_be_merged")
    body["draft"] = True
    body["detailed_merge_status"] = "draft_status"
    body["sha"] = None
    body["diff_refs"] = {"base_sha": None, "head_sha": None, "start_sha": "e6f0bbcc"}
    return body


def _empty_nondraft_mr():
    """!33194, live: *not* a draft, zero commits, zero changes.

    The source branch is the merge base, so `base_sha == head_sha`. GitLab names
    this one `commits_status` — "source branch exists and contains commits" —
    and still sets `has_conflicts: True`. Proof the false positive is not a
    draft-only phenomenon, which is what rules out an allow-list keyed on
    `draft_status`.
    """
    sha = "5cd635d275ec51592809a5442b56dd73492a024b"
    body = _mr(conflicts=True, merge_status="cannot_be_merged")
    body["detailed_merge_status"] = "commits_status"
    body["sha"] = sha
    body["diff_refs"] = {"base_sha": sha, "head_sha": sha, "start_sha": sha}
    return body


def _really_conflicted_mr(detailed="conflict", draft=False):
    """!19509, live: 20 commits, 603+ changed files, genuinely conflicted."""
    body = _mr(conflicts=True, merge_status="cannot_be_merged")
    body["draft"] = draft
    body["detailed_merge_status"] = detailed
    body["sha"] = "288f40e19482f216c3adc9dc3a83fb5a1935fb11"
    body["diff_refs"] = {
        "base_sha": "863f7d48c32e1969c4b19a8edcb6836623b34fdc",
        "head_sha": "288f40e19482f216c3adc9dc3a83fb5a1935fb11",
        "start_sha": "9e3198b443765e161726319d41b9ab6e2c19236d",
    }
    return body


def test_an_empty_mr_does_not_announce_a_conflict() -> None:
    """The #465 repro: no commits, no diff, nothing that can conflict."""
    events, _ = _drive([_empty_draft_mr()])
    assert all(e["event"] != "conflicts_appeared" for e in events)


def test_an_empty_mr_does_not_latch_a_conflict_into_state() -> None:
    """Emission is half of it. A poller that records True has already decided
    the MR is conflicted, and the next real signal reads as "no change"."""
    _, state = _drive([_empty_draft_mr()])
    assert state["has_conflicts"] is False


def test_an_empty_non_draft_mr_does_not_announce_a_conflict() -> None:
    """`base_sha == head_sha`: the source branch is the merge base. Not a draft,
    so no allow-list of draft-ish block reasons would have caught it."""
    events, _ = _drive([_empty_nondraft_mr()])
    assert all(e["event"] != "conflicts_appeared" for e in events)


def test_an_empty_mr_that_gets_commits_and_conflicts_is_announced() -> None:
    """The path that matters: suppressing the empty state must not consume the
    rising edge the real conflict arrives on."""
    events, _ = _drive([_empty_draft_mr(), _really_conflicted_mr()])
    assert [e["event"] for e in events].count("conflicts_appeared") == 1


# ---- mirror direction: real conflicts must still be reported ---------------

def test_a_genuine_conflict_on_a_settled_mr_is_still_announced() -> None:
    events, state = _drive([_really_conflicted_mr()])
    assert any(e["event"] == "conflicts_appeared" for e in events)
    assert state["has_conflicts"] is True


def test_a_conflicted_draft_is_announced_though_gitlab_names_the_block_draft() -> None:
    """`detailed_merge_status` reports the first failing check and draft is
    checked before conflict, so a conflicted draft reads `draft_status`. This is
    the test that a `detailed_merge_status == "conflict"` gate would fail."""
    events, _ = _drive([_really_conflicted_mr(detailed="draft_status", draft=True)])
    assert any(e["event"] == "conflicts_appeared" for e in events)


def test_a_conflicted_mr_blocked_on_discussions_is_still_announced() -> None:
    """Same shape, different masking check: threads are resolved before the
    conflict check runs, so the reason field says `discussions_not_resolved`."""
    events, _ = _drive([_really_conflicted_mr(detailed="discussions_not_resolved")])
    assert any(e["event"] == "conflicts_appeared" for e in events)


def test_a_conflict_is_announced_when_the_payload_carries_no_diff_fields() -> None:
    """Absence of `sha`/`diff_refs` is not evidence of an empty MR. Only
    positive evidence of no diff suppresses; otherwise `has_conflicts` stands."""
    events, _ = _drive([_mr(conflicts=True)])
    assert any(e["event"] == "conflicts_appeared" for e in events)


# ---------------------------------------------------------------------------
# #463 control — `pipeline_failed` is the edge-triggered sibling conflicts are
# being made to match. These pin that it was not disturbed on the way past.
# ---------------------------------------------------------------------------

def test_a_pipeline_that_stays_failed_is_announced_once() -> None:
    events, _ = _drive([
        _mr(pipeline_status="failed"),
        _mr(pipeline_status="failed"),
        _mr(pipeline_status="failed"),
    ])
    assert [e["event"] for e in events].count("pipeline_failed") == 1


def test_a_pipeline_that_fails_again_after_going_green_is_announced_again() -> None:
    events, _ = _drive([
        _mr(pipeline_status="failed"),
        _mr(pipeline_status="success"),
        _mr(pipeline_status="failed"),
    ])
    assert [e["event"] for e in events].count("pipeline_failed") == 2


def test_is_terminal_when_merged() -> None:
    assert poller.is_terminal({"mr_state": "merged"}) is True


def test_is_terminal_when_closed() -> None:
    assert poller.is_terminal({"mr_state": "closed"}) is True


def test_is_not_terminal_when_open() -> None:
    assert poller.is_terminal({"mr_state": "opened"}) is False


def test_fetch_failure_returns_no_events() -> None:
    with mock.patch.object(poller, "_fetch", return_value=None):
        events, new_state = poller.poll({"x": 1}, {"id": "21803"})
    assert events == []
    assert new_state == {"x": 1}  # state preserved on transient failure


def test_first_poll_records_notes_count_without_event() -> None:
    """First poll on empty state must NOT fire comment_added — just baseline."""
    with mock.patch.object(poller, "_fetch", return_value=_mr(user_notes_count=3)):
        events, new_state = poller.poll({}, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)
    assert new_state["notes_count"] == 3


def test_notes_count_increase_emits_comment_added() -> None:
    state = {"notes_count": 1, "mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(user_notes_count=2)):
        events, _ = poller.poll(state, {"id": "21803"})
    matches = [e for e in events if e["event"] == "comment_added"]
    assert len(matches) == 1
    assert matches[0]["payload"]["new_count"] == 1


def test_notes_count_unchanged_no_event() -> None:
    state = {"notes_count": 2, "mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(user_notes_count=2)):
        events, _ = poller.poll(state, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)


def test_glab_helper_imported_from_mr_op() -> None:
    """The poller must reuse _glab_api from presets/gitlab/mr.py."""
    assert poller._glab_api_cli.__module__ == "gitlab_mr_op"


def test_missing_user_notes_count_does_not_lock_baseline_at_zero() -> None:
    """Absent field keeps notes_count=None so a later real value can still baseline."""
    mr_no_field = {
        "iid": 21803, "title": "x", "state": "opened", "has_conflicts": False,
        "head_pipeline": {"id": "9", "status": "running"},
        "web_url": "https://example.com/mr/21803",
        # user_notes_count intentionally absent
    }
    with mock.patch.object(poller, "_fetch", return_value=mr_no_field):
        events, new_state = poller.poll({}, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)
    assert new_state["notes_count"] is None


def test_notes_count_field_disappearing_skips_event() -> None:
    """If notes_count drops to None on a later poll, no comparison, no event."""
    state = {"notes_count": 5, "mr_state": "opened", "pipeline_status": "running"}
    mr_no_field = {
        "iid": 21803, "title": "x", "state": "opened", "has_conflicts": False,
        "head_pipeline": {"id": "9", "status": "running"},
        "web_url": "https://example.com/mr/21803",
    }
    with mock.patch.object(poller, "_fetch", return_value=mr_no_field):
        events, _ = poller.poll(state, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)


# ---------------------------------------------------------------------------
# #435 — an event could not be understood without a call back.
#
# Four of six events in a live radar session triggered a `gl-mr:<iid>:status`
# confirm purely to learn the branch and the current MR state, and every one of
# those confirms returned data the poller had held in memory ~20s earlier. The
# poller already fetches the whole MR; the snapshot rides along.
#
# The hazard the fields are shaped against is this repository's house defect: a
# value that looks authoritative while being an artefact of when the tool
# happened to look. So the snapshot is `observed_`-prefixed at every read site
# and always carries `observed_at`, the moment the API was read.
# ---------------------------------------------------------------------------

SNAPSHOT_KEYS = {
    "observed_at",
    "observed_mr_state",
    "observed_pipeline_status",
    "observed_pipeline_id",
    # #537. Not an MR fact but a fact about the *read*: whether this poll could
    # tell the head pipeline apart from the one the previous poll saw. It rides
    # on every event for the reason `observed_pipeline_id` does — one meaning,
    # uniformly — and its third value, `unknown`, is the whole point.
    "observed_pipeline_identity",
    "observed_has_conflicts",
    "observed_source_branch",
    "observed_target_branch",
    "observed_head_sha",
}


def _branched(**kw):
    """An MR body carrying the branch pair and head sha a real API returns."""
    body = _mr(**kw)
    body["source_branch"] = "feat/435-event-payload"
    body["target_branch"] = "master"
    body["sha"] = "288f40e19482f216c3adc9dc3a83fb5a1935fb11"
    return body


def test_every_event_carries_the_snapshot_that_produced_it() -> None:
    """One fetch, one snapshot, on every event key the tick emits."""
    state = {"mr_state": "opened", "pipeline_status": "running", "notes_count": 0}
    body = _branched(pipeline_status="failed", state="closed", user_notes_count=3)
    with mock.patch.object(poller, "_fetch", return_value=body):
        events, _ = poller.poll(state, {"id": "21803"})
    assert {e["event"] for e in events} >= {"pipeline_failed", "closed", "comment_added"}
    for ev in events:
        missing = SNAPSHOT_KEYS - set(ev["payload"])
        assert not missing, f"{ev['event']} is missing {sorted(missing)}"


def test_the_snapshot_says_when_it_was_read() -> None:
    """`observed_at` is the whole reason the snapshot is safe to ship.

    An absolute instant, not an age: an age is computed once and is wrong from
    the next second onward, which is the exact defect this field exists to
    prevent. The consumer subtracts.
    """
    before = datetime.now(timezone.utc).replace(microsecond=0)
    with mock.patch.object(poller, "_fetch", return_value=_branched(pipeline_status="failed")):
        events, _ = poller.poll({"pipeline_status": "running"}, {"id": "21803"})
    after = datetime.now(timezone.utc)
    observed_at = events[0]["payload"]["observed_at"]
    assert observed_at.endswith("Z"), observed_at
    parsed = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before <= parsed <= after, f"{observed_at} not within [{before}, {after}]"


def test_a_merge_is_tied_to_the_pipeline_that_permitted_it() -> None:
    """The gap named in #435: `merged` carried no pipeline id at all, so a merge
    could not be attributed to the pipeline that let it through."""
    state = {"mr_state": "opened", "pipeline_status": "success"}
    body = _branched(state="merged", pipeline_status="success", pipeline_id="154253")
    with mock.patch.object(poller, "_fetch", return_value=body):
        events, _ = poller.poll(state, {"id": "21803"})
    merged = next(e for e in events if e["event"] == "merged")
    assert merged["payload"]["observed_pipeline_id"] == "154253"


def test_the_branch_pair_rides_along_though_it_is_not_in_state() -> None:
    """`source_branch`/`target_branch` are in the fetched body and retained
    nowhere, so before #435 the only way to learn a branch name was a second
    call for data the poller had just thrown away."""
    with mock.patch.object(poller, "_fetch", return_value=_branched(pipeline_status="failed")):
        events, new_state = poller.poll({"pipeline_status": "running"}, {"id": "21803"})
    payload = events[0]["payload"]
    assert payload["observed_source_branch"] == "feat/435-event-payload"
    assert payload["observed_target_branch"] == "master"
    assert payload["observed_head_sha"] == "288f40e19482f216c3adc9dc3a83fb5a1935fb11"
    assert "source_branch" not in new_state, "the snapshot is built from the fetch, not from state"


def test_the_snapshot_reports_the_corrected_conflict_flag_not_the_raw_field() -> None:
    """`has_conflicts` is an alias for `cannot_be_merged?` and over-reports on an
    MR with no diff (#465). The poller already knows better; shipping the raw
    field would re-export a false positive the poller had just suppressed."""
    with mock.patch.object(poller, "_fetch", return_value=_empty_nondraft_mr()):
        events, _ = poller.poll({"pipeline_status": "pending"}, {"id": "21803"})
    assert events, "expected at least the pipeline transition"
    assert all(e["payload"]["observed_has_conflicts"] is False for e in events)


def test_an_unsettled_check_ships_the_carried_forward_answer() -> None:
    """Mid-recheck `has_conflicts` reads False on a conflicted MR (#463). The
    snapshot must carry the last settled answer, not the not-yet-computed one."""
    state = {"has_conflicts": True, "mr_state": "opened", "pipeline_status": "running"}
    body = _rechecking()
    body["state"] = "merged"
    with mock.patch.object(poller, "_fetch", return_value=body):
        events, _ = poller.poll(state, {"id": "21803"})
    merged = next(e for e in events if e["event"] == "merged")
    assert merged["payload"]["observed_has_conflicts"] is True


def test_the_snapshot_does_not_ship_the_whole_mr() -> None:
    """Bounded on purpose. The fetched body is kilobytes; what rides along is
    the eight fields that answered the confirms, and nothing that would grow
    with the MR."""
    body = _branched(pipeline_status="failed", user_notes_count=17)
    body["description"] = "x" * 5000
    body["author"] = {"username": "fdavid"}
    body["labels"] = ["a", "b"]
    with mock.patch.object(poller, "_fetch", return_value=body):
        events, _ = poller.poll({"pipeline_status": "running"}, {"id": "21803"})
    payload = events[0]["payload"]
    # The three `observed_failed_job*` keys are the deliberate #509 addition to
    # this pinned set — they ride on `pipeline_failed` and nowhere else.
    assert set(payload) == SNAPSHOT_KEYS | FAILED_JOB_KEYS | {"pipeline_id", "url", "title"}
    for banned in ("description", "author", "labels", "notes_count", "merge_status",
                   "detailed_merge_status", "diff_refs", "observed_notes_count",
                   "observed_merge_status", "jobs", "failed_jobs", "trace"):
        assert banned not in payload


def test_the_snapshot_is_flat_so_a_string_attribute_bridge_can_render_it() -> None:
    """`notifiers/claude-channel` turns each payload key into a string XML
    attribute via `String(v)`. A nested object renders `[object Object]` — the
    snapshot would be invisible on the one surface #435 was reported from."""
    with mock.patch.object(poller, "_fetch", return_value=_branched(pipeline_status="failed")):
        events, _ = poller.poll({"pipeline_status": "running"}, {"id": "21803"})
    payload = events[0]["payload"]
    assert SNAPSHOT_KEYS <= set(payload)
    for k in SNAPSHOT_KEYS:
        assert not isinstance(payload[k], (dict, list)), f"{k} is not a scalar"


def test_the_existing_top_level_payload_keys_did_not_move() -> None:
    """The #439/#464 invariant: no existing field added, removed or retyped.

    `merged` in particular gains no *top-level* `pipeline_id` — `radar.drift()`
    reads exactly that key to decide an event is stale history, and a merge event
    joining that comparison would change the board for a fact nobody reported.
    """
    state = {"mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_branched(pipeline_status="failed")):
        failed, _ = poller.poll(dict(state), {"id": "21803"})
    with mock.patch.object(poller, "_fetch", return_value=_branched(state="merged")):
        merged, _ = poller.poll(dict(state), {"id": "21803"})
    fp = failed[0]["payload"]
    assert (fp["pipeline_id"], fp["url"], fp["title"]) == (
        "9", "https://example.com/mr/21803", "feat: do the thing")
    mp = next(e for e in merged if e["event"] == "merged")["payload"]
    assert mp["url"] == "https://example.com/mr/21803"
    assert "pipeline_id" not in mp


# ---------------------------------------------------------------------------
# #509 — `pipeline_failed` names the jobs that broke.
#
# The event said "pipeline 154253 failed" and nothing else, so identifying the
# actual failure cost the consumer `gl-mr` -> `gl-pipeline:<id>:failed` ->
# `gl-job`: three round-trips for one string, on the one occasion a radar
# session is doing work and can least afford to spend context.
#
# This is the only field set in the payload that is NOT free. It buys itself
# with one `?scope[]=failed` job lookup, and the budget is bounded by *where*
# the call is made: inside the `pipeline_status == "failed"` transition branch,
# which is edge-triggered, so a pipeline that stays red is looked up once.
#
# The hazard is this repository's house defect pointing at a new surface: if
# the lookup fails, times out or returns junk, the event must not report that
# nothing failed. Three states — named jobs, none recorded, could not tell.
# ---------------------------------------------------------------------------

FAILED_JOB_KEYS = {
    "observed_failed_jobs",
    "observed_failed_job_count",
    "observed_failed_jobs_lookup",
}


def _job(name, jid, started, stage="unit", allow_failure=False, status="failed"):
    return {
        "id": jid, "name": name, "stage": stage, "status": status,
        "started_at": started, "allow_failure": allow_failure,
    }


def _drive_failed(jobs_response, body=None, state=None):
    """One poll that transitions into `pipeline_failed`, with the job lookup stubbed.

    `_fetch` is patched separately from `_glab_api`, so every `_glab_api` call
    counted here is the job lookup and nothing else.
    """
    body = body if body is not None else _branched(pipeline_status="failed")
    state = state if state is not None else {"mr_state": "opened", "pipeline_status": "running"}
    calls = []

    def _api(endpoint):
        calls.append(endpoint)
        return jobs_response

    with mock.patch.object(poller, "_fetch", return_value=body), \
            mock.patch.object(poller, "_glab_api", side_effect=_api):
        events, new_state = poller.poll(state, {"id": "21803"})
    return events, new_state, calls


def _failed_payload(jobs_response, **kw):
    events, _, calls = _drive_failed(jobs_response, **kw)
    ev = next(e for e in events if e["event"] == "pipeline_failed")
    return ev["payload"], calls


def test_pipeline_failed_names_the_jobs_that_broke() -> None:
    """The whole point: the failure *class* is the job name. `test_unit_dpt`
    versus `phpstan` versus `rector` tells a reader whether it is theirs."""
    payload, _ = _failed_payload([
        _job("test_unit_dpt", 6962424, "2026-07-29T11:11:18.595Z"),
        _job("test_unit_modular 1/1", 6962420, "2026-07-29T11:11:15.216Z"),
    ])
    assert payload["observed_failed_jobs"] == "test_unit_modular 1/1,test_unit_dpt"
    assert payload["observed_failed_job_count"] == "2"
    assert payload["observed_failed_jobs_lookup"] == "ok"


def test_the_failing_jobs_are_ordered_by_start_time_not_by_gitlabs_own_order() -> None:
    """GitLab returns pipeline jobs newest-id-first, i.e. the *last* failure
    first — verified live on pipeline 154599, where the API hands back
    `test_unit_dpt` (started :18.595) ahead of `test_unit_modular` (:15.216).
    Taking the response order would name the wrong end of the failure."""
    payload, _ = _failed_payload([
        _job("test_unit_dpt", 6962424, "2026-07-29T11:11:18.595Z"),
        _job("test_unit_modular 1/1", 6962420, "2026-07-29T11:11:15.216Z"),
    ])
    first = payload["observed_failed_jobs"].split(",")[0]
    assert first == "test_unit_modular 1/1"


def test_ascending_job_id_is_not_used_as_a_proxy_for_stage_order() -> None:
    """The other tempting ordering, and it is wrong. Pipeline 154527, live:
    `conformity_basic` holds job id 6953208 but ran at 05:20:21, while the
    `unit` jobs hold *higher* ids (6953222+) and ran at 05:14:10. Job ids are
    not allocated in the order stages execute, so sorting by id would claim a
    stage ran first that ran six minutes later."""
    payload, _ = _failed_payload([
        _job("test_unit_funds", 6953222, "2026-07-29T05:14:10.683Z", stage="unit"),
        _job("phpstan", 6953208, "2026-07-29T05:20:21.578Z", stage="conformity_basic"),
    ])
    assert payload["observed_failed_jobs"] == "test_unit_funds,phpstan"


def test_a_failed_lookup_does_not_claim_there_were_no_failing_jobs() -> None:
    """The house defect, in its newest available form: an absence produced by
    the tool read as an absence in the world. A timed-out or refused job lookup
    must never render as "0 jobs failed" on a pipeline that is demonstrably red.
    """
    payload, _ = _failed_payload(None)
    assert payload["observed_failed_jobs_lookup"] == "unavailable"
    assert payload["observed_failed_job_count"] == ""
    assert payload["observed_failed_job_count"] != "0"
    assert payload["observed_failed_jobs"] == ""


def test_no_recorded_failing_jobs_is_distinguishable_from_a_failed_lookup() -> None:
    """The other side of the same coin. An empty list IS an answer — GitLab
    looked and recorded nothing — and must not be flattened into the same
    reading as "could not look"."""
    payload, _ = _failed_payload([])
    assert payload["observed_failed_jobs_lookup"] == "ok"
    assert payload["observed_failed_job_count"] == "0"
    assert payload["observed_failed_jobs"] == ""


def test_a_timed_out_lookup_does_not_kill_the_tick() -> None:
    """`subprocess.TimeoutExpired` is a `SubprocessError`, not an `OSError`, so
    it used to propagate straight out of `poll()`. One `_fetch` could survive
    that; a second call on the failure path could not. It degrades to the same
    reported "could not tell" as every other lookup failure — not to silence,
    and not to a zero."""
    import subprocess as _sp

    def _boom(_endpoint, timeout=10):
        raise _sp.TimeoutExpired(cmd="glab", timeout=timeout)

    with mock.patch.object(poller, "_fetch", return_value=_branched(pipeline_status="failed")), \
            mock.patch.object(poller, "_glab_api", _REAL_GLAB_API), \
            mock.patch.object(poller, "_glab_api_cli", side_effect=_boom):
        events, _ = poller.poll({"mr_state": "opened", "pipeline_status": "running"},
                                {"id": "21803"})
    payload = next(e for e in events if e["event"] == "pipeline_failed")["payload"]
    assert payload["observed_failed_jobs_lookup"] == "unavailable"
    assert payload["observed_failed_job_count"] == ""


def test_a_malformed_lookup_response_is_reported_as_unavailable() -> None:
    """A dict where a list was expected is not zero jobs either."""
    payload, _ = _failed_payload({"message": "404 Not Found"})
    assert payload["observed_failed_jobs_lookup"] == "unavailable"
    assert payload["observed_failed_job_count"] == ""


def test_a_missing_pipeline_id_does_not_produce_a_lookup_or_a_false_zero() -> None:
    """No id, no endpoint to call. Reporting "0 failing jobs" because there was
    nothing to ask would be the same invention with no request involved."""
    body = _branched(pipeline_status="failed", pipeline_id="")
    body["head_pipeline"] = {"status": "failed"}
    payload, calls = _failed_payload([], body=body)
    assert calls == []
    assert payload["observed_failed_jobs_lookup"] == "unavailable"
    assert payload["observed_failed_job_count"] == ""


def test_allow_failure_jobs_are_not_named_as_the_reason_the_pipeline_is_red() -> None:
    """Pipeline 154527, live: `test_unit_full_with_coverage` failed with
    `allow_failure: true`, so it is not why the pipeline is red — the two
    `unit` jobs are. Naming it would send the reader to the wrong log, and it
    sorts *first* by every ordering, so it would be the name they saw."""
    payload, _ = _failed_payload([
        _job("test_unit_full_with_coverage", 6953208, "2026-07-29T05:10:00.000Z",
             stage="conformity_basic", allow_failure=True),
        _job("test_unit_funds", 6953222, "2026-07-29T05:14:10.683Z"),
        _job("test_unit_exco", 6953223, "2026-07-29T05:14:10.929Z"),
    ])
    assert "full_with_coverage" not in payload["observed_failed_jobs"]
    assert payload["observed_failed_jobs"] == "test_unit_funds,test_unit_exco"
    assert payload["observed_failed_job_count"] == "2"


def test_only_failed_jobs_are_named_even_if_the_scope_filter_is_ignored() -> None:
    """`?scope[]=failed` is a request, not a guarantee. A proxy or an older
    GitLab that returns the whole board must not turn every green job into a
    reported failure."""
    payload, _ = _failed_payload([
        _job("build", 1, "2026-07-29T05:00:00.000Z", status="success"),
        _job("rector", 2, "2026-07-29T05:01:00.000Z", status="failed"),
        _job("deploy", 3, "2026-07-29T05:02:00.000Z", status="manual"),
    ])
    assert payload["observed_failed_jobs"] == "rector"
    assert payload["observed_failed_job_count"] == "1"


def test_the_job_list_is_capped_and_says_how_many_it_left_out() -> None:
    """A fan-out of parallel unit jobs puts eight names on the wire — observed
    live on pipeline 154533. The payload is capped, and the truncation is
    visible *inside the joined string*, because a surface that renders only
    this one attribute would otherwise read five as the whole story."""
    jobs = [_job(f"test_unit_{i}", 6953910 + i, f"2026-07-29T06:03:{30 + i}.000Z")
            for i in range(8)]
    payload, _ = _failed_payload(jobs)
    names = payload["observed_failed_jobs"]
    assert names == "test_unit_0,test_unit_1,test_unit_2,test_unit_3,test_unit_4,+3 more"
    assert payload["observed_failed_job_count"] == "8"


def test_a_retried_job_is_named_once() -> None:
    """A retry keeps the name and takes a new id, so both attempts come back
    failed. The reader wants the set of broken things, not the attempt count."""
    payload, _ = _failed_payload([
        _job("rector", 6953208, "2026-07-29T05:10:00.000Z"),
        _job("rector", 6953999, "2026-07-29T05:30:00.000Z"),
    ])
    assert payload["observed_failed_jobs"] == "rector"


def test_a_job_that_never_started_is_still_named_and_sorts_last() -> None:
    """`stuck_or_timeout_failure` / `scheduler_failure` leave `started_at` null.
    Dropping it would lose a real failure; sorting it first would claim it ran
    before jobs that demonstrably did."""
    payload, _ = _failed_payload([
        _job("stuck_job", 6953300, None),
        _job("rector", 6953208, "2026-07-29T05:10:00.000Z"),
    ])
    assert payload["observed_failed_jobs"] == "rector,stuck_job"


def test_the_failing_job_lookup_happens_once_per_red_streak_not_once_per_tick() -> None:
    """The budget claim, pinned. `pipeline_failed` is edge-triggered and the
    lookup lives inside that branch, so three polls of a standing red pipeline
    cost one request, not three."""
    state = {"mr_state": "opened", "pipeline_status": "running"}
    calls = []

    def _api(endpoint):
        calls.append(endpoint)
        return [_job("rector", 1, "2026-07-29T05:10:00.000Z")]

    with mock.patch.object(poller, "_glab_api", side_effect=_api):
        for _ in range(3):
            with mock.patch.object(poller, "_fetch",
                                   return_value=_branched(pipeline_status="failed")):
                _, state = poller.poll(state, {"id": "21803"})
    assert len(calls) == 1, calls
    assert "scope" in calls[0] and "failed" in calls[0]
    assert "/jobs" in calls[0]


def test_a_green_or_merged_tick_costs_no_extra_request() -> None:
    """Nothing on the common path pays for this feature."""
    for body in (_branched(pipeline_status="success"),
                 _branched(state="merged", pipeline_status="success"),
                 _branched(pipeline_status="running", user_notes_count=9)):
        _, _, calls = _drive_failed(
            [], body=body,
            state={"mr_state": "opened", "pipeline_status": "pending", "notes_count": 1},
        )
        assert calls == [], f"{body['state']}/{body['head_pipeline']['status']} called {calls}"
    # Positive control. Without it this test passes on a poller that never
    # looks anything up, which is the "would it pass if the code did nothing"
    # bar — it would, and then it pins nothing.
    _, _, red_calls = _drive_failed([_job("rector", 1, "2026-07-29T05:10:00.000Z")])
    assert len(red_calls) == 1, red_calls


def test_the_failing_job_fields_ride_only_on_pipeline_failed() -> None:
    """`merged` has no failing-job concept, and three empty attributes on every
    event is wire noise that also invites a consumer to read a blank as a fact.
    The keys exist exactly where a lookup happened."""
    state = {"mr_state": "opened", "pipeline_status": "running", "notes_count": 0}
    body = _branched(pipeline_status="failed", state="closed", user_notes_count=3)
    events, _, _ = _drive_failed([_job("rector", 1, "2026-07-29T05:10:00.000Z")],
                                 body=body, state=state)
    by_key = {e["event"]: e["payload"] for e in events}
    assert FAILED_JOB_KEYS <= set(by_key["pipeline_failed"])
    for key in ("closed", "comment_added"):
        assert not (FAILED_JOB_KEYS & set(by_key[key])), key


def test_the_failing_job_fields_are_flat_scalars() -> None:
    """Same constraint as the #435 snapshot: `notifiers/claude-channel` renders
    each payload key as an XML string attribute via `String(v)`, so a list of
    job names would arrive as `[object Object]` — invisible on the surface this
    feature exists to serve. The joined string is the encoding, chosen for that."""
    payload, _ = _failed_payload([
        _job("rector", 1, "2026-07-29T05:10:00.000Z"),
        _job("phpstan", 2, "2026-07-29T05:11:00.000Z"),
    ])
    for key in FAILED_JOB_KEYS:
        assert isinstance(payload[key], str), f"{key} is {type(payload[key])}"


def test_the_failing_job_fields_are_observed_prefixed_and_dated() -> None:
    """A job list read at poll time is a snapshot like everything else, and the
    tense has to survive to the read site — the #508 constraint, unchanged."""
    payload, _ = _failed_payload([_job("rector", 1, "2026-07-29T05:10:00.000Z")])
    assert FAILED_JOB_KEYS <= set(payload), sorted(FAILED_JOB_KEYS - set(payload))
    for key in FAILED_JOB_KEYS:
        assert key.startswith(poller.SNAPSHOT_PREFIX), key
    assert payload["observed_at"]


def test_the_lookup_does_not_ship_job_ids_urls_or_traces() -> None:
    """Bounded on purpose: the name is the failure class, which is all #509
    asked for. Ids and URLs are reconstructible from `observed_pipeline_id`."""
    payload, _ = _failed_payload([
        dict(_job("rector", 6953208, "2026-07-29T05:10:00.000Z"),
             web_url="https://example.com/jobs/6953208",
             **{"runner": {"id": 7}}),
    ])
    assert set(payload) == SNAPSHOT_KEYS | FAILED_JOB_KEYS | {"pipeline_id", "url", "title"}
    for banned in ("jobs", "failed_jobs", "trace", "observed_failed_job_ids",
                   "observed_failed_job_urls", "runner", "web_url"):
        assert banned not in payload


# ---------------------------------------------------------------------------
# #519 — `comment_added` was excluded from the default because
# `user_notes_count` was believed to count system notes. It does not.
#
# GitLab's `Note` model scopes `user` as `where(system: false)` and
# `user_notes_count` is the counter over that scope. Verified against the live
# instance (GitLab 18.11.7) on twelve merge requests, `user_notes_count` equal
# to the number of `system == false` notes on every one of them:
#
#   !19509  75 system notes,  0 human  -> user_notes_count = 0
#   !22026  20 system notes,  2 human  -> user_notes_count = 2
#   !33244  26 system notes, 16 human  -> user_notes_count = 16
#   !33265   3 system notes,  0 human  -> user_notes_count = 0
#
# So the defect the issue describes does not exist, the `/notes` call it
# proposed buys nothing, and the event's exclusion from the default rested on
# a source comment that was never checked. No API call is added for this.
# ---------------------------------------------------------------------------

def test_comment_added_costs_no_extra_request() -> None:
    """The budget decision for #519: nothing. A rising note count is answered
    by the fetch the poller already makes."""
    state = {"notes_count": 1, "mr_state": "opened", "pipeline_status": "running"}
    calls = []
    with mock.patch.object(poller, "_fetch", return_value=_mr(user_notes_count=5)), \
            mock.patch.object(poller, "_glab_api", side_effect=lambda e: calls.append(e)):
        events, _ = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "comment_added" for e in events)
    assert calls == []


def test_comment_added_is_shipped_in_the_default_only_set() -> None:
    """The resolution of #519. It was held out of the default for one stated
    reason, and that reason turned out not to be true."""
    import importlib.util as _il
    dpath = Path(__file__).parent.parent / "presets" / "watch" / "defaults.py"
    spec = _il.spec_from_file_location("watch_defaults", dpath)
    assert spec is not None and spec.loader is not None
    defaults = _il.module_from_spec(spec)
    spec.loader.exec_module(defaults)
    assert "comment_added" in defaults.DEFAULT_ONLY.split(",")


def test_the_poller_no_longer_asserts_that_user_notes_count_includes_system_notes() -> None:
    """The false claim lived in a source comment, and two issues (#417 item 3,
    #519) were filed off it without anyone re-reading the API. Leaving it in
    place would produce a third."""
    src = POLLER.read_text(encoding="utf-8")
    lowered = src.lower()
    assert "counts system notes" not in lowered
    assert "counts *all* notes including system notes" not in lowered


# ---------------------------------------------------------------------------
# #537 — the pipeline edge was computed from the status string alone.
#
# `pipeline_failed` fired on `pipeline_status != prev_pipeline_status`, and no
# pipeline identity was carried into that comparison. So a *second* pipeline
# that also ended `failed`, with no `running` tick observed in between, fired
# nothing: the previous status was already `"failed"`, the inequality was False,
# and the MR went red for a new reason in silence. Worse than a wrong value —
# there was no output at all to be suspicious of.
#
# Live on this instance while the fix was written: MR !33194 ran pipeline 154628
# to `failed`, took a push, and ran 154636 to `failed` as well. Two distinct
# reds, one status string. The ids below are that pair.
#
# **The retry case is the one that must not change, and it was settled against
# the live API rather than the docs: GitLab does not mint a new pipeline id on a
# retry.** Pipeline 154635 was caught mid-retry — job `test_unit_pavillon`
# failed as job 6966698, was retried as job 6967497, and `head_pipeline` went on
# reporting id 154635 with its status flipped back to `running`. A retry moves
# the *status* under a stable id, which is the edge the poller already computed
# correctly, so adding an id comparison cannot double-fire it. The
# characterization tests below pin that, and passed before the fix as well as
# after.
# ---------------------------------------------------------------------------

def test_a_second_failing_pipeline_is_announced_though_the_status_never_changed() -> None:
    """The defect. Two distinct pipelines, both `failed`, no `running` tick
    observed between them — and before #537 this emitted exactly one event."""
    events, _ = _drive([
        _mr(pipeline_status="failed", pipeline_id="154628"),
        _mr(pipeline_status="failed", pipeline_id="154636"),
    ])
    failed = [e for e in events if e["event"] == "pipeline_failed"]
    assert len(failed) == 2, [e["event"] for e in events]
    assert [e["payload"]["pipeline_id"] for e in failed] == ["154628", "154636"]


def test_a_second_pipeline_that_succeeds_is_announced_too() -> None:
    """The edge is the pipeline, not the colour. A green run after a green run
    is a different push having passed, and `pipeline_succeeded` is the only
    proof an automated fix worked."""
    events, _ = _drive([
        _mr(pipeline_status="success", pipeline_id="154627"),
        _mr(pipeline_status="success", pipeline_id="154630"),
    ])
    assert [e["event"] for e in events].count("pipeline_succeeded") == 2


def test_the_second_failure_looks_up_its_own_pipelines_failing_jobs() -> None:
    """The #536 tie-in, and why this is worth more than the odds suggest: an
    event that never fires is a set of job names that never arrives, for the
    pipeline a reader most needs them for — the second failure, where "same
    breakage or a new one?" is the actual question."""
    state: dict = {}
    calls: list[str] = []

    def _api(endpoint):
        calls.append(endpoint)
        return [_job("rector", 1, "2026-07-29T05:10:00.000Z")]

    with mock.patch.object(poller, "_glab_api", side_effect=_api):
        for pid in ("154628", "154636"):
            with mock.patch.object(
                poller, "_fetch",
                return_value=_mr(pipeline_status="failed", pipeline_id=pid),
            ):
                _, state = poller.poll(state, {"id": "21803"})
    assert len(calls) == 2, calls
    assert "/pipelines/154628/jobs" in calls[0]
    assert "/pipelines/154636/jobs" in calls[1]


def test_a_retried_pipeline_is_not_double_announced() -> None:
    """Characterization — green before #537 and green after.

    The live shape: one pipeline id, status `failed → running → failed`, with
    repeat polls at every step. Two red *arrivals*, so two events; the repeats
    add none and the stable id adds none. Trading a silent miss for a duplicate
    would not be a win — duplicates are what train a reader to stop reading."""
    events, _ = _drive([
        _mr(pipeline_status="failed", pipeline_id="154635"),
        _mr(pipeline_status="failed", pipeline_id="154635"),
        _mr(pipeline_status="running", pipeline_id="154635"),
        _mr(pipeline_status="running", pipeline_id="154635"),
        _mr(pipeline_status="failed", pipeline_id="154635"),
        _mr(pipeline_status="failed", pipeline_id="154635"),
    ])
    kinds = [e["event"] for e in events]
    assert kinds.count("pipeline_failed") == 2, kinds
    assert kinds.count("pipeline_running") == 1, kinds


def test_a_pipeline_sitting_red_across_many_polls_is_still_announced_once() -> None:
    """Characterization — green before #537 and green after. The edge-triggering
    is deliberate; #537 changes what the edge is computed from, not that there
    is one. A long-lived radar session must not fill with repeats."""
    events, _ = _drive([_mr(pipeline_status="failed", pipeline_id="154635")] * 10)
    assert [e["event"] for e in events].count("pipeline_failed") == 1


def test_the_snapshot_says_whether_this_is_the_pipeline_the_last_poll_saw() -> None:
    """Three states, not two — `same`, `new`, and `unknown`, on the wire."""
    events, state = _drive([_mr(pipeline_status="failed", pipeline_id="154628")])
    assert events[0]["payload"]["observed_pipeline_identity"] == "new"
    with mock.patch.object(
        poller, "_fetch", return_value=_mr(pipeline_status="running", pipeline_id="154628"),
    ):
        events, _ = poller.poll(state, {"id": "21803"})
    assert events[0]["payload"]["observed_pipeline_identity"] == "same"


def test_an_undeterminable_identity_does_not_silently_become_no_transition() -> None:
    """The house defect, in the one place this fix could reintroduce it.

    A state file written by a pre-#537 supertool carries `pipeline_status` and
    no `pipeline_id`, so the first poll after an upgrade genuinely cannot say
    whether this red is the red the last event described. That is *not* an
    answer of "same pipeline, stay quiet" — it is a third state, and it is
    announced, marked `unknown` so nobody reads the event as a fresh failure
    that was positively identified."""
    state = {"mr_state": "opened", "pipeline_status": "failed"}  # no pipeline_id
    body = _mr(pipeline_status="failed", pipeline_id="154636")
    with mock.patch.object(poller, "_fetch", return_value=body):
        events, state = poller.poll(state, {"id": "21803"})
    failed = [e for e in events if e["event"] == "pipeline_failed"]
    assert len(failed) == 1
    assert failed[0]["payload"]["observed_pipeline_identity"] == "unknown"
    # And the poll after it *can* tell, so it goes quiet again.
    with mock.patch.object(poller, "_fetch", return_value=body):
        events, _ = poller.poll(state, {"id": "21803"})
    assert events == []


def test_an_unidentifiable_pipeline_is_announced_once_not_on_every_poll() -> None:
    """The other half of that guard, and the reason `unknown` is persisted
    rather than recomputed as "fire". A payload that reports a status with no id
    is anomalous, and it must be said out loud — once. Saying it every 30
    seconds forever would be the loud failure traded for a flood, which reads
    as noise and gets muted, which is the silence again by another route."""
    events, _ = _drive([
        _mr(pipeline_status="failed", pipeline_id="154636"),
        _mr(pipeline_status="failed", pipeline_id=None),
        _mr(pipeline_status="failed", pipeline_id=None),
        _mr(pipeline_status="failed", pipeline_id=None),
    ])
    assert [(e["event"], e["payload"]["observed_pipeline_identity"]) for e in events] == [
        ("pipeline_failed", "new"),
        ("pipeline_failed", "unknown"),
    ]


def test_a_pipeline_id_that_comes_back_is_a_transition_again() -> None:
    """Re-armable, like the conflict latch, and for the same reason: the last
    *known* id is carried forward across polls that could not read one, so an
    unknown streak ends the moment the id is readable again and a genuinely new
    pipeline is still announced. The poller does not latch into silence because
    it once could not look."""
    events, _ = _drive([
        _mr(pipeline_status="failed", pipeline_id="154628"),
        _mr(pipeline_status="failed", pipeline_id=None),
        _mr(pipeline_status="failed", pipeline_id=None),
        _mr(pipeline_status="failed", pipeline_id="154636"),
    ])
    assert [(e["event"], e["payload"]["observed_pipeline_identity"]) for e in events] == [
        ("pipeline_failed", "new"),
        ("pipeline_failed", "unknown"),
        ("pipeline_failed", "new"),
    ]
    assert [e["payload"]["pipeline_id"] for e in events] == ["154628", "", "154636"]


def test_the_pipeline_identity_is_persisted_so_the_next_poll_can_compare() -> None:
    """`radar.drift()` already reads `source_state.pipeline_id`; #537 makes the
    poller read it too, and adds the verdict beside it."""
    _, state = _drive([_mr(pipeline_status="failed", pipeline_id="154628")])
    assert state["pipeline_id"] == "154628"
    assert state["pipeline_identity"] == "new"
