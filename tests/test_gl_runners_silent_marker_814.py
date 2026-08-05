"""`<! silent` must not land on a row whose STATUS column says `online` (#814).

The fleet table marks a row from a three-way chain:

    stuck, unproven = stranded_split_for(runner, pending, runners)
    if not responsive and stuck:      "  <! STARVED"
    elif not responsive and unproven: "  <! UNKNOWN"
    elif not responsive:              "  <! silent"

The `UNKNOWN` branch that #805 added is *not* the liveness-UNKNOWN state, and
that is the part the issue body and the pre-flight both read past. `stuck` and
`unproven` are counts of **pending work stranded behind this runner**. With an
empty queue both are `0`, so every non-responsive runner — whatever the evidence
behind it — falls through to `<! silent`:

    21  docker-db-on-disk  project  online  idle  35m  0  0  0  ...  <! silent

GitLab calls that runner `online`. It failed `_is_responsive` on `contacted_at`
age alone, which is the throttled field #805 established is a reason to look and
never a finding. `_liveness_unknown` names that state precisely — and the row
prints the opposite of it, next to a STATUS column that says the opposite again.
A reader has a self-contradicting row and nothing telling them which half to
believe, which by this repo's own test (`docs/validators.md` §"Declining instead
of guessing") is #750 arriving through the marker instead of the caveat.

#806's agent left this alone on the argument that *"silent" states an
observation (the heartbeat is stale) rather than a verdict*. The distinction is
real, and it does not survive this row: the heartbeat age is already printed, in
the SEEN column, three columns to the left. As an observation the marker is
redundant with the table; the only work it does is verdict work, and beside
`online` it is read as one.

So the fix is a re-key, not a removal. `<! silent` keeps firing — on runners
GitLab itself calls paused/offline/stale/never_contacted, whose STATUS column
agrees with it. The gap it used to cover is handed to `<! UNKNOWN`, which
already exists one line above for exactly this evidence. Every row that carried
a marker before still carries one; none is widened away, none goes quiet.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

OP_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "runners.py"
_spec = importlib.util.spec_from_file_location("gl_runners_814", OP_PATH)
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
    """The #814 shape: GitLab says online and idle, heartbeat past 30m, DONE 0."""
    return _runner(rid, contacted_at=_iso(2100), _recent_jobs=0)


def _pending(jid: int, tags: list[str] | None = None) -> dict:
    return {"id": jid, "name": "build", "ref": "master",
            "created_at": _iso(900), "tag_list": ["docker"] if tags is None else tags}


def _row_for(out: str, description: str) -> str:
    """The single table row naming `description`, excluding the NOTE footer."""
    body = out.split("NOTE")[0]
    rows = [line for line in body.splitlines() if description in line]
    assert len(rows) == 1, f"expected exactly one row for {description}, got {rows}"
    return rows[0]


# ---------------------------------------------------------------------------
# the contradiction — the assertion the issue was filed for
# ---------------------------------------------------------------------------

def test_the_online_row_from_the_issue_is_not_marked_silent(capsys) -> None:
    """#814 verbatim: online, idle, 35m heartbeat, RUN 0, WAIT 0, DONE 0."""
    fleet = [_quiet_online(21) | {"description": "docker-db-on-disk"}]
    runners_op._print_fleet(fleet, [], [])
    row = _row_for(capsys.readouterr().out, "docker-db-on-disk")

    assert "online" in row, "precondition: GitLab still advertises this runner"
    assert "<! silent" not in row, (
        "the row asserts about the runner what is only true of the throttled "
        "heartbeat field, while its own STATUS column says the opposite"
    )


def test_the_online_row_still_carries_a_marker(capsys) -> None:
    """The signal is re-keyed, never dropped — a blank row tells nobody anything."""
    fleet = [_quiet_online(21) | {"description": "docker-db-on-disk"}]
    runners_op._print_fleet(fleet, [], [])
    row = _row_for(capsys.readouterr().out, "docker-db-on-disk")

    assert "<!" in row, "a non-responsive runner must still be flagged"
    assert "<! UNKNOWN" in row, (
        "liveness is unmeasured, which is the state _liveness_unknown names and "
        "the marker vocabulary already carries one line above"
    )


def test_no_row_ever_says_online_and_silent_at_once(capsys) -> None:
    """The invariant, over the whole render rather than one hand-picked row."""
    fleet = [
        _quiet_online(21) | {"description": "quiet-online"},
        _runner(22, description="fresh-online"),
        _runner(23, description="long-stale", status="stale",
                contacted_at=_iso(442 * 86400)),
        _runner(24, description="parked", paused=True, contacted_at=_iso(11 * 86400)),
        _runner(25, description="working", _recent_jobs=4),
    ]
    runners_op._print_fleet(fleet, [], [])
    body = capsys.readouterr().out.split("NOTE")[0]

    for line in body.splitlines():
        if "<! silent" in line:
            assert "online" not in line, (
                f"self-contradicting row, silent beside GitLab's own online: {line}"
            )


# ---------------------------------------------------------------------------
# the loud signal survives — silent still fires where the evidence is there
# ---------------------------------------------------------------------------

def test_a_stale_runner_is_still_marked_silent(capsys) -> None:
    """Runner 23 on the live fleet: 442d, GitLab's own verdict, not an inference."""
    fleet = [_runner(23, description="dptools-runner-3", status="stale",
                     contacted_at=_iso(442 * 86400))]
    runners_op._print_fleet(fleet, [], [])
    row = _row_for(capsys.readouterr().out, "dptools-runner-3")

    assert "<! silent" in row, (
        "GitLab itself calls this down; removing the marker here would trade a "
        "loud false alarm for a fleet check that flags nothing"
    )


def test_a_paused_runner_is_still_marked_silent(capsys) -> None:
    """Runner 29 on the live fleet: paused 11d. STATUS agrees with the marker."""
    fleet = [_runner(29, description="dptools-runner-7", paused=True,
                     contacted_at=_iso(11 * 86400))]
    runners_op._print_fleet(fleet, [], [])
    row = _row_for(capsys.readouterr().out, "dptools-runner-7")

    assert "<! silent" in row
    assert "paused" in row, "precondition: the STATUS column states the same thing"


def test_an_offline_runner_is_still_marked_silent(capsys) -> None:
    fleet = [_runner(30, description="gone", status="offline",
                     contacted_at=_iso(3 * 86400))]
    runners_op._print_fleet(fleet, [], [])
    assert "<! silent" in _row_for(capsys.readouterr().out, "gone")


def test_a_responsive_runner_carries_no_marker(capsys) -> None:
    """The other direction: nothing gets flagged for having done work."""
    fleet = [_runner(31, description="busy", _recent_jobs=7)]
    runners_op._print_fleet(fleet, [], [])
    assert "<!" not in _row_for(capsys.readouterr().out, "busy")


# ---------------------------------------------------------------------------
# the branches above it keep winning — the re-key is not allowed to eat them
# ---------------------------------------------------------------------------

def test_stranded_work_behind_a_down_runner_still_reads_starved(capsys) -> None:
    fleet = [_runner(40, description="only-docker", status="offline",
                     contacted_at=_iso(3 * 86400))]
    runners_op._print_fleet(fleet, [_pending(1)], [])
    row = _row_for(capsys.readouterr().out, "only-docker")

    assert "<! STARVED" in row, (
        "work is queued behind a runner GitLab calls offline — the finding this "
        "op exists for, and it outranks the state marker"
    )
    assert "<! silent" not in row


def test_stranded_work_behind_an_online_runner_still_reads_unknown(capsys) -> None:
    """#805's branch: the queue is stalled but the evidence does not name a cause."""
    fleet = [_quiet_online(41) | {"description": "quiet-owner"}]
    runners_op._print_fleet(fleet, [_pending(1)], [])
    row = _row_for(capsys.readouterr().out, "quiet-owner")

    assert "<! UNKNOWN" in row
    assert "<! STARVED" not in row, "nothing here is demonstrably down"


def test_a_down_runner_beside_a_live_one_keeps_the_queue_s_unknown(capsys) -> None:
    """`unproven` stays in the disjunction, and this is the row that needs it.

    A job both runners may take, one demonstrably down and one merely unmeasured:
    `classify_queue` puts it in the unproven bucket, because not every candidate
    is down. The down runner's own liveness is not in question, so the re-key
    would hand it `<! silent` — losing #805's statement that work is stranded
    behind it for a reason nobody can prove. That is a queue claim, not a
    liveness claim, and it does not contradict the STATUS column the way #814
    does: `offline` says this host is down, `<! UNKNOWN` says the queue's fate
    is not settled by that alone.
    """
    down = _runner(60, description="down-owner", status="offline",
                   contacted_at=_iso(3 * 86400))
    quiet = _quiet_online(61) | {"description": "quiet-owner"}
    runners_op._print_fleet([down, quiet], [_pending(1)], [])
    body = capsys.readouterr().out.split("NOTE")[0]

    assert "<! UNKNOWN" in _row_for(body, "down-owner"), (
        "stranded work whose cause is unproven outranks the liveness marker"
    )
    assert "<! silent" not in _row_for(body, "down-owner")


# ---------------------------------------------------------------------------
# the predicate the marker is re-keyed onto, pinned directly
# ---------------------------------------------------------------------------

def test_liveness_unknown_covers_the_issue_shape() -> None:
    """The premise: the #814 row is the state `_liveness_unknown` already names."""
    runner = _quiet_online(21)

    assert runners_op._liveness_unknown(runner) is True
    assert runners_op._demonstrably_down(runner) is False
    assert runners_op._is_responsive(runner) is False, (
        "precondition: it falls through to the marker chain at all"
    )


def test_silent_and_unknown_partition_the_non_responsive_rows(capsys) -> None:
    """Exactly one of the two fires per unresponsive row, and one always does.

    A marker vocabulary with a gap is a row that says nothing; one with an
    overlap is a row that says two things. Neither is readable at a glance,
    which is the whole point of the column.
    """
    fleet = [
        _quiet_online(50) | {"description": "unknown-one"},
        _runner(51, description="down-one", status="stale",
                contacted_at=_iso(9 * 86400)),
        _runner(52, description="down-two", paused=True),
        _quiet_online(53) | {"description": "unknown-two"},
    ]
    runners_op._print_fleet(fleet, [], [])
    body = capsys.readouterr().out.split("NOTE")[0]

    for name in ("unknown-one", "unknown-two", "down-one", "down-two"):
        row = _row_for(body, name)
        flagged = [m for m in ("<! silent", "<! UNKNOWN") if m in row]
        assert len(flagged) == 1, f"{name} carried {flagged}, expected exactly one"
        expected = "<! UNKNOWN" if name.startswith("unknown") else "<! silent"
        assert flagged == [expected], f"{name} carried {flagged}"
