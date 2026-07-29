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
    """A runner record as the judgement sees it — after both annotators ran.

    The two `_`-prefixed keys are what `annotate_recent_work` /
    `annotate_live_jobs` leave behind. They are in the baseline because
    liveness is only defined on an annotated record; a fixture without them
    would be asking the questions below of evidence nobody gathered.
    """
    base = {
        "id": rid, "description": f"runner-{rid}", "tag_list": ["docker"],
        "run_untagged": False, "status": "online", "active": True,
        "paused": False, "contacted_at": _iso(10), "job_execution_status": "idle",
        "_recent_jobs": 0, "_live_jobs_checked": True,
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


# ---------------------------------------------------------------------------
# radar tier contract
# ---------------------------------------------------------------------------

def _api_stub(runners=None, pending=None, running=None, history=None, errors=None):
    """Route the op's endpoints to canned payloads."""
    errors = errors or {}

    def fake_api(endpoint, paginate=False, timeout=20):
        for key, err in errors.items():
            if key in endpoint:
                return (None, err)
        if "runners?" in endpoint:
            return (runners or [], None)
        if "scope[]=pending" in endpoint:
            return (pending or [], None)
        if "scope[]=running" in endpoint:
            return (running or [], None)
        return (history or [], None)

    return fake_api


def test_the_tier_names_the_runner_holding_the_queue(monkeypatch) -> None:
    dead = _runner(1, description="dptools-runner-2", tag_list=["runner-2"],
                   contacted_at=_iso(7200))
    monkeypatch.setattr(runners_op, "_api", _api_stub(
        runners=[dead], pending=[{"tag_list": ["runner-2"], "created_at": _iso(1800)}]))
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})

    lines, ok = runners_op.radar_report({})
    joined = "\n".join(lines)
    assert ok is False
    assert "1 pending job(s) cannot start" in joined
    assert "dptools-runner-2" in joined
    assert "may be this, not your code" in joined


def test_the_tier_is_healthy_when_a_live_runner_covers_the_queue(monkeypatch) -> None:
    alive = _runner(1, tag_list=["docker"], contacted_at=_iso(10))
    monkeypatch.setattr(runners_op, "_api", _api_stub(
        runners=[alive], pending=[{"tag_list": ["docker"], "created_at": _iso(1800)}]))
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})

    lines, ok = runners_op.radar_report({})
    assert ok is True
    assert "fleet ok" in "\n".join(lines)


def test_a_403_tells_the_operator_what_to_do_about_it(monkeypatch) -> None:
    """Most people who install this are not Maintainers on the project. A tier
    they registered and cannot use must say so actionably, not cry wolf."""
    monkeypatch.setattr(runners_op, "_api", _api_stub(
        errors={"runners?": "ERROR: permission denied reading runners (403)"}))
    lines, ok = runners_op.radar_report({})
    joined = "\n".join(lines)
    assert ok is False
    assert "Maintainer" in joined
    assert "radar_tiers" in joined


def test_an_empty_queue_skips_the_history_scan(monkeypatch) -> None:
    """With nothing queued there is no starvation question, so five pages of
    job history answer nothing. A registered tier should stay cheap."""
    calls: list[str] = []

    def counting_api(endpoint, paginate=False, timeout=20):
        calls.append(endpoint)
        return _api_stub(runners=[_runner(1, contacted_at=_iso(10))])(endpoint, paginate, timeout)

    monkeypatch.setattr(runners_op, "_api", counting_api)
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    _lines, ok = runners_op.radar_report({})

    assert ok is True
    assert not any("&page=" in call for call in calls)


def test_the_window_option_reaches_the_history_scan(monkeypatch) -> None:
    seen: dict[str, int] = {}
    monkeypatch.setattr(runners_op, "_api", _api_stub(
        runners=[_runner(1, tag_list=["docker"], contacted_at=_iso(10))],
        pending=[{"tag_list": ["docker"], "created_at": _iso(1800)}]))
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(runners_op, "fetch_recent_finished",
                        lambda window=None: (seen.setdefault("window", window), [])[1:] or ([], False))
    runners_op.radar_report({"window": 60})
    assert seen["window"] == 60


def test_the_tier_declares_the_options_it_understands() -> None:
    """radar validates against this set, so an option added here without being
    read, or read without being declared, is a silent no-op."""
    assert runners_op.RADAR_OPTIONS == {"window", "quiet_when_healthy"}


# ---------------------------------------------------------------------------
# the annotation precondition (#533)
# ---------------------------------------------------------------------------

def _unannotated(rid: int = 1, **over) -> dict:
    """A runner exactly as GitLab hands it over — before either annotator ran."""
    raw = _runner(rid, **over)
    raw.pop("_recent_jobs", None)
    raw.pop("_live_jobs_checked", None)
    return raw


def test_a_caller_that_skipped_annotation_is_refused_not_told_six_of_six_are_silent() -> None:
    """The failure this guard exists for.

    Without the annotators, the evidence ladder has only its weakest rung left
    — contacted_at, the field GitLab throttles — and a fleet that is working
    normally reads as wholly silent. That is not a degraded answer, it is a
    confident wrong one, and it buries the single runner that is really wedged.
    """
    fleet = [_unannotated(rid, contacted_at=_iso(2700)) for rid in range(1, 7)]
    for runner in fleet:
        with pytest.raises(runners_op.UnannotatedFleetError):
            runners_op._is_responsive(runner)


def test_the_refusal_names_the_annotation_step_that_was_missed() -> None:
    """A guard that only says 'no' costs the next caller a debugging session."""
    with pytest.raises(runners_op.UnannotatedFleetError) as excinfo:
        runners_op._is_responsive(_unannotated())
    message = str(excinfo.value)
    assert "annotate_recent_work" in message
    assert "annotate_live_jobs" in message


def test_half_annotated_is_refused_as_firmly_as_not_annotated_at_all() -> None:
    fleet = [_unannotated(1)]
    runners_op.annotate_live_jobs(fleet, [])
    with pytest.raises(runners_op.UnannotatedFleetError) as excinfo:
        runners_op._is_responsive(fleet[0])
    assert "annotate_recent_work" in str(excinfo.value)
    assert "annotate_live_jobs" not in str(excinfo.value)


def test_a_disqualified_runner_is_refused_too_rather_than_answered_by_luck() -> None:
    """Paused and offline are decided before any annotation is consulted, so an
    un-annotated caller with a mostly-paused fleet would get plausible answers
    and ship. The refusal has to be unconditional or it teaches the wrong
    lesson at exactly the moment someone is learning it.
    """
    with pytest.raises(runners_op.UnannotatedFleetError):
        runners_op._is_responsive(_unannotated(1, paused=True))


def test_the_refusal_is_never_downgraded_into_an_all_clear() -> None:
    """The opposite mistake, and the worse one: answering 'nothing is starved'
    for a fleet nobody looked at turns a false alarm into a false all-clear.
    starved_tags must decline to answer, not answer empty.
    """
    dead = _unannotated(1, tag_list=["runner-2"], contacted_at=_iso(7200))
    pending = [{"tag_list": ["runner-2"], "created_at": _iso(1800)}]
    with pytest.raises(runners_op.UnannotatedFleetError):
        runners_op.starved_tags([dead], pending)


def test_the_annotators_leave_a_mark_that_says_they_ran() -> None:
    """'Annotated, found nothing' and 'never annotated' must not be the same
    record. Zero completed jobs is a real observation; a missing key is not.
    """
    fleet = [_unannotated(1)]
    runners_op.annotate_recent_work(fleet, [])
    runners_op.annotate_live_jobs(fleet, [])
    assert fleet[0]["_recent_jobs"] == 0
    assert runners_op._is_responsive(fleet[0]) is True


def test_the_tier_does_not_report_a_live_count_it_declined_to_measure() -> None:
    """radar skips the history scan when nothing is queued — deliberately, to
    keep a registered tier cheap. That leaves it holding un-annotated records,
    so it must stop counting liveness rather than count it wrong.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(runners_op, "_api", _api_stub(
            runners=[_runner(1, contacted_at=_iso(10))]))
        monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
        lines, ok = runners_op.radar_report({})
    finally:
        monkeypatch.undo()

    joined = "\n".join(lines)
    assert ok is True
    assert "runners live" not in joined
