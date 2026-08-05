"""The `DONE/30m 0` caveat must name only rows with work at stake (#806).

Reproduced live against the DPT fleet while #805 was in review:

    21  docker-db-on-disk  project  online  idle  35m  0  0  0  ...  <! silent

    NOTE: DONE/30m 0 reads as a wedge only for a runner that has been up the
    whole 30m ... Check host uptime before calling a wedge: docker-db-on-disk

Runner 21 is `online`, holding nothing (`RUN 0`) with nothing queued that it
may take (`WAIT 0`). Its only demerit is `contacted_at` age — the field #805
established is throttled, and which an *idle* fleet drifts past on every row
at once precisely because nothing is running to refresh it. The note
nonetheless sent the reader to go and check a host, before a wedge nobody had
called, about work that does not exist.

`done_zero_unreadable`'s own docstring already says the exclusions exist so the
caveat does not become "wallpaper on every quiet fleet". They did not achieve
that: the only quiet runner they excluded was one with a *fresh* heartbeat, and
a fresh heartbeat is the first thing an idle fleet loses. So the note fired on
exactly the fleet shape it was written to stay quiet on.

The narrowing is by stake, not by liveness verdict:

- a runner holding running jobs and completing none is still named — that is
  the wedge shape the op exists for, and its `0` is genuinely unreadable;
- a runner whose liveness is UNKNOWN *and* which pending work is waiting on is
  still named — the `0` is unreadable and something is blocked behind it;
- a runner whose liveness is UNKNOWN with nothing at stake is not named. There
  is no wedge reading to caveat when there is nothing to be wedged on.

Runners GitLab itself calls `paused`/`offline`/`stale`/`never_contacted` stay
excluded, as they were before, and this file pins that: uptime is not the thing
to go and check for a row whose STATUS column already explains its `0`.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

OP_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "runners.py"
_spec = importlib.util.spec_from_file_location("gl_runners_806", OP_PATH)
assert _spec is not None and _spec.loader is not None
runners_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runners_op)


def _iso(seconds_ago: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


def _runner(rid: int = 1, **over) -> dict:
    base = {
        "id": rid, "description": f"runner-{rid}", "tag_list": ["docker"],
        "run_untagged": False, "status": "online", "active": True,
        "paused": False, "contacted_at": _iso(10), "job_execution_status": "idle",
        "_recent_jobs": 0, "_live_jobs_checked": True,
    }
    base.update(over)
    return base


def _quiet_online(rid: int) -> dict:
    """The live shape: GitLab says online and idle, heartbeat past 30m, DONE 0."""
    return _runner(rid, contacted_at=_iso(2100), _recent_jobs=0)


def _pending(jid: int, tags: list[str] | None = None) -> dict:
    return {"id": jid, "name": "build", "ref": "master",
            "created_at": _iso(900), "tag_list": ["docker"] if tags is None else tags}


def _running_on(rid: int) -> dict:
    return {"id": rid * 100, "name": "build", "ref": "master", "runner": {"id": rid}}


def _note(out: str) -> str:
    """Everything after the NOTE header, or "" when no note was printed."""
    return out.split("NOTE")[-1] if "NOTE" in out else ""


# ---------------------------------------------------------------------------
# the negative — the assertion that matters
# ---------------------------------------------------------------------------

def test_the_live_shape_with_nothing_at_stake_is_not_named(capsys) -> None:
    """#806 verbatim: online, idle, 35m heartbeat, RUN 0, WAIT 0, DONE 0."""
    fleet = [_quiet_online(21) | {"description": "docker-db-on-disk"}]
    runners_op._print_fleet(fleet, [], [])
    out = capsys.readouterr().out

    assert "docker-db-on-disk" in out, "the row itself must still be rendered"
    assert "docker-db-on-disk" not in _note(out), (
        "the note sent the reader to check the uptime of a host holding no job "
        "and blocking no queued work"
    )
    assert "uptime" not in out, "with nothing at stake the note has nothing to say"


def test_an_idle_fleet_does_not_get_the_note_on_every_row(capsys) -> None:
    """The wallpaper case the exclusions were written for and did not cover.

    An idle fleet is exactly when every heartbeat goes stale at once (#805), so
    the pre-#806 predicate named the whole fleet in one breath.
    """
    fleet = [_quiet_online(rid) for rid in range(1, 7)]
    runners_op._print_fleet(fleet, [], [])
    out = capsys.readouterr().out

    assert "uptime" not in out, "six healthy idle runners, no queue, no note"


def test_a_row_gitlab_already_explains_is_not_named_even_with_work_waiting(capsys) -> None:
    """Pushback pinned: a `paused`/`stale` row does NOT want this note.

    #806 floats "demonstrably down" as a candidate audience. It is the wrong
    one and always was: the STATUS column already explains the `0`, and host
    uptime is not what a reader should go and check for a runner nobody
    un-paused or nobody deregistered. Naming them is how the note becomes
    wallpaper from the other end.
    """
    fleet = [
        _runner(5, paused=True, _recent_jobs=0),
        _runner(6, status="stale", contacted_at=_iso(86400 * 400), _recent_jobs=0),
    ]
    runners_op._print_fleet(fleet, [_pending(1)], [])
    out = capsys.readouterr().out

    assert "uptime" not in out, "a status that explains the zero gets no caveat"


def test_a_down_runner_still_holding_a_job_is_not_named_either(capsys) -> None:
    """The one case where the down exclusion is the only thing doing the work.

    `_liveness_unknown` already keeps a `paused`/`stale` row out of the
    *blocking* branch, so the exclusion looks redundant until GitLab's
    running-job scope still attributes a job to a record it also calls down —
    a runner paused mid-job, or a `stale` registration with a job listed
    against it. That row reaches the *holding* branch, and its `0` is still
    explained by the STATUS column rather than by uptime.
    """
    fleet = [
        _runner(7, paused=True, _recent_jobs=0),
        _runner(8, status="stale", contacted_at=_iso(86400 * 400), _recent_jobs=0),
    ]
    runners_op._print_fleet(fleet, [], [_running_on(7), _running_on(8)])
    out = capsys.readouterr().out

    assert "uptime" not in out, (
        "a runner GitLab itself calls down does not want an uptime caveat, "
        "whichever branch of the note it arrives through"
    )


# ---------------------------------------------------------------------------
# the positives — these hold on the broken version too, and are here so the
# narrowing cannot be passed by deleting the note
# ---------------------------------------------------------------------------

def test_a_quiet_online_runner_with_work_waiting_on_it_is_still_named(capsys) -> None:
    """Same runner as the negative, one queued job it may take. Now it matters."""
    fleet = [_quiet_online(21) | {"description": "docker-db-on-disk"}]
    runners_op._print_fleet(fleet, [_pending(1)], [])
    out = capsys.readouterr().out

    assert "uptime" in out, "work is queued behind a runner whose DONE 0 is unreadable"
    assert "docker-db-on-disk" in _note(out)


def test_executing_but_completing_nothing_is_still_named(capsys) -> None:
    """RUN>0 with DONE 0 is the original #531 shape and stays named with an
    empty queue: the runner is holding work it is not returning."""
    fleet = [_runner(1, _recent_jobs=0)]
    runners_op._print_fleet(fleet, [], [_running_on(1)])
    out = capsys.readouterr().out

    assert "uptime" in out
    assert "runner-1" in _note(out)


def test_a_runner_that_completed_work_is_never_named(capsys) -> None:
    """DONE>0 answers the question outright, stake or no stake."""
    fleet = [_runner(2, _recent_jobs=4, contacted_at=_iso(2100))]
    runners_op._print_fleet(fleet, [_pending(1)], [_running_on(2)])
    out = capsys.readouterr().out

    assert "uptime" not in out


# ---------------------------------------------------------------------------
# the predicate #805 introduced but never named
# ---------------------------------------------------------------------------

def test_liveness_unknown_is_the_gap_between_the_two_positive_predicates() -> None:
    """Three states, three answers — not two answers and a negation."""
    responsive = _runner(1, _recent_jobs=3)
    down = _runner(2, status="offline", contacted_at=_iso(86400))
    gap = _quiet_online(3)

    assert runners_op._is_responsive(responsive) is True
    assert runners_op._liveness_unknown(responsive) is False

    assert runners_op._demonstrably_down(down) is True
    assert runners_op._liveness_unknown(down) is False

    assert runners_op._is_responsive(gap) is False
    assert runners_op._demonstrably_down(gap) is False
    assert runners_op._liveness_unknown(gap) is True, (
        "online, un-paused, stale heartbeat only — the #805 UNKNOWN gap"
    )
