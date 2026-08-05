"""gl-runners: heartbeat-only staleness is a question, not a verdict (#750).

Twelve fleet alarms in one ~28h session, all false, all self-resolving. Three
shapes were reported. Probed against the code as it stands, they are not three
live defects — one of them was already closed by #613 and is kept here as a
fence, and one of the remaining two is a true finding wearing a false sentence.

    paused + replaced   runner 29 paused beside same-tag runner 32
                        -> already silent since #613. Fence, not a fix.
    438-day stale       runner 23, exclusive tag, work pinned behind it
                        -> fires, and should: nothing else can take that work.
                           But the notify text says "GitLab still reports it
                           online" when GitLab reports it `stale`.
    idle + throttled    runner 18, `online`, idle, contacted_at 38m
                        -> fires `runner_starved`. This is the false one.

The last shape is the whole issue. `contacted_at` is the field GitLab throttles
— the module docstring says so, `docs/presets/watch.md` says so, and the 30
minute threshold was picked to sit clear of an observed ~10 minute drift. The
session that produced this issue saw six runners cluster at ~40m during an idle
window and all go active seconds later. So a runner GitLab still advertises as
`online`, not paused and not stale, whose only demerit is heartbeat age, has
not been shown to be dead. It has not been shown to be alive either.

That is the three-state contract in `docs/validators.md` §"Declining instead of
guessing", applied to a fleet: `ok`, a finding, and **declined**. The tests
below pin both halves of it, because either half alone is a different bug. The
alarm must stop asserting a wedge it cannot prove — and the queue must still be
disclosed, loudly, or a false alarm has been traded for a false all-clear,
which is the more expensive of the two.

The fences matter as much as the fixes. GitLab's own `paused` / `offline` /
`stale` / `never_contacted` are *answers*, not absences, and work pinned behind
a runner in one of those states is a finding with no uncertainty in it. So is
work carrying a tag no runner in the fleet holds. Those must stay loud.

Everything is driven through `poll()` and through the printing functions, never
through the helper under change, so a half-implementation that fixes the helper
and does not adopt it at the call site still fails.
"""
from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
from pathlib import Path

_ROOT = Path(__file__).parent.parent
POLLER_PATH = _ROOT / "presets" / "watch" / "sources" / "gl-runners" / "poller.py"

_spec = importlib.util.spec_from_file_location("gl_runners_poller_750", POLLER_PATH)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

runners_op = poller.runners_op


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


def _throttled(rid: int, tags: list[str], minutes: int = 38, **over) -> dict:
    """The reported shape: GitLab says online, heartbeat is the only demerit."""
    return _runner(rid, tags, contacted_at=_iso(minutes * 60), **over)


def _job(tags: list[str], age: float = 1800, name: str = "phpstan2") -> dict:
    return {"tag_list": tags, "created_at": _iso(age), "name": name, "ref": "master"}


def _api_stub(runners: list[dict], pending: list[dict], running: list[dict]):
    def fake_api(endpoint, paginate=False, timeout=20):
        if "runners?" in endpoint:
            return (runners, None)
        if "scope[]=pending" in endpoint:
            return (pending, None)
        if "scope[]=running" in endpoint:
            return (running, None)
        return ([], None)  # job history

    return fake_api


def _poll_once(monkeypatch, runners, pending, running=()):
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(runners_op, "_api", _api_stub(runners, pending, list(running)))
    return poller.poll({}, {"id": "fleet"})


def _transition(monkeypatch, before, after, pending, running=()):
    """Baseline tick then the degraded tick; return the second tick's events."""
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(runners_op, "_api", _api_stub(before, pending, list(running)))
    _events, state = poller.poll({}, {"id": "fleet"})
    monkeypatch.setattr(runners_op, "_api", _api_stub(after, pending, list(running)))
    events, _state = poller.poll(state, {"id": "fleet"})
    return events


def _names(events) -> list[str]:
    return [e["event"] for e in events]


def _render(runners: list[dict], pending: list[dict], running=()) -> str:
    """The `gl-runners` default view: fleet table then diagnosis footer."""
    fleet = [dict(r) for r in runners]
    runners_op.annotate_live_jobs(fleet, list(running))
    runners_op.annotate_recent_work(fleet, [])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        runners_op._print_fleet(fleet, pending, list(running))
        runners_op._print_diagnosis(fleet, pending)
    return buffer.getvalue()


def _row_flags_starved(rendered: str) -> bool:
    table = rendered.split("Queue:")[0].split("## STARVED")[0]
    return "STARVED" in table


def _footer_flags_starved(rendered: str) -> bool:
    return "## STARVED" in rendered


# ---------------------------------------------------------------------------
# the live false alarm: heartbeat age is not a death certificate
# ---------------------------------------------------------------------------

def test_an_online_runner_stale_only_by_heartbeat_does_not_assert_starvation(
        monkeypatch) -> None:
    """The filed shape. Runner 18 is `online`, not paused, idle, last contacted
    38 minutes ago while a 73-minute job runs elsewhere. Nothing in that record
    shows it cannot take work — `contacted_at` is the throttled field, and the
    same session saw six runners cluster at ~40m and all go active seconds
    later. `runner_starved` claims the jobs are stuck. It has not been shown."""
    tags = ["dptools-runner-2"]
    long_job = {"tag_list": ["coverage"], "created_at": _iso(4449),
                "name": "test_unit_full_with_coverage", "runner": {"id": 40},
                "ref": "master"}
    fleet = [_throttled(18, tags), _throttled(40, ["coverage"], minutes=40)]

    events, _state = _poll_once(monkeypatch, fleet, [_job(tags)], [long_job])

    assert "runner_starved" not in _names(events)


def test_the_unproven_queue_is_disclosed_rather_than_dropped(monkeypatch) -> None:
    """The other half, and the one that makes the first half safe. Silence
    would trade a false alarm for a false all-clear: jobs really are sitting in
    the queue and the only runner that may take them really is unaccounted for.
    So the poll declines out loud — a stated UNKNOWN, not an absence."""
    tags = ["dptools-runner-2"]
    fleet = [_throttled(18, tags)]

    events, state = _poll_once(monkeypatch, fleet, [_job(tags)])

    assert "queue_liveness_unknown" in _names(events)
    unknown = [e for e in events if e["event"] == "queue_liveness_unknown"][0]
    assert unknown["payload"]["pending"] == 1
    assert unknown["payload"]["tags"] == "dptools-runner-2"
    assert state["unknown"] == {"dptools-runner-2": 1}


def test_the_decline_names_the_runner_and_why_it_is_unaccounted_for(
        monkeypatch) -> None:
    """A decline nobody can act on is noise. It has to name the runner to go
    and look at, and say what the tool could not establish about it."""
    tags = ["dptools-runner-2"]
    events, _state = _poll_once(monkeypatch, [_throttled(18, tags)], [_job(tags)])

    unknown = [e for e in events if e["event"] == "queue_liveness_unknown"][0]
    text = f"{unknown['notify_title']} {unknown['notify_message']}".lower()
    assert "runner-18" in text
    assert "38m" in text


def test_a_wedged_runner_is_reported_unconfirmed_not_wedged(monkeypatch) -> None:
    """The per-runner event carries the same distinction as the queue one. A
    runner GitLab still calls `online` whose heartbeat merely aged out has not
    been shown wedged, so `runner_silent` — which states a wedge — must not be
    the event that fires for it."""
    tags = ["dptools-runner-2"]
    events = _transition(monkeypatch, [_runner(18, tags)], [_throttled(18, tags)],
                         [_job(tags)])

    assert "runner_silent" not in _names(events)
    assert "runner_liveness_unknown" in _names(events)


def test_recovery_still_fires_after_a_declined_reading(monkeypatch) -> None:
    """A runner that came back must clear, whichever of the two events it went
    out on. Otherwise the decline is a state the fleet can never leave."""
    tags = ["dptools-runner-2"]
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    pending = [_job(tags)]

    monkeypatch.setattr(runners_op, "_api", _api_stub([_runner(18, tags)], pending, []))
    _first, state = poller.poll({}, {"id": "fleet"})
    monkeypatch.setattr(runners_op, "_api",
                        _api_stub([_throttled(18, tags)], pending, []))
    _second, state = poller.poll(state, {"id": "fleet"})
    monkeypatch.setattr(runners_op, "_api", _api_stub([_runner(18, tags)], pending, []))
    events, _state = poller.poll(state, {"id": "fleet"})

    assert "runner_recovered" in _names(events)


# ---------------------------------------------------------------------------
# fences — GitLab's own verdict is an answer, and those must stay loud
# ---------------------------------------------------------------------------

def test_a_paused_runner_with_work_pinned_behind_it_still_alarms(
        monkeypatch) -> None:
    """The issue proposes never alarming on `paused`. Rejected. A runner
    somebody paused and forgot to un-pause, holding the only tag a queued job
    carries, is a stranded fleet — and the pause is the fix, not the excuse."""
    tags = ["dptools-runner-7"]
    paused = _runner(29, tags, paused=True, status="paused",
                     contacted_at=_iso(6 * 86400))

    events = _transition(monkeypatch, [_runner(29, tags)], [paused], [_job(tags)])

    assert "runner_silent" in _names(events)
    assert "runner_starved" in _names(events)


def test_a_paused_runner_alarm_names_the_pause_as_the_thing_to_undo(
        monkeypatch) -> None:
    """Loud is not enough — the sentence has to point at the one-step fix."""
    tags = ["dptools-runner-7"]
    paused = _runner(29, tags, paused=True, status="paused",
                     contacted_at=_iso(6 * 86400))

    events = _transition(monkeypatch, [_runner(29, tags)], [paused], [_job(tags)])
    silent = [e for e in events if e["event"] == "runner_silent"][0]

    assert "paused" in silent["notify_message"].lower()


def test_a_438_day_stale_registration_with_pinned_work_still_alarms(
        monkeypatch) -> None:
    """The issue proposes a staleness ceiling that reclassifies or suppresses
    very old registrations. Rejected as a suppression. A runner GitLab itself
    calls `stale` is the most certainly-dead record on the board; if work is
    pinned to its exclusive tag that work cannot move, and saying nothing is
    the failure this source exists to prevent."""
    tags = ["dptools-runner-3"]
    dead = _runner(23, tags, status="stale", contacted_at=_iso(438 * 86400))

    events = _transition(monkeypatch, [_runner(23, tags)], [dead], [_job(tags)])

    assert "runner_silent" in _names(events)
    assert "runner_starved" in _names(events)


def test_the_stale_runner_alarm_does_not_claim_gitlab_reports_it_online(
        monkeypatch) -> None:
    """The sentence was hardcoded. GitLab reports runner 23 as `stale`, and a
    reader told it is "still online" goes looking for a wedged host instead of
    deleting a registration nobody deregistered 438 days ago."""
    tags = ["dptools-runner-3"]
    dead = _runner(23, tags, status="stale", contacted_at=_iso(438 * 86400))

    events = _transition(monkeypatch, [_runner(23, tags)], [dead], [_job(tags)])
    silent = [e for e in events if e["event"] == "runner_silent"][0]

    assert "still reports it online" not in silent["notify_message"]
    assert "stale" in silent["notify_message"].lower()


def test_work_carrying_a_tag_no_runner_holds_is_starved_not_unknown(
        monkeypatch) -> None:
    """No candidate runner at all is a certainty, not an absence of evidence.
    Routing this into the declined bucket would be the fix eating the signal."""
    events, state = _poll_once(monkeypatch, [_runner(18, ["dptools-runner-2"])],
                               [_job(["gpu"])])

    assert "runner_starved" in _names(events)
    assert state["unknown"] == {}


def test_a_replaced_runner_stays_silent(monkeypatch) -> None:
    """#613's fence, re-pinned here because #750 re-reported it as live. Runner
    29 paused beside runner 32 carrying the same tag and online: the job runs
    on 32, nothing is stuck, and nothing is said."""
    tags = ["dptools-runner-7"]
    successor = _runner(32, tags, description="dptools-runner-7 V2")
    paused = _runner(29, tags, description="dptools-runner-7", paused=True,
                     status="paused", contacted_at=_iso(6 * 86400))

    events = _transition(monkeypatch, [_runner(29, tags), successor],
                         [paused, successor], [_job(tags)])

    assert "runner_silent" not in _names(events)
    assert "runner_liveness_unknown" not in _names(events)
    assert "runner_starved" not in _names(events)


def test_a_fresh_queue_is_neither_starved_nor_unknown(monkeypatch) -> None:
    """The minimum-queue-age filter is load-bearing on both buckets. A pipeline
    that started ten seconds ago has not been waiting on anything."""
    tags = ["dptools-runner-2"]
    events, state = _poll_once(monkeypatch, [_throttled(18, tags)],
                               [_job(tags, age=10)])

    assert _names(events) == []
    assert state["starved"] == {}
    assert state["unknown"] == {}


# ---------------------------------------------------------------------------
# the row and the footer are one computation
# ---------------------------------------------------------------------------

def test_the_row_flag_and_the_footer_cannot_disagree_about_a_fresh_queue() -> None:
    """Observed in one render: two rows flagged `<! STARVED` under a footer
    reading "all have a responsive runner". The row matched pending tags
    against non-heartbeating runners; the footer asked the routing question per
    job and applied the queue-age floor. A reader acting on the row concludes
    the opposite of a reader acting on the footer."""
    tags = ["dptools-runner-2"]
    rendered = _render([_throttled(18, tags)], [_job(tags, age=10)])

    assert _row_flags_starved(rendered) == _footer_flags_starved(rendered)


def test_the_row_flag_and_the_footer_cannot_disagree_about_a_covered_queue() -> None:
    """The #613 shape at the table level: a silent runner beside a live
    same-tag successor. The footer routes the job to the successor; the row
    must not brand the stale record as starving it."""
    tags = ["dptools-runner-7"]
    stale = _runner(29, tags, status="stale", contacted_at=_iso(6 * 86400))
    rendered = _render([stale, _runner(32, tags)], [_job(tags)])

    assert _row_flags_starved(rendered) == _footer_flags_starved(rendered)


def test_the_row_flag_still_fires_where_the_footer_does() -> None:
    """The fence on the other side: agreement reached by never flagging a row
    would delete the per-runner locality the table exists for."""
    tags = ["dptools-runner-3"]
    dead = _runner(23, tags, status="stale", contacted_at=_iso(438 * 86400))
    rendered = _render([dead], [_job(tags)])

    assert _footer_flags_starved(rendered)
    assert _row_flags_starved(rendered)


def test_a_queue_too_young_to_judge_does_not_print_the_all_clear() -> None:
    """The same false all-clear from the other end. Every pending job is below
    the queue-age floor, so none of them was routed at all — and the only
    runner that could take them is not responsive. "All have a responsive
    runner" is a claim about work nobody looked at."""
    tags = ["dptools-runner-2"]
    rendered = _render([_throttled(18, tags)], [_job(tags, age=10)])

    assert "all have a responsive runner" not in rendered
    assert "too soon" in rendered


def test_a_judged_queue_still_prints_the_all_clear() -> None:
    """And the fence: work past the floor that a live runner may take is the
    one case the all-clear was always for."""
    tags = ["dptools-runner-2"]
    rendered = _render([_runner(18, tags)], [_job(tags)])

    assert "all have a responsive runner" in rendered


def test_the_footer_states_the_declined_queue() -> None:
    """The op renders the same three states the watcher emits. A queue whose
    routing could not be established must not print the all-clear sentence."""
    tags = ["dptools-runner-2"]
    rendered = _render([_throttled(18, tags)], [_job(tags)])

    assert "all have a responsive runner" not in rendered
    assert "UNKNOWN" in rendered
    assert "runner-18" in rendered


# ---------------------------------------------------------------------------
# radar carries the decline too
# ---------------------------------------------------------------------------

def test_radar_is_not_green_when_the_queue_cannot_be_explained(
        monkeypatch) -> None:
    """Radar renders a tier's verdict. An unproven queue is not `fleet ok` —
    that is the false all-clear arriving one layer up."""
    tags = ["dptools-runner-2"]
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(runners_op, "_api",
                        _api_stub([_throttled(18, tags)], [_job(tags)], []))

    lines, healthy = runners_op.radar_report({})

    assert healthy is False
    assert any("UNKNOWN" in line for line in lines)


def test_radar_stays_green_on_a_queue_that_is_genuinely_routed(
        monkeypatch) -> None:
    """And the fence: a live runner covering the queue is still `fleet ok`."""
    tags = ["dptools-runner-2"]
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(runners_op, "_api",
                        _api_stub([_runner(18, tags)], [_job(tags)], []))

    lines, healthy = runners_op.radar_report({})

    assert healthy is True
    assert any("fleet ok" in line for line in lines)
