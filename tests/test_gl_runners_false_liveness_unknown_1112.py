"""gl-runners: a failed running-jobs read is not an idle fleet (#1112).

Filed live on 2026-08-08. `hercule` was executing `test_unit_dv` and
`test_unit_mathematic` on a live MR pipeline; `gl-runners:queue`, read seconds
later, showed both. The watcher emitted:

    runner_liveness_unknown  runner_id=25 hercule last_seen=30m running_on_it="0"

and retracted it 69 seconds later with `running_on_it="2"`, nothing about the
world having changed in between.

**The verdict and the count share one source, and that source can fail.**
`_fetch_fleet` reads the running-jobs list, and on an API error coerces it to
`[]`:

    running = [] if err_running else (running or [])
    runners_op.annotate_live_jobs(merged, running)

`annotate_live_jobs` then stamps `_live_jobs_checked` on every runner — the mark
whose entire job is to distinguish "annotated, found nothing" from "never
annotated". With the list unread the mark is a lie, `UnannotatedFleetError`
never fires, `_is_responsive` falls through past both throughput tests onto
`contacted_at` alone — the throttled field, the exact fallback the guard exists
to prevent — and `running_on_it` counts 0 running jobs on a runner running two.

That is this repo's named defect class: an absence produced by the tool, read as
an absence in the world. The pending read four lines below already gets it right
— it returns `None`, and `poll` carries the last queue reading forward untouched
so an API blip cannot read as either starvation or its resolution. The running
read never adopted the sibling.

**The fix is the read, not the disclosure policy.** #750 decided that the
unproven queue keeps being reported with the certainty claim withdrawn, because
"it said nothing while I was blocked" is this repo's most-filed defect. That
reasoning is untouched here and the fences below pin it: a stale heartbeat over
a running list that was genuinely read and genuinely empty must still fire
`runner_liveness_unknown`. What must stop is emitting it off evidence nobody
gathered.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
POLLER_PATH = _ROOT / "presets" / "watch" / "sources" / "gl-runners" / "poller.py"

_spec = importlib.util.spec_from_file_location("gl_runners_poller_1112", POLLER_PATH)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

runners_op = poller.runners_op

_FAILED_READ = "ERROR: glab timed out reading projects/:id/jobs?scope[]=running"

# Every age below is derived from the threshold it is meant to sit clear of,
# never written as a literal. The first cut of this file used `_iso(1800)` for a
# "30 minutes stale" heartbeat, and `_HEARTBEAT_WARN_SECONDS` is 1800 — so the
# fixture landed exactly on the `age <= threshold` boundary, and the verdict was
# decided by whether the wall clock advanced by one microsecond between building
# the record and grading it. Linux and macOS advanced; one Windows leg did not,
# and three tests went red on the runner having been graded alive. A test may
# not depend on clock resolution, and a margin written as a literal beside a
# constant somebody may later change is the same trap with a longer fuse.
_CLEAR_OF_THRESHOLD = 120


def _stale_heartbeat_seconds() -> int:
    """An age unambiguously past the heartbeat threshold, whatever it is set to."""
    return runners_op._HEARTBEAT_WARN_SECONDS + _CLEAR_OF_THRESHOLD


def _queued_long_enough_seconds() -> int:
    """A pending age unambiguously past the queue-age floor."""
    return runners_op._STARVED_MIN_QUEUE_SECONDS + _CLEAR_OF_THRESHOLD


def _iso(seconds_ago: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


def _runner(rid: int, tags: list[str], **over) -> dict:
    """A runner as list+detail return it: online, idle, freshly contacted."""
    base = {
        "id": rid,
        "description": f"runner-{rid}",
        "tag_list": tags,
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


def _hercule(**over) -> dict:
    """The filed runner: GitLab says online, the heartbeat is the only demerit."""
    return _runner(25, ["dptools-runner-2"], description="hercule",
                   contacted_at=_iso(_stale_heartbeat_seconds()), **over)


def _pending_job(tags: list[str]) -> dict:
    return {"tag_list": tags, "created_at": _iso(_queued_long_enough_seconds()),
            "name": "phpstan2", "ref": "refs/merge-requests/33743/head"}


def _running_job(rid: int, name: str) -> dict:
    return {"tag_list": ["dptools-runner-2"], "created_at": _iso(600),
            "name": name, "ref": "refs/merge-requests/33743/head",
            "runner": {"id": rid}}


def _api_stub(runners: list[dict], pending: list[dict], running: list[dict],
              fail_running: bool = False):
    def fake_api(endpoint, paginate=False, timeout=20):
        if "runners?" in endpoint:
            return (runners, None)
        if "scope[]=pending" in endpoint:
            return (pending, None)
        if "scope[]=running" in endpoint:
            if fail_running:
                return (None, _FAILED_READ)
            return (running, None)
        return ([], None)  # job history

    return fake_api


def _poll(monkeypatch, state, runners, pending, running, fail_running=False):
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(
        runners_op, "_api",
        _api_stub(runners, pending, list(running), fail_running=fail_running))
    return poller.poll(state, {"id": "fleet"})


def _names(events) -> list[str]:
    return [e["event"] for e in events]


# ---------------------------------------------------------------------------
# the filed shape: the runner is executing jobs, only our read of that failed
# ---------------------------------------------------------------------------

def test_a_failed_running_read_does_not_make_a_busy_runner_unaccounted_for(
        monkeypatch) -> None:
    """`hercule`, two jobs executing on it, heartbeat 30m stale, one job queued
    behind it. Tick one reads the running list and correctly says nothing. Tick
    two changes nothing about the world — the same two jobs are still running —
    and only the running-jobs API call fails. The runner is demonstrably alive
    at both timestamps, so no liveness event may fire at either."""
    tags = ["dptools-runner-2"]
    fleet = [_hercule()]
    pending = [_pending_job(tags)]
    running = [_running_job(25, "test_unit_dv"),
               _running_job(25, "test_unit_mathematic")]

    first, state = _poll(monkeypatch, {}, fleet, pending, running)
    assert "runner_liveness_unknown" not in _names(first)

    events, _state = _poll(monkeypatch, state, fleet, pending, running,
                           fail_running=True)

    assert "runner_liveness_unknown" not in _names(events)
    assert "runner_silent" not in _names(events)


def test_a_failed_running_read_never_publishes_a_running_count(
        monkeypatch) -> None:
    """`running_on_it="0"` rode on the filed event, and it was wrong about the
    world at the moment it was emitted. Every fleet event carries that count,
    so an unread running list must not produce one on any of them — including
    the membership and paused events, which are not liveness verdicts and would
    otherwise smuggle the same fabricated zero out."""
    tags = ["dptools-runner-2"]
    pending = [_pending_job(tags)]
    running = [_running_job(25, "test_unit_dv"),
               _running_job(25, "test_unit_mathematic")]

    _first, state = _poll(monkeypatch, {}, [_hercule()], pending, running)

    # A newly joined runner and a paused flag flip on the same failed tick.
    fleet = [_hercule(paused=True), _runner(31, tags, description="newcomer")]
    events, new_state = _poll(monkeypatch, state, fleet, pending, running,
                              fail_running=True)

    assert [e for e in events if "running_on_it" in e["payload"]] == []
    # And the tick may not overwrite the last good reading with a fabricated one.
    assert new_state == state


def test_the_running_list_being_unread_is_distinguishable_from_it_being_empty(
        monkeypatch) -> None:
    """The helper contract, pinned at the level the callers share. `[]` says
    "looked, nobody is executing"; the absence of a reading says nothing at all
    and must not stamp the annotation mark, or the `UnannotatedFleetError` guard
    is bypassed by every caller that coerces its own API error."""
    unread = [_hercule()]
    runners_op.annotate_live_jobs(unread, None)
    runners_op.annotate_recent_work(unread, [])
    with pytest.raises(runners_op.UnannotatedFleetError):
        runners_op._is_responsive(unread[0])

    looked = [_hercule()]
    runners_op.annotate_live_jobs(looked, [])
    runners_op.annotate_recent_work(looked, [])
    assert runners_op._is_responsive(looked[0]) is False


def test_radar_declines_the_tier_when_the_running_list_is_unreadable(
        monkeypatch) -> None:
    """Radar's own sibling: an unreadable pending queue already returns WARNING
    with health UNKNOWN rather than a verdict. The running list is the stronger
    evidence of the two — losing it must not leave the tier blaming the runners
    for a read that failed on our side.

    Same fleet twice. With the running list read, `hercule` is executing the
    queued job's tag and radar is green. With only that call failing, radar
    today reports `FLEET UNKNOWN — waiting on runners whose liveness could not
    be established`, which sends an operator to check hosts that are fine. The
    tier has to name the unreadable read instead."""
    tags = ["dptools-runner-2"]
    fleet = [_hercule()]
    pending = [_pending_job(tags)]
    running = [_running_job(25, "test_unit_dv")]
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})

    monkeypatch.setattr(runners_op, "_api", _api_stub(fleet, pending, running))
    ok_lines, ok_healthy = runners_op.radar_report({})
    assert ok_healthy is True, ok_lines

    monkeypatch.setattr(runners_op, "_api",
                        _api_stub(fleet, pending, running, fail_running=True))
    lines, healthy = runners_op.radar_report({})

    assert healthy is False
    assert any("unreadable" in line and "UNKNOWN" in line for line in lines), lines


# ---------------------------------------------------------------------------
# fences: the #750 disclosure policy is not what is being fixed here
# ---------------------------------------------------------------------------

def test_a_read_running_list_that_is_empty_still_declines_out_loud(
        monkeypatch) -> None:
    """The row this issue does NOT touch. Heartbeat stale, running list read
    successfully and genuinely empty, work queued behind the runner: nothing
    proves it alive and something is waiting on it. `runner_liveness_unknown`
    is the correct event and must keep firing, or a false alarm has been traded
    for a silence (#750)."""
    tags = ["dptools-runner-2"]
    pending = [_pending_job(tags)]

    _first, state = _poll(monkeypatch, {},
                          [_runner(25, tags, description="hercule")],
                          pending, [])
    events, _state = _poll(monkeypatch, state, [_hercule()], pending, [])

    assert "runner_liveness_unknown" in _names(events)
    unknown = [e for e in events if e["event"] == "runner_liveness_unknown"][0]
    assert unknown["payload"]["running_on_it"] == 0


def test_a_genuinely_down_runner_is_still_reported_through_a_failed_read(
        monkeypatch) -> None:
    """GitLab own `offline` is an answer, not an absence. Losing the running
    list costs us the strongest evidence of life, not the evidence of death —
    but the count that rides on the event is still unmeasured, so the tick has
    to decline as a whole rather than publish a verdict beside a fabricated
    zero. Pinned so the refusal is a deliberate choice, not an oversight."""
    tags = ["dptools-runner-2"]
    pending = [_pending_job(tags)]

    _first, state = _poll(monkeypatch, {}, [_runner(25, tags)], pending, [])
    events, _state = _poll(monkeypatch, state,
                           [_runner(25, tags, status="offline",
                                    contacted_at=_iso(1800))],
                           pending, [], fail_running=True)

    assert _names(events) == []


# ---------------------------------------------------------------------------
# second finding: a payload key the transport structurally cannot carry
# ---------------------------------------------------------------------------

def test_runner_tags_reach_the_channel_instead_of_being_refused(
        monkeypatch) -> None:
    """Every runner event carried `unsendable="1 payload key refused — tags
    (array)"`. The transport is right to refuse it and right to say so, but
    tags are exactly what an operator needs to know which jobs the runner can
    take. Joined at the emit site, in `classify_queue` key format so a runner
    event and a queue event can be matched against each other by eye."""
    tags = ["dptools-runner-2", "docker"]
    pending = [_pending_job(["dptools-runner-2"])]

    _first, state = _poll(monkeypatch, {}, [_runner(25, tags)], pending, [])
    events, _state = _poll(monkeypatch, state, [_hercule(tag_list=tags)],
                           pending, [])

    tagged = [e for e in events if "tags" in e["payload"]]
    assert tagged, "expected at least one event carrying tags"
    for event in tagged:
        assert isinstance(event["payload"]["tags"], str), event["event"]

    fleet_events = [e for e in events if "runner_id" in e["payload"]]
    assert fleet_events, _names(events)
    for event in fleet_events:
        assert event["payload"]["tags"] == "docker,dptools-runner-2", event["event"]

# ---------------------------------------------------------------------------
# the threshold itself: pinned from both sides, from neither boundary
# ---------------------------------------------------------------------------

def test_the_heartbeat_threshold_is_pinned_without_standing_on_it() -> None:
    """What the fixtures above stopped asserting, asserted deliberately.

    Moving the fixtures clear of `_HEARTBEAT_WARN_SECONDS` would otherwise
    delete the only coverage of which side of it means alive, so the semantics
    are pinned here instead — from a safe margin on each side, where the answer
    does not depend on how fast the clock ticks."""
    def probe(age_seconds: float) -> bool:
        return runners_op._is_responsive({
            "id": 25, "active": True, "paused": False, "status": "online",
            "job_execution_status": "idle", "_recent_jobs": 0,
            runners_op._LIVE_JOBS_MARK: True,
            "contacted_at": _iso(age_seconds),
        })

    threshold = runners_op._HEARTBEAT_WARN_SECONDS
    assert probe(threshold - _CLEAR_OF_THRESHOLD) is True
    assert probe(threshold + _CLEAR_OF_THRESHOLD) is False


# ---------------------------------------------------------------------------
# second finding, from auditing the liveness read for timing dependencies
# ---------------------------------------------------------------------------

def test_a_heartbeat_from_the_future_is_not_evidence_of_life(monkeypatch) -> None:
    """`contacted_at` is GitLab's clock; `now` is ours. Nothing measures the gap.

    When ours runs behind, `_age_seconds` returns a NEGATIVE number and every
    temporal test in `_is_responsive` is a `<=` against a ceiling — so a
    heartbeat two hours in the future passes as *fresh*, the strongest verdict
    the rung can give, off a number that is evidence our clock is wrong rather
    than evidence of life.

    The direction is what makes this worse than #1112 rather than the same size.
    A false `runner_liveness_unknown` is a loud alarm about a healthy runner. A
    false *alive* is a silent all-clear over a genuinely wedged one, and it
    lands on every runner at once, because clock skew is a property of the host
    and not of any runner. `docs/validators.md` calls that the more expensive of
    the two trades, and it is the one the tool currently makes."""
    skewed = _runner(25, ["dptools-runner-2"], description="hercule",
                     contacted_at=_iso(-7200))
    runners_op.annotate_live_jobs([skewed], [])
    runners_op.annotate_recent_work([skewed], [])

    assert runners_op._is_responsive(skewed) is False
    # And it must land in the third state, not in the death state: a skewed
    # clock says nothing about whether GitLab thinks the runner is up.
    assert runners_op._demonstrably_down(skewed) is False
    assert runners_op._liveness_unknown(skewed) is True


def test_a_skewed_clock_does_not_silently_clear_a_stranded_queue(
        monkeypatch) -> None:
    """The consequence, at the level an operator sees. Work is queued behind the
    only runner that may take it, and nothing about the timestamps is usable.

    **Both timestamps are skewed, because that is the only way it happens.**
    Skew belongs to the host: `contacted_at` and the job's `created_at` are both
    written by GitLab and read against the same local clock, so they move ahead
    together. Skewing only the runner describes a state the world cannot be in,
    and it passes while the queue-age floor — an `_age_seconds` compared against
    a threshold exactly like the two rungs above — still drops every job as
    "queued too recently" on a negative age. An age we cannot interpret is not
    grounds to dismiss work from the board."""
    tags = ["dptools-runner-2"]
    skewed = _runner(25, tags, contacted_at=_iso(-7200))
    queued_under_skew = {"tag_list": tags, "created_at": _iso(-7200),
                         "name": "phpstan2", "ref": "master"}
    runners_op.annotate_live_jobs([skewed], [])
    runners_op.annotate_recent_work([skewed], [])

    stuck, unproven = runners_op.classify_queue([skewed], [queued_under_skew])

    assert stuck == {}
    assert unproven == {"dptools-runner-2": 1}

    # The per-runner row is a second implementation of the same question, and
    # the docs promise the two cannot disagree by construction. They share the
    # floor, so they shared the gap.
    row_stuck, row_unproven = runners_op.stranded_split_for(
        skewed, [queued_under_skew], [skewed])
    assert (row_stuck, row_unproven) == (0, 1)


def test_future_dated_finished_jobs_are_not_counted_as_recent_work(
        monkeypatch) -> None:
    """The same skew, one rung up the evidence ladder. `fetch_recent_finished`
    keeps a job whose `finished_at` age is `<= window`, and a negative age
    satisfies that too — so under a behind-running clock the whole job history
    reads as "finished just now" and every runner in it scores throughput
    evidence it did not earn. Fixing only the heartbeat rung would leave the
    strongest rung of the three still forging the same false alive."""
    monkeypatch.setattr(
        runners_op, "_api",
        lambda endpoint, paginate=False, timeout=20: (
            [{"finished_at": _iso(-7200), "created_at": _iso(-7200),
              "runner": {"id": 25}}], None))

    kept, _truncated = runners_op.fetch_recent_finished()

    assert kept == []


def test_ordinary_sub_minute_skew_between_two_hosts_still_reads_fresh() -> None:
    """The fence, and the reason this is a tolerance rather than a sign test.

    Two NTP-synced hosts disagree by milliseconds, and a heartbeat written a few
    seconds "in the future" is definitionally the freshest reading there is.
    Refusing every negative age would turn that into a fleet-wide UNKNOWN — a
    false alarm on every runner at once, which is the failure #750 exists to
    stop. Only a skew too large to be jitter withdraws the evidence."""
    jittered = _runner(25, ["dptools-runner-2"], contacted_at=_iso(-5))
    runners_op.annotate_live_jobs([jittered], [])
    runners_op.annotate_recent_work([jittered], [])

    assert runners_op._is_responsive(jittered) is True


def test_a_future_heartbeat_is_not_rendered_as_an_age() -> None:
    """`_human_age(-3600)` returned `'-3600s'`, which reads as a runner seen
    minus-one-hour ago. The renderer has to say the reading is unusable, because
    a negative age is the one thing on that row an operator can act on — it is
    about their clock, not about the fleet."""
    rendered = runners_op._human_age(-7200)

    assert not rendered.startswith("-")
    assert "-7200" not in rendered
