"""`runner_silent` must gate on stranded work, not on work that merely matches.

The defect (#613): a decommissioned runner left in GitLab and replaced by a
same-tag successor satisfies both halves of the old gate forever — it is not
responsive, and jobs carrying its tags are queued. But those jobs route to the
successor and run normally, so the event is a *permanent* false alarm. A signal
that is always noise trains the reader to skim the board, which is how the one
real starvation gets missed.

Everything here is driven through `poll()` with the GitLab API stubbed, not
through the helper being changed, so a half-implementation that fixes the
helper without adopting it at the call site still fails.

The suppression tests are the RED ones. The rest are the fence: the cases that
must keep firing after the gate is narrowed, including the two tag-set
semantics that a naive "are this runner's tags covered" check gets wrong in
opposite directions.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
POLLER_PATH = _ROOT / "presets" / "watch" / "sources" / "gl-runners" / "poller.py"

_spec = importlib.util.spec_from_file_location("gl_runners_poller_613", POLLER_PATH)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

runners_op = poller.runners_op


def _iso(seconds_ago: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


def _runner(rid: int, tags: list[str], *, alive: bool = True, **over) -> dict:
    """A runner as the list+detail endpoints return it, before annotation."""
    base = {
        "id": rid,
        "description": f"runner-{rid}",
        "tag_list": tags,
        "run_untagged": False,
        "active": True,
        "status": "online" if alive else "paused",
        "paused": not alive,
        "contacted_at": _iso(10) if alive else _iso(6 * 86400),
        "job_execution_status": "idle",
    }
    base.update(over)
    return base


def _job(tags: list[str]) -> dict:
    return {"tag_list": tags, "created_at": _iso(1800), "name": "fbm_test"}


def _api_stub(runners: list[dict], pending: list[dict]):
    def fake_api(endpoint, paginate=False, timeout=20):
        if "runners?" in endpoint:
            return (runners, None)
        if "scope[]=pending" in endpoint:
            return (pending, None)
        if "scope[]=running" in endpoint:
            return ([], None)
        return ([], None)  # job history

    return fake_api


def _transition(monkeypatch, before: list[dict], after: list[dict],
                pending: list[dict]) -> list[dict]:
    """Poll a healthy fleet, then the degraded one; return the second tick's events.

    Two ticks because the first is baseline-quiet by design — announcing a
    transition that predates the watcher is history, not news.
    """
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})

    monkeypatch.setattr(runners_op, "_api", _api_stub(before, pending))
    _events, state = poller.poll({}, {"id": "fleet"})

    monkeypatch.setattr(runners_op, "_api", _api_stub(after, pending))
    events, _state = poller.poll(state, {"id": "fleet"})
    return events


def _silent(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "runner_silent"]


# ---------------------------------------------------------------------------
# suppression — the reported defect
# ---------------------------------------------------------------------------

def test_a_runner_replaced_by_a_same_tag_successor_is_not_reported_wedged(
        monkeypatch) -> None:
    """The filed case. Runner 29 is dead six days; runner 32 carries the same
    tags and is online; the pending job runs on 32. Nothing is stuck, so
    nothing is reported — for as long as the stale record sits there."""
    tags = ["docker", "dptools-runner-7"]
    successor = _runner(32, tags, description="dptools-runner-7 V2")
    before = [_runner(29, tags, description="dptools-runner-7"), successor]
    after = [_runner(29, tags, description="dptools-runner-7", alive=False), successor]

    events = _transition(monkeypatch, before, after, [_job(tags)])

    assert _silent(events) == []


def test_coverage_is_judged_per_job_not_per_tag_the_runner_happens_to_carry(
        monkeypatch) -> None:
    """A silent runner carries tags no live runner carries — but no queued job
    asks for them. Gating on the runner's own tag set would fire here forever
    on a tag nobody uses. What matters is whether the queued work can move."""
    before = [_runner(1, ["docker", "gpu"]), _runner(2, ["docker"])]
    after = [_runner(1, ["docker", "gpu"], alive=False), _runner(2, ["docker"])]

    events = _transition(monkeypatch, before, after, [_job(["docker"])])

    assert _silent(events) == []


# ---------------------------------------------------------------------------
# the fence — what must still fire
# ---------------------------------------------------------------------------

def test_an_exclusive_tag_with_no_live_owner_still_reports_the_wedge(
        monkeypatch) -> None:
    """The regression guard. This is the event the source exists for and the
    one the narrowed gate must not touch."""
    before = [_runner(1, ["runner-2"]), _runner(2, ["docker"])]
    after = [_runner(1, ["runner-2"], alive=False), _runner(2, ["docker"])]

    events = _transition(monkeypatch, before, after, [_job(["runner-2"])])

    assert len(_silent(events)) == 1
    assert _silent(events)[0]["payload"]["runner_id"] == "1"


def test_partial_coverage_still_fires_for_the_jobs_that_cannot_move(
        monkeypatch) -> None:
    """Two queued jobs, one of which the live runner may take. The other is
    stranded, and one stranded job is a wedge."""
    before = [_runner(1, ["docker", "gpu"]), _runner(2, ["docker"])]
    after = [_runner(1, ["docker", "gpu"], alive=False), _runner(2, ["docker"])]

    events = _transition(monkeypatch, before, after,
                         [_job(["docker"]), _job(["docker", "gpu"])])

    assert len(_silent(events)) == 1


def test_two_live_runners_splitting_the_tags_do_not_cover_one_job_needing_both(
        monkeypatch) -> None:
    """The other way a tag-wise check goes wrong. GitLab requires ONE runner to
    carry every tag a job asks for, so `docker` here and `gpu` there covers
    nothing. Reading that as coverage would silence a real starvation."""
    split = [_runner(2, ["docker"]), _runner(3, ["gpu"])]
    before = [_runner(1, ["docker", "gpu"]), *split]
    after = [_runner(1, ["docker", "gpu"], alive=False), *split]

    events = _transition(monkeypatch, before, after, [_job(["docker", "gpu"])])

    assert len(_silent(events)) == 1


def test_a_successor_that_is_itself_silent_covers_nothing(monkeypatch) -> None:
    """Suppression needs positive evidence of a live runner. Two dead runners
    sharing a tag are two dead runners, not redundancy."""
    tags = ["docker", "dptools-runner-7"]
    before = [_runner(29, tags), _runner(32, tags, alive=False)]
    after = [_runner(29, tags, alive=False), _runner(32, tags, alive=False)]

    events = _transition(monkeypatch, before, after, [_job(tags)])

    assert "29" in {e["payload"]["runner_id"] for e in _silent(events)}


def test_an_unknown_queue_still_refuses_to_judge_either_way(monkeypatch) -> None:
    """The existing decline, guarded against the new gate quietly answering
    for it: with the pending read failed there is no evidence of coverage and
    no evidence of a wedge, so the tick says nothing rather than all-clear."""
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    fleet_before = [_runner(1, ["runner-2"])]
    fleet_after = [_runner(1, ["runner-2"], alive=False)]

    monkeypatch.setattr(runners_op, "_api", _api_stub(fleet_before, [_job(["runner-2"])]))
    _events, state = poller.poll({}, {"id": "fleet"})

    def queue_down(endpoint, paginate=False, timeout=20):
        if "scope[]=pending" in endpoint:
            return (None, "ERROR: 500")
        return _api_stub(fleet_after, [])(endpoint, paginate, timeout)

    monkeypatch.setattr(runners_op, "_api", queue_down)
    events, new_state = poller.poll(state, {"id": "fleet"})

    assert _silent(events) == []
    assert new_state["queue_known"] is False


def test_one_live_runner_out_of_several_is_enough_to_cover_a_job(monkeypatch) -> None:
    """Mutation guard on the `any`. A fleet where most live runners cannot take
    the job and one can is a covered job — reading it as `all` would restore
    the false alarm on every fleet with more than one machine in it."""
    bystanders = [_runner(3, ["windows"]), _runner(4, ["macos"])]
    before = [_runner(1, ["docker"]), _runner(2, ["docker"]), *bystanders]
    after = [_runner(1, ["docker"], alive=False), _runner(2, ["docker"]), *bystanders]

    events = _transition(monkeypatch, before, after, [_job(["docker"])])

    assert _silent(events) == []


def test_the_suppressed_runner_is_still_named_in_state_just_not_notified(
        monkeypatch) -> None:
    """The deliberate middle: no event, but "why is this one not firing" has an
    answer without going back to GitLab for it."""
    tags = ["docker", "dptools-runner-7"]
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    fleet = [_runner(29, tags, alive=False),
             _runner(32, tags, description="dptools-runner-7 V2")]
    monkeypatch.setattr(runners_op, "_api", _api_stub(fleet, [_job(tags)]))

    _events, state = poller.poll({}, {"id": "fleet"})

    assert state["superseded"] == ["runner-29"]
    assert state["runners"]["29"]["blocked"] is False
    assert state["runners"]["32"]["superseded"] is False


# ---------------------------------------------------------------------------
# what the event says once it fires
# ---------------------------------------------------------------------------

def test_the_event_reports_the_stranded_count_not_the_matching_count(
        monkeypatch) -> None:
    """`pending_for_it` was the number a reader acted on, and after the gate
    narrows it overstates the damage: three jobs match the runner's tags, one
    of them is actually stuck. Both numbers ride on the payload, and the
    headline is the actionable one."""
    before = [_runner(1, ["docker", "gpu"]), _runner(2, ["docker"])]
    after = [_runner(1, ["docker", "gpu"], alive=False), _runner(2, ["docker"])]
    pending = [_job(["docker"]), _job(["docker"]), _job(["docker", "gpu"])]

    events = _transition(monkeypatch, before, after, pending)

    event = _silent(events)[0]
    assert event["payload"]["pending_for_it"] == 3
    assert event["payload"]["stranded_for_it"] == 1
    assert "1 job(s)" in event["notify_title"]


# ---------------------------------------------------------------------------
# the shared helper, so the op and the watcher cannot drift apart
# ---------------------------------------------------------------------------

def _annotated(runner: dict) -> dict:
    runners_op.annotate_live_jobs([runner], [])
    runners_op.annotate_recent_work([runner], [])
    return runner


def test_stranded_for_counts_only_work_no_responsive_runner_may_take() -> None:
    dead = _annotated(_runner(1, ["docker", "gpu"], alive=False))
    live = _annotated(_runner(2, ["docker"]))
    pending = [_job(["docker"]), _job(["docker", "gpu"]), _job(["other"])]

    assert runners_op.waiting_for(dead, pending) == 2
    assert runners_op.stranded_for(dead, pending, [dead, live]) == 1


def test_stranded_for_refuses_an_unannotated_fleet_like_every_other_judgement() -> None:
    """Same refusal as `_is_responsive` (#533): without the annotators the
    verdict falls back to the throttled field, and a coverage check built on
    that would suppress the fleet's real wedges."""
    dead = _annotated(_runner(1, ["docker"], alive=False))
    unchecked = _runner(2, ["docker"])

    with pytest.raises(runners_op.UnannotatedFleetError):
        runners_op.stranded_for(dead, [_job(["docker"])], [dead, unchecked])
