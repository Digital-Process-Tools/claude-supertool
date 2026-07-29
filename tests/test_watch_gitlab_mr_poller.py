"""Unit tests for the gitlab-mr poller source — state diff logic."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gitlab-mr" / "poller.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_poller", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


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
    assert set(payload) == SNAPSHOT_KEYS | {"pipeline_id", "url", "title"}
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
