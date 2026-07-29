"""Tests for the `gl-runners` op.

The op exists because GitLab reports a wedged runner as `online` and `idle`,
which is indistinguishable from a healthy runner between jobs. Everything here
guards one of the two ways that judgement went wrong in practice: trusting a
field GitLab throttles, and ordering job history by the wrong timestamp.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest

OP_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "runners.py"

_spec = importlib.util.spec_from_file_location("gl_runners_op", OP_PATH)
assert _spec is not None and _spec.loader is not None
runners_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runners_op)


def _iso(seconds_ago: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


def _runner(rid: int = 1, **over) -> dict:
    base = {
        "id": rid, "description": f"runner-{rid}", "tag_list": ["docker"],
        "run_untagged": False, "status": "online", "active": True,
        "paused": False, "contacted_at": _iso(10), "job_execution_status": "idle",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# liveness — the evidence ladder
# ---------------------------------------------------------------------------

def test_a_runner_that_finished_work_is_live_however_stale_its_heartbeat() -> None:
    """Completed work outranks contacted_at, which GitLab throttles. This is the
    case that made the first version fire on 6 of 6 healthy runners."""
    runner = _runner(contacted_at=_iso(3600), _recent_jobs=4)
    assert runners_op._is_responsive(runner) is True


def test_a_runner_executing_right_now_is_live_however_stale_its_heartbeat() -> None:
    runner = _runner(contacted_at=_iso(3600), job_execution_status="active")
    assert runners_op._is_responsive(runner) is True


def test_a_quiet_runner_inside_the_heartbeat_window_is_live() -> None:
    assert runners_op._is_responsive(_runner(contacted_at=_iso(600))) is True


def test_a_quiet_runner_past_the_heartbeat_window_with_no_work_is_not_live() -> None:
    runner = _runner(contacted_at=_iso(runners_op._HEARTBEAT_WARN_SECONDS + 60))
    assert runners_op._is_responsive(runner) is False


def test_the_heartbeat_window_clears_gitlabs_measured_write_throttle() -> None:
    """contacted_at was observed frozen for minutes on a runner that was working.
    A window under that granularity measures GitLab's write policy, not health."""
    assert runners_op._HEARTBEAT_WARN_SECONDS >= 1800


@pytest.mark.parametrize("field,value", [
    ("paused", True),
    ("active", False),
    ("status", "offline"),
    ("status", "stale"),
    ("status", "never_contacted"),
])
def test_an_explicitly_down_runner_is_never_rescued_by_recent_work(field, value) -> None:
    """Ordering guard: the disqualifiers are checked before the proofs, so a
    paused runner with recent history does not read as available."""
    runner = _runner(_recent_jobs=9, job_execution_status="active", **{field: value})
    assert runners_op._is_responsive(runner) is False


# ---------------------------------------------------------------------------
# throughput history
# ---------------------------------------------------------------------------

def test_history_counts_jobs_by_when_they_finished_not_when_they_started(monkeypatch) -> None:
    """The regression this file was written for.

    Job ids order by created_at, and finished_at is not monotonic with it: a
    test job created hours ago finishes after jobs created since. Scanning by
    created_at dropped exactly those, so busy runners reported zero throughput
    while they were completing work.
    """
    old_job_just_finished = {
        "id": 2, "created_at": _iso(4 * 3600), "finished_at": _iso(60),
        "runner": {"id": 7},
    }
    recent_job_still_running = {
        "id": 9, "created_at": _iso(120), "finished_at": None, "runner": {"id": 7},
    }
    pages = {1: [recent_job_still_running, old_job_just_finished], 2: []}

    def fake_api(endpoint, paginate=False, timeout=20):
        page = int(endpoint.split("&page=")[1])
        return (pages.get(page, []), None)

    monkeypatch.setattr(runners_op, "_api", fake_api)
    finished, truncated = runners_op.fetch_recent_finished()

    assert [job["id"] for job in finished] == [2]
    assert truncated is False


def test_history_ignores_jobs_that_finished_before_the_window(monkeypatch) -> None:
    stale = {"id": 1, "created_at": _iso(9000), "finished_at": _iso(9000), "runner": {"id": 7}}
    monkeypatch.setattr(runners_op, "_api",
                        lambda endpoint, paginate=False, timeout=20:
                        ([stale], None) if "page=1" in endpoint else ([], None))
    finished, _truncated = runners_op.fetch_recent_finished()
    assert finished == []


def test_hitting_the_page_cap_is_reported_not_swallowed(monkeypatch) -> None:
    """A truncated count that reads as a total is worse than no count."""
    job = {"id": 1, "created_at": _iso(60), "finished_at": _iso(30), "runner": {"id": 7}}
    monkeypatch.setattr(runners_op, "_api",
                        lambda endpoint, paginate=False, timeout=20: ([job], None))
    _finished, truncated = runners_op.fetch_recent_finished()
    assert truncated is True


def test_an_unreadable_history_costs_evidence_not_correctness(monkeypatch) -> None:
    monkeypatch.setattr(runners_op, "_api",
                        lambda endpoint, paginate=False, timeout=20: (None, "ERROR: 500"))
    finished, truncated = runners_op.fetch_recent_finished()
    assert (finished, truncated) == ([], False)


def test_annotations_attribute_work_to_the_runner_that_did_it() -> None:
    fleet = [_runner(7), _runner(8)]
    runners_op.annotate_recent_work(fleet, [
        {"runner": {"id": 7}}, {"runner": {"id": 7}}, {"runner": {"id": 8}},
        {"runner": None},
    ])
    assert [r["_recent_jobs"] for r in fleet] == [2, 1]


def test_a_runner_named_by_the_running_list_is_marked_active() -> None:
    """Two independent reads: the runner record can be a moment staler than the
    job list, and the job list naming it is proof enough."""
    fleet = [_runner(7, job_execution_status="idle", contacted_at=_iso(9999))]
    runners_op.annotate_live_jobs(fleet, [{"runner": {"id": 7}}])
    assert runners_op._is_responsive(fleet[0]) is True


# ---------------------------------------------------------------------------
# tag routing and starvation
# ---------------------------------------------------------------------------

def test_a_runner_needs_every_tag_a_job_asks_for() -> None:
    runner = _runner(tag_list=["docker"])
    assert runners_op._can_serve(runner, ["docker"]) is True
    assert runners_op._can_serve(runner, ["docker", "heavy"]) is False


def test_untagged_jobs_only_go_to_runners_that_accept_them() -> None:
    assert runners_op._can_serve(_runner(run_untagged=False), []) is False
    assert runners_op._can_serve(_runner(run_untagged=True), []) is True


def test_work_pinned_to_a_dead_runner_is_starved() -> None:
    """The finding the op exists for: an exclusive tag means no fallback."""
    dead = _runner(1, tag_list=["runner-2"], contacted_at=_iso(7200))
    alive = _runner(2, tag_list=["docker"], contacted_at=_iso(10))
    pending = [{"tag_list": ["runner-2"], "created_at": _iso(1800)}]
    assert runners_op.starved_tags([dead, alive], pending) == {"runner-2": 1}


def test_work_a_live_runner_can_take_is_never_starved() -> None:
    alive = _runner(2, tag_list=["docker"], contacted_at=_iso(10))
    pending = [{"tag_list": ["docker"], "created_at": _iso(1800)}]
    assert runners_op.starved_tags([alive], pending) == {}


def test_a_job_queued_a_moment_ago_is_scheduling_delay_not_starvation() -> None:
    """Otherwise every pipeline reports starvation for its first seconds."""
    dead = _runner(1, tag_list=["runner-2"], contacted_at=_iso(7200))
    pending = [{"tag_list": ["runner-2"], "created_at": _iso(5)}]
    assert runners_op.starved_tags([dead], pending) == {}


def test_starvation_survives_a_job_with_no_creation_timestamp() -> None:
    """Unknown age must not silently drop the job from the count — a missing
    field is not evidence that the work is fine."""
    dead = _runner(1, tag_list=["runner-2"], contacted_at=_iso(7200))
    assert runners_op.starved_tags([dead], [{"tag_list": ["runner-2"]}]) == {"runner-2": 1}


def test_waiting_for_counts_only_what_this_runner_may_take() -> None:
    runner = _runner(tag_list=["docker"])
    pending = [{"tag_list": ["docker"]}, {"tag_list": ["docker"]}, {"tag_list": ["other"]}]
    assert runners_op.waiting_for(runner, pending) == 2
