"""`radar_report`’s live count rests on an unstated adjacency (#807).

The shape, in `presets/gitlab/runners.py`:

* `annotate_recent_work` is skipped when the pending queue is empty —
  deliberately, so a registered tier with nothing queued stays cheap.
* `_is_responsive` reads `_recent_jobs`, the annotation that call writes, and
  raises `UnannotatedFleetError` when it is absent.
* the `live = [...]` count that calls it sits below an early return taken on
  exactly the empty-queue path.

Today that is correct, and only because the skip and the count are on mutually
exclusive branches. Nothing stated the invariant and nothing tested it — you
have to hold both branches in your head at once to see why it is safe, and one
reordering (hoisting the count, merging the branches, adding an early return)
produces a crash on a live radar run.

**What the file already had is not this pin.** `test_the_tier_does_not_report_a
_live_count_it_declined_to_measure` drives `radar_report` with `_runner()`,
whose baseline carries `_recent_jobs` and `_live_jobs_checked` because liveness
is only defined on an annotated record. So the fleet in that test is annotated
before radar sees it: hoisting the count would not raise there, and the
assertion is about the wording of the summary line rather than the invariant.
Everything below feeds `radar_report` records in the shape GitLab actually
hands them over — both annotator marks absent — so the branch is the only
thing keeping the count from asking a question nobody gathered evidence for.

The fix is a pin, not a refactor. Making `_is_responsive` decline instead of
raise was weighed and refused: an unannotated fleet at that point is a
programming error, not an unmeasurable subject, and turning a loud crash into a
quiet UNKNOWN is the trade this repo keeps warning against.
"""
from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest

OP_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "runners.py"

_spec = importlib.util.spec_from_file_location("gl_runners_807", OP_PATH)
assert _spec is not None and _spec.loader is not None
runners_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runners_op)


def _iso(seconds_ago: float) -> str:
    moment = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(seconds=seconds_ago))
    return moment.isoformat().replace("+00:00", "Z")


def _raw(rid: int = 1, **over) -> dict:
    """A runner exactly as GitLab lists it — before either annotator ran.

    Deliberately NOT `test_gl_runners._runner`, which carries both annotation
    marks in its baseline. A fixture that arrives pre-annotated cannot detect
    the defect this file exists for.
    """
    base = {
        "id": rid, "description": f"runner-{rid}", "tag_list": ["docker"],
        "run_untagged": False, "status": "online", "active": True,
        "paused": False, "contacted_at": _iso(10),
        "job_execution_status": "idle",
    }
    base.update(over)
    return base


def _api_stub(runners=None, pending=None, running=None, history=None):
    def fake_api(endpoint, paginate=False, timeout=20):
        if "runners?" in endpoint:
            return (runners or [], None)
        if "scope[]=pending" in endpoint:
            return (pending or [], None)
        if "scope[]=running" in endpoint:
            return (running or [], None)
        return (history or [], None)

    return fake_api


def _drive(monkeypatch, fleet, pending=None, history=None):
    """One `radar_report`, returning (lines, healthy, records at classify_queue).

    The spy wraps `classify_queue` rather than replacing it: that call sits
    between the conditional annotation and the live count, so the records it
    receives are exactly the ones the count would be asked about.
    """
    seen: list[dict] = []
    real = runners_op.classify_queue

    def spy(runners, queue):
        seen.extend(runners)
        return real(runners, queue)

    monkeypatch.setattr(runners_op, "_api", _api_stub(
        runners=fleet, pending=pending, history=history))
    monkeypatch.setattr(runners_op, "_fetch_details", lambda listed: {})
    monkeypatch.setattr(runners_op, "classify_queue", spy)
    lines, healthy = runners_op.radar_report({})
    return lines, healthy, seen


# ---------------------------------------------------------------------------
# the invariant, from both ends
# ---------------------------------------------------------------------------

def test_the_empty_queue_path_never_reaches_the_liveness_question(monkeypatch) -> None:
    """The pin. Hoisting the live count above the early return, merging the two
    branches, or adding one more early return below the count all raise here.
    """
    lines, healthy, seen = _drive(monkeypatch, [_raw(1), _raw(2)])

    assert healthy is True
    assert "2 runners" in "\n".join(lines)


def test_and_the_fleet_on_that_path_is_genuinely_unannotated(monkeypatch) -> None:
    """Without this the test above passes for the wrong reason.

    A fixture that happened to carry `_recent_jobs` would make the run safe by
    accident of the data rather than by the arrangement of the branches, and
    the pin would go quietly vacuous the day someone 'tidied' the fixture.
    """
    _lines, _healthy, seen = _drive(monkeypatch, [_raw(1)])

    assert seen, "classify_queue must have been reached at all"
    assert runners_op._missing_annotations(seen[0]) == ["annotate_recent_work"]
    with pytest.raises(runners_op.UnannotatedFleetError):
        runners_op._is_responsive(seen[0])


def test_classify_queue_asks_nothing_of_an_unannotated_fleet_when_nothing_waits(
) -> None:
    """The other half of the exclusivity, at the one call between the skip and
    the count. `classify_queue` loops over the queue, so an empty queue means
    `_is_responsive` is never consulted — which is what lets `radar_report`
    reach its early return rather than raising on the way there."""
    assert runners_op.classify_queue([_raw(1), _raw(2)], []) == ({}, {})


def test_a_non_empty_queue_annotates_before_it_counts(monkeypatch) -> None:
    """The path that does reach the count must have run the annotator first.

    Same invariant read the other way round: it is not that the count is never
    reached, it is that it is only reached where the evidence exists.
    """
    pending = [{"tag_list": ["docker"], "created_at": _iso(1800)}]
    lines, healthy, seen = _drive(monkeypatch, [_raw(1)], pending=pending)

    assert seen and runners_op._missing_annotations(seen[0]) == []
    assert healthy is True
    assert "1/1 runners live" in "\n".join(lines)


@pytest.mark.parametrize("pending", [
    [],
    [{"tag_list": ["docker"], "created_at": _iso(1800)}],
    [{"tag_list": ["nobody-has-this"], "created_at": _iso(1800)}],
    [{"tag_list": ["docker"], "created_at": _iso(5)}],
])
def test_no_queue_shape_makes_radar_ask_about_a_fleet_it_did_not_annotate(
    monkeypatch, pending
) -> None:
    """Including the two shapes that reach the count without filling a bucket:
    a queue too young for the starvation floor, and one whose tags no runner
    carries. Both skip past `classify_queue`’s buckets and land on the live
    count — and both are on the annotated side of the branch, because the
    annotation is gated on the queue being non-empty rather than on what it
    contains."""
    _lines, _healthy, seen = _drive(
        monkeypatch, [_raw(1, tag_list=["docker"], contacted_at=_iso(4000))],
        pending=pending)

    assert seen, "classify_queue must have been reached at all"
    expected = [] if pending else ["annotate_recent_work"]
    assert runners_op._missing_annotations(seen[0]) == expected
