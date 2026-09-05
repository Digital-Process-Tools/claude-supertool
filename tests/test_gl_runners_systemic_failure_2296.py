"""gl-runners: a runner that fails every job it accepts is invisible (#2296).

Filed live: a self-hosted runner with a full disk accepted jobs and died in
`prepare_executor` within 4-10s of each one, over and over. None of the
watcher's nine existing events fire on this — `poller.py` reads fleet
membership and the pending queue, never how a job *ended*. The runner stays
`online`, never paused or removed, some jobs even complete on other runners,
and the queue never starves because GitLab keeps accepting and killing work
fast enough that nothing piles up behind it.

GitLab's own job API already carries the diagnosis: a job GitLab attributes to
the runner rather than to the pipeline's own script gets
`failure_reason: "runner_system_failure"`. This file pins two new events built
on that field — `runner_failing_systemically` when a runner crosses
`_SYSTEMIC_FAILURE_THRESHOLD` failures of that kind inside the same rolling
window `annotate_recent_work` already reads (`_THROUGHPUT_WINDOW_SECONDS`,
fetched once per tick for the `_recent_jobs` count), and
`runner_recovered_systemically` when it goes back to completing jobs with none
of them.

No new state and no new API call: the window is already fetched every tick for
throughput, so the count is derived from data the poller already has in hand,
with only the previous tick's verdict (already carried in `state["runners"]`)
needed to detect the transition.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).parent.parent
POLLER_PATH = _ROOT / "presets" / "watch" / "sources" / "gl-runners" / "poller.py"

_spec = importlib.util.spec_from_file_location("gl_runners_poller_2296", POLLER_PATH)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

runners_op = poller.runners_op


def _iso(seconds_ago: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


def _runner(rid: int, tags: list[str] | None = None, **over) -> dict:
    base = {
        "id": rid,
        "description": f"runner-{rid}",
        "tag_list": tags or ["dptools-runner-2"],
        "run_untagged": False,
        "active": True,
        "status": "online",
        "paused": False,
        "contacted_at": _iso(10),
        "job_execution_status": "idle",
        "runner_type": "project_type",
    }
    base.update(over)
    return base


def _finished_job(rid: int, seconds_ago: float, failure_reason: str | None = None) -> dict:
    """A finished job as the jobs endpoint returns it, attributed to `rid`."""
    return {
        "id": int(100000 - seconds_ago) * 10 + rid,
        "runner": {"id": rid},
        "created_at": _iso(seconds_ago + 60),
        "finished_at": _iso(seconds_ago),
        "failure_reason": failure_reason,
        "status": "failed" if failure_reason else "success",
    }


def _api_stub(runners: list[dict], pending: list[dict], running: list[dict],
              finished: list[dict]):
    def fake_api(endpoint, paginate=False, timeout=20):
        if "runners?" in endpoint:
            return (runners, None)
        if "scope[]=pending" in endpoint:
            return (pending, None)
        if "scope[]=running" in endpoint:
            return (running, None)
        # job history. `per_page=100&page=1` contains the substring "page=1"
        # a second time inside "per_page=100" itself ("page=100"[:6] ==
        # "page=1"), so matching on substring returns the fixture for every
        # page instead of page 1 alone. Read the actual trailing page number.
        page = endpoint.rsplit("page=", 1)[-1]
        if page == "1":
            return (finished, None)
        return ([], None)

    return fake_api


def _poll(monkeypatch, state, runners, pending, running, finished):
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(runners_op, "_api",
                        _api_stub(runners, pending, list(running), finished))
    return poller.poll(state, {"id": "fleet"})


def _names(events) -> list[str]:
    return [e["event"] for e in events]


def _by_name(events, name):
    return [e for e in events if e["event"] == name]


# ---------------------------------------------------------------------------
# annotate_systemic_failures — the shared counting helper
# ---------------------------------------------------------------------------

def test_annotate_counts_only_runner_system_failure_on_the_right_runner() -> None:
    runners = [_runner(1), _runner(2)]
    finished = [
        _finished_job(1, 600, failure_reason="runner_system_failure"),
        _finished_job(1, 700, failure_reason="runner_system_failure"),
        _finished_job(1, 800, failure_reason="script_failure"),  # not a runner fault
        _finished_job(2, 600, failure_reason="runner_system_failure"),
        _finished_job(2, 700, failure_reason=None),
    ]
    runners_op.annotate_systemic_failures(runners, finished)
    assert runners[0]["_systemic_failures"] == 2
    assert runners[1]["_systemic_failures"] == 1


def test_annotate_leaves_a_clean_runner_at_zero() -> None:
    runners = [_runner(5)]
    runners_op.annotate_systemic_failures(runners, [])
    assert runners[0]["_systemic_failures"] == 0


# ---------------------------------------------------------------------------
# runner_failing_systemically
# ---------------------------------------------------------------------------

def test_three_systemic_failures_in_window_fires_the_event(monkeypatch) -> None:
    rid = 7
    fleet = [_runner(rid)]

    # Tick one: clean baseline, quiet (first tick).
    first, state = _poll(monkeypatch, {}, fleet, [], [], [])
    assert first == []

    failing = [_finished_job(rid, 300, failure_reason="runner_system_failure"),
               _finished_job(rid, 400, failure_reason="runner_system_failure"),
               _finished_job(rid, 500, failure_reason="runner_system_failure")]

    events, new_state = _poll(monkeypatch, state, fleet, [], [], failing)

    assert len(_by_name(events, "runner_failing_systemically")) == 1
    event = _by_name(events, "runner_failing_systemically")[0]
    assert event["payload"]["runner_id"] == str(rid)
    assert event["payload"]["failed_for_it"] == 3
    assert new_state["runners"][str(rid)]["systemically_failing"] is True


def test_two_systemic_failures_does_not_fire(monkeypatch) -> None:
    rid = 8
    fleet = [_runner(rid)]
    _first, state = _poll(monkeypatch, {}, fleet, [], [], [])

    under_threshold = [_finished_job(rid, 300, failure_reason="runner_system_failure"),
                       _finished_job(rid, 400, failure_reason="runner_system_failure")]

    events, _new_state = _poll(monkeypatch, state, fleet, [], [], under_threshold)

    assert "runner_failing_systemically" not in _names(events)


def test_the_event_does_not_refire_every_tick_while_still_failing(monkeypatch) -> None:
    rid = 9
    fleet = [_runner(rid)]
    _first, state = _poll(monkeypatch, {}, fleet, [], [], [])

    failing = [_finished_job(rid, 300, failure_reason="runner_system_failure"),
               _finished_job(rid, 400, failure_reason="runner_system_failure"),
               _finished_job(rid, 500, failure_reason="runner_system_failure")]

    events, state = _poll(monkeypatch, state, fleet, [], [], failing)
    assert len(_by_name(events, "runner_failing_systemically")) == 1

    events_again, _state = _poll(monkeypatch, state, fleet, [], [], failing)
    assert _by_name(events_again, "runner_failing_systemically") == []


# ---------------------------------------------------------------------------
# runner_recovered_systemically
# ---------------------------------------------------------------------------

def test_recovery_fires_once_completions_resume_with_no_systemic_failures(
        monkeypatch) -> None:
    rid = 11
    fleet = [_runner(rid)]
    _first, state = _poll(monkeypatch, {}, fleet, [], [], [])

    failing = [_finished_job(rid, 300, failure_reason="runner_system_failure"),
               _finished_job(rid, 400, failure_reason="runner_system_failure"),
               _finished_job(rid, 500, failure_reason="runner_system_failure")]
    events, state = _poll(monkeypatch, state, fleet, [], [], failing)
    assert len(_by_name(events, "runner_failing_systemically")) == 1

    # The window ages the failures out and the runner completes clean work.
    recovered = [_finished_job(rid, 60, failure_reason=None)]
    events, state = _poll(monkeypatch, state, fleet, [], [], recovered)

    assert len(_by_name(events, "runner_recovered_systemically")) == 1
    assert state["runners"][str(rid)]["systemically_failing"] is False


def test_dropping_below_threshold_with_lingering_failures_is_not_recovery(
        monkeypatch) -> None:
    """Self-review finding: `recent_jobs > 0` was true of a runner that went
    from 3 systemic failures to 2 -- still below the (now non-)threshold, but
    every one of those 2 completions was itself a `runner_system_failure`.
    `runner_recovered_systemically` must not fire, and its `notify_message`
    claiming "none ending in runner_system_failure" must never be false."""
    rid = 13
    fleet = [_runner(rid)]
    _first, state = _poll(monkeypatch, {}, fleet, [], [], [])

    failing = [_finished_job(rid, 300, failure_reason="runner_system_failure"),
               _finished_job(rid, 400, failure_reason="runner_system_failure"),
               _finished_job(rid, 500, failure_reason="runner_system_failure")]
    events, state = _poll(monkeypatch, state, fleet, [], [], failing)
    assert len(_by_name(events, "runner_failing_systemically")) == 1

    # Still failing every job it completes, just two of them this tick.
    still_failing = [_finished_job(rid, 200, failure_reason="runner_system_failure"),
                     _finished_job(rid, 300, failure_reason="runner_system_failure")]
    events, state = _poll(monkeypatch, state, fleet, [], [], still_failing)

    assert "runner_recovered_systemically" not in _names(events)


def test_no_recovery_event_while_the_runner_stays_idle(monkeypatch) -> None:
    """Failures aging out of the window with no new completions at all is not
    evidence of recovery — it is silence, and this repo declines to read
    silence as an all-clear (see the module docstring on `runner_silent`).

    The per-tick `systemically_failing` verdict is derived fresh from the
    window every tick, same as `blocked`/`responsive` elsewhere in this
    module — so it is allowed to go quiet on its own once the failures age
    out. What must not happen is an EVENT claiming recovery with no evidence
    a job ran clean; a subsequent failure re-fires `runner_failing_systemically`
    from scratch rather than treating the runner as still-flagged."""
    rid = 12
    fleet = [_runner(rid)]
    _first, state = _poll(monkeypatch, {}, fleet, [], [], [])

    failing = [_finished_job(rid, 300, failure_reason="runner_system_failure"),
               _finished_job(rid, 400, failure_reason="runner_system_failure"),
               _finished_job(rid, 500, failure_reason="runner_system_failure")]
    events, state = _poll(monkeypatch, state, fleet, [], [], failing)
    assert len(_by_name(events, "runner_failing_systemically")) == 1

    events, state = _poll(monkeypatch, state, fleet, [], [], [])

    assert "runner_recovered_systemically" not in _names(events)
    assert "runner_failing_systemically" not in _names(events)
