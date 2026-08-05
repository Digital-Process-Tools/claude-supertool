"""`DONE/30m 0` does not mean "wedged" on its own, and must stop implying it (#531).

The column is the best signal in the op — it found a runner GitLab was still
reporting `online, idle`. It is also ambiguous, and was misread twice in
opposite directions inside one session: a runner showing `RUN 6  DONE/30m 0`
was called wedged 22 minutes after its host rebooted (zero completions was
expected), and the identical reading sixteen minutes later *was* a wedge, by
which point the earlier over-call had spent the credibility.

The issue proposed scoping the counter to `min(window, uptime)` and labelling
it `DONE/22m`. That is not buildable, and the tests below are written the way
they are because of what the live API says:

- `runners/:id.created_at`   — registration. Years old. Not uptime.
- `runners/:id.contacted_at` — last seen. Says nothing about continuity.
- `runner_managers[].createdAt` (GraphQL; absent from REST on 18.11.7 CE) —
  the manager's first registration, also years old.

There is no uptime and no first-heartbeat, so a scoped label would have to be
inferred from job history — and that inference fails in exactly the direction
that matters: a runner up for hours and wedged has *no* activity to infer from,
so it would be labelled with the shortest window of all and its `0` would read
as "we only just started looking". That silences the wedge. So the op states
the confound instead of inventing a number for it, and states it only on the
rows where a reader would act on the `0`.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

OP_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "runners.py"
_spec = importlib.util.spec_from_file_location("gl_runners_531", OP_PATH)
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


def _job_on(rid: int) -> dict:
    return {"id": rid * 100, "name": "build", "ref": "master", "runner": {"id": rid}}


# ---------------------------------------------------------------------------
# which rows the caveat is about
# ---------------------------------------------------------------------------

def test_executing_but_completing_nothing_is_the_ambiguous_shape() -> None:
    """RUN>0 with DONE 0 is both a wedge and a fresh reboot. It cannot be read."""
    runner = _runner(1, _recent_jobs=0)
    assert runners_op.done_zero_unreadable([runner], {1: 6}, {}) == ["runner-1"]


def test_a_runner_gitlab_still_calls_online_but_quiet_is_the_ambiguous_shape() -> None:
    """Advertised healthy, heartbeat past the threshold, nothing finished —
    and pending work queued that it may take.

    The queued job is not decoration. This case originally asserted the caveat
    on the heartbeat alone, which named a runner holding nothing and blocking
    nothing, and named every row at once on an idle fleet ([#806]). The `0` is
    still unreadable; it is only worth a line when something is waiting on it.
    """
    runner = _runner(2, _recent_jobs=0, contacted_at=_iso(3600))
    assert runners_op.done_zero_unreadable([runner], {}, {2: 1}) == ["runner-2"]


def test_a_quiet_runner_with_nothing_at_stake_is_not_caveated() -> None:
    """The same record with an empty queue — no wedge reading to disclaim (#806)."""
    runner = _runner(2, _recent_jobs=0, contacted_at=_iso(3600))
    assert runners_op.done_zero_unreadable([runner], {}, {}) == []


def test_a_status_that_already_explains_the_zero_gets_no_caveat() -> None:
    """`paused` / `stale` rows say why they finished nothing. Uptime is not the
    thing to go and check, and naming them here is how the note becomes
    wallpaper — a live fleet had two such rows and no wedge."""
    paused = _runner(5, _recent_jobs=0, paused=True)
    stale = _runner(6, _recent_jobs=0, status="stale", contacted_at=_iso(86400 * 400))
    assert runners_op.done_zero_unreadable([paused, stale], {}, {5: 1, 6: 1}) == []


def test_a_runner_that_completed_work_is_not_ambiguous() -> None:
    """DONE>0 answers the question outright — no caveat, no line spent."""
    runner = _runner(3, _recent_jobs=4)
    assert runners_op.done_zero_unreadable([runner], {3: 2}, {3: 1}) == []


def test_an_idle_live_runner_with_nothing_to_do_is_not_ambiguous() -> None:
    """Otherwise the caveat is wallpaper on every quiet fleet and stops being read."""
    runner = _runner(4, _recent_jobs=0, job_execution_status="active")
    assert runners_op.done_zero_unreadable([runner], {}, {4: 1}) == []


# ---------------------------------------------------------------------------
# what the table prints
# ---------------------------------------------------------------------------

def test_the_table_states_the_confound_and_names_the_rows(capsys) -> None:
    fleet = [_runner(1, _recent_jobs=0), _runner(2, _recent_jobs=3)]
    runners_op._print_fleet(fleet, [], [_job_on(1)])
    out = capsys.readouterr().out

    assert "DONE/30m" in out
    assert "uptime" in out, "the caveat must name what is missing"
    assert "runner-1" in out.split("NOTE")[-1], "the ambiguous row must be named"
    assert "runner-2" not in out.split("NOTE")[-1], "a measured row is not caveated"


def test_no_caveat_when_every_row_reads_cleanly(capsys) -> None:
    fleet = [_runner(1, _recent_jobs=2), _runner(2, _recent_jobs=5)]
    runners_op._print_fleet(fleet, [], [_job_on(1), _job_on(2)])
    out = capsys.readouterr().out

    assert "DONE/30m" in out
    assert "uptime" not in out


def test_the_window_and_the_evidence_order_are_untouched() -> None:
    """#527 measured these against a live fleet; #531 is a rendering change only."""
    assert runners_op._THROUGHPUT_WINDOW_SECONDS == 1800
    assert runners_op._HEARTBEAT_WARN_SECONDS == 1800
    # Completed work still outranks a stale heartbeat, and executing still
    # outranks it — the ladder from #527, unchanged.
    assert runners_op._is_responsive(_runner(9, contacted_at=_iso(3600),
                                             _recent_jobs=4)) is True
    assert runners_op._is_responsive(
        _runner(9, contacted_at=_iso(3600), job_execution_status="active")) is True
