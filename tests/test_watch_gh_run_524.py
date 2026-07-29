"""Tests for the `gh-run` watch source — issue #524.

`gh-run` is the GitHub twin of `gl-pipeline`: it watches a **workflow run by
id**, independent of any pull request. `github-pr` already reports CI for runs
attached to a PR (`checks_failed` / `checks_succeeded` / `checks_pending`); the
hole `gh-run` fills is every run that has no PR to hang off — a `master` run
after a merge, a manual `workflow_dispatch`, a `gh run rerun` whose new id
nothing is following.

`gh` is mocked at the poller's `_fetch` seam — same style as
tests/test_watch_gl_pipeline_353.py — so these never touch a live run.

Two things get pinned harder than the happy path, because both are house
defects with filings behind them:

  * A conclusion the event map does not name must not become silence
    (#445/#454 — a tally that counted `CANCELLED` as neither pass nor pending,
    and a run that concluded `failure` read as still waiting).
  * A `gh run view` that fails, times out, is unauthenticated or returns junk
    must not render as "nothing happened" — three states, not two.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

_d_spec = importlib.util.spec_from_file_location("watch_dispatcher_524", WATCH_DIR / "dispatcher.py")
assert _d_spec is not None and _d_spec.loader is not None
dispatcher = importlib.util.module_from_spec(_d_spec)
_d_spec.loader.exec_module(dispatcher)

SOURCE_DIR = WATCH_DIR / "sources" / "gh-run"
POLLER = SOURCE_DIR / "poller.py"
EVENTS_JSON = SOURCE_DIR / "events.json"

RUN_ID = "18234567890"
CTX = {"id": RUN_ID}


def _load_poller():
    spec = importlib.util.spec_from_file_location("gh_run_poller", POLLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(status="in_progress", conclusion=None, name="CI", url=f"https://ex/r/{RUN_ID}"):
    """A `gh run view --json ...` payload. `conclusion` is null until completed."""
    return {
        "databaseId": int(RUN_ID),
        "status": status,
        "conclusion": conclusion,
        "workflowName": name,
        "url": url,
        "headBranch": "master",
        "event": "push",
    }


def _ok(data):
    """The (data, error) pair `_fetch` returns on a successful lookup."""
    return (data, "")


def _unreachable(msg="ERROR: gh CLI not authenticated. Run: gh auth login"):
    """The (data, error) pair `_fetch` returns when it could not look."""
    return (None, msg)


def _keys(events):
    return [e["event"] for e in events]


# --- Registration -------------------------------------------------------------

def test_dispatcher_resolves_gh_run_source() -> None:
    """RED before fix: no sources/gh-run/poller.py -> watch:gh-run:<ID> printed
    'unknown source' and exited 1."""
    mod = dispatcher._load_source("gh-run")
    assert mod is not None
    assert hasattr(mod, "poll")
    assert hasattr(mod, "INTERVAL")
    assert hasattr(mod, "is_terminal")


def test_events_json_declares_every_event_the_poller_can_emit() -> None:
    """The declared vocabulary is what `only=` filters against — an event the
    poller emits but events.json omits is unfilterable and undiscoverable."""
    import json
    declared = {e["key"] for e in json.loads(EVENTS_JSON.read_text(encoding="utf-8"))["events"]}
    poller = _load_poller()
    emitted = set()
    cases = [
        ({"status": "queued"}, _run(status="in_progress")),
        ({"status": "in_progress"}, _run(status="completed", conclusion="success")),
        ({"status": "in_progress"}, _run(status="completed", conclusion="failure")),
        ({"status": "in_progress"}, _run(status="completed", conclusion="cancelled")),
        ({"status": "in_progress"}, _run(status="completed", conclusion="action_required")),
        ({"status": "in_progress"}, _run(status="completed", conclusion="skipped")),
    ]
    for state, data in cases:
        with mock.patch.object(poller, "_fetch", return_value=_ok(data)):
            events, _ = poller.poll(state, CTX)
        emitted.update(_keys(events))
    with mock.patch.object(poller, "_fetch", return_value=_unreachable()):
        events, _ = poller.poll({"status": "in_progress"}, CTX)
    emitted.update(_keys(events))
    assert emitted <= declared, f"undeclared events: {sorted(emitted - declared)}"
    assert declared <= emitted, f"declared but unreachable: {sorted(declared - emitted)}"


# --- Baseline / in-flight -----------------------------------------------------

def test_first_poll_baselines_without_event() -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_ok(_run(status="in_progress"))):
        events, new_state = poller.poll({}, CTX)
    assert events == []
    assert new_state["status"] == "in_progress"


def test_null_conclusion_while_in_flight_emits_no_phantom_event() -> None:
    """`conclusion` is null until `status` reaches completed. Branching on
    conclusion first would read every in-flight poll as an unknown conclusion
    and fire `run_inconclusive` on a perfectly healthy running job."""
    poller = _load_poller()
    for status in ("queued", "in_progress", "waiting", "requested", "pending"):
        with mock.patch.object(poller, "_fetch", return_value=_ok(_run(status=status, conclusion=None))):
            events, new_state = poller.poll({"status": "queued"} if status != "queued" else {}, CTX)
        assert all(e["event"] != "run_inconclusive" for e in events), status
        assert new_state["status"] == status


def test_queued_to_in_progress_emits_run_started() -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_ok(_run(status="in_progress"))):
        events, _ = poller.poll({"status": "queued"}, CTX)
    assert _keys(events) == ["run_started"]
    assert events[0]["payload"]["run_id"] == RUN_ID


def test_no_change_emits_nothing() -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_ok(_run(status="in_progress"))):
        events, _ = poller.poll({"status": "in_progress"}, CTX)
    assert events == []


# --- The four named conclusions -----------------------------------------------

@pytest.mark.parametrize("conclusion,event", [
    ("success", "run_succeeded"),
    ("failure", "run_failed"),
    ("cancelled", "run_cancelled"),
])
def test_completion_emits_the_named_event(conclusion: str, event: str) -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion=conclusion))):
        events, new_state = poller.poll({"status": "in_progress"}, CTX)
    assert _keys(events) == [event]
    assert events[0]["payload"]["conclusion"] == conclusion
    assert events[0]["payload"]["url"]
    assert events[0]["notify_title"]
    assert new_state["conclusion"] == conclusion


# --- The tail conclusions: #445 / #454 territory -------------------------------

@pytest.mark.parametrize("conclusion", ["timed_out", "startup_failure"])
def test_red_tail_conclusions_are_failures_not_silence(conclusion: str) -> None:
    """`timed_out` and `startup_failure` are red in the GitHub UI. Folding them
    onto `run_failed` is a decision; dropping them is the #454 defect."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion=conclusion))):
        events, _ = poller.poll({"status": "in_progress"}, CTX)
    assert _keys(events) == ["run_failed"]
    assert events[0]["payload"]["conclusion"] == conclusion


@pytest.mark.parametrize("conclusion", ["neutral", "skipped", "stale"])
def test_verdictless_tail_conclusions_are_reported_as_inconclusive(conclusion: str) -> None:
    """A run that ended without a pass/fail verdict still ended. Reporting it as
    neither succeeded nor failed is honest; reporting nothing is not."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion=conclusion))):
        events, _ = poller.poll({"status": "in_progress"}, CTX)
    assert _keys(events) == ["run_inconclusive"]
    assert events[0]["payload"]["conclusion"] == conclusion
    assert events[0]["payload"]["recognised"] == "yes"


def test_action_required_gets_its_own_event() -> None:
    """`action_required` is a run waiting on a human — the one tail conclusion
    where the reader has something to do."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion="action_required"))):
        events, _ = poller.poll({"status": "in_progress"}, CTX)
    assert _keys(events) == ["run_action_required"]


def test_unrecognised_conclusion_is_reported_verbatim_not_swallowed() -> None:
    """A conclusion GitHub adds after this code was written must still reach the
    reader, flagged as unrecognised rather than quietly mapped to nothing."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion="quantum_flux"))):
        events, _ = poller.poll({"status": "in_progress"}, CTX)
    assert _keys(events) == ["run_inconclusive"]
    assert events[0]["payload"]["conclusion"] == "quantum_flux"
    assert events[0]["payload"]["recognised"] == "no"
    assert "quantum_flux" in events[0]["notify_message"]


def test_completed_with_null_conclusion_is_not_silence() -> None:
    """Terminal *and* verdict-less: the watcher stops here, so if this poll says
    nothing the run is never reported at all."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion=None))):
        events, new_state = poller.poll({"status": "in_progress"}, CTX)
    assert _keys(events) == ["run_inconclusive"]
    assert events[0]["payload"]["recognised"] == "no"
    assert poller.is_terminal(new_state) is True


# --- Terminality --------------------------------------------------------------

def test_completed_is_terminal() -> None:
    poller = _load_poller()
    for conclusion in ("success", "failure", "cancelled", "skipped", "quantum_flux", None):
        with mock.patch.object(poller, "_fetch",
                               return_value=_ok(_run(status="completed", conclusion=conclusion))):
            _, new_state = poller.poll({"status": "in_progress"}, CTX)
        assert poller.is_terminal(new_state) is True, conclusion


def test_in_flight_is_not_terminal() -> None:
    poller = _load_poller()
    for status in ("queued", "in_progress", "waiting", "requested", ""):
        assert poller.is_terminal({"status": status}) is False
    assert poller.is_terminal({}) is False


def test_terminal_conclusion_emits_once_not_every_tick() -> None:
    """Edge-triggered: re-polling a state that already saw the completion must
    not re-notify (the dispatcher stops, but state can be replayed)."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion="failure"))):
        first, state = poller.poll({"status": "in_progress"}, CTX)
        second, _ = poller.poll(state, CTX)
    assert _keys(first) == ["run_failed"]
    assert second == []


# --- Three states, not two: a lookup that failed is not a quiet run -----------

def test_unreachable_run_emits_run_unreachable_not_silence() -> None:
    """The house defect: an absence produced by the tool read as an absence in
    the world. A `gh run view` that failed must say so."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_unreachable()):
        events, new_state = poller.poll({"status": "in_progress"}, CTX)
    assert _keys(events) == ["run_unreachable"]
    assert new_state["lookup"] == "unavailable"
    assert events[0]["notify_title"]


def test_unreachable_carries_the_gh_error_so_the_reader_can_act() -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_unreachable("ERROR: gh CLI not authenticated. Run: gh auth login")):
        events, _ = poller.poll({"status": "in_progress"}, CTX)
    assert "auth" in events[0]["payload"]["error"]


def test_unreachable_does_not_repeat_every_tick() -> None:
    """Loud once, not loud forever — an outage that fires every 30s is a signal
    people mute, which is trading the loud failure for a quiet one by a longer
    route."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_unreachable()):
        first, state = poller.poll({"status": "in_progress"}, CTX)
        second, state = poller.poll(state, CTX)
    assert _keys(first) == ["run_unreachable"]
    assert second == []
    assert state["lookup"] == "unavailable"


def test_unreachable_preserves_last_known_status_and_stays_non_terminal() -> None:
    """A failed lookup must not retire the watcher — that would turn a network
    blip into a run nobody is watching any more."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_unreachable()):
        _, new_state = poller.poll({"status": "in_progress", "url": "https://ex/r/1"}, CTX)
    assert new_state["status"] == "in_progress"
    assert poller.is_terminal(new_state) is False


def test_recovery_after_unreachable_reports_the_outcome_it_missed() -> None:
    """The run completed while we could not see it. The completion is still the
    news; the outage must not have consumed the transition."""
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_unreachable()):
        _, state = poller.poll({"status": "in_progress"}, CTX)
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_run(status="completed", conclusion="failure"))):
        events, state = poller.poll(state, CTX)
    assert "run_failed" in _keys(events)
    assert state["lookup"] == "ok"


def test_fetch_reports_junk_json_as_unreachable_not_as_a_run() -> None:
    """Invalid JSON is a lookup that could not tell, not an empty run."""
    poller = _load_poller()
    r = mock.Mock(returncode=0, stdout="not json at all", stderr="")
    with mock.patch.object(poller, "_gh", return_value=r):
        data, err = poller._fetch(RUN_ID)
    assert data is None
    assert err


def test_fetch_classifies_gh_failure_through_the_shared_error_formatter() -> None:
    """gh's stderr becomes an actionable message rather than a raw dump —
    reusing the classifier the `gh-run` op already ships."""
    poller = _load_poller()
    r = mock.Mock(returncode=1, stdout="", stderr="HTTP 401: Bad credentials")
    with mock.patch.object(poller, "_gh", return_value=r):
        data, err = poller._fetch(RUN_ID)
    assert data is None
    assert "auth" in err.lower()


def test_fetch_reports_a_timeout_rather_than_raising_into_the_dispatcher() -> None:
    import subprocess
    poller = _load_poller()
    with mock.patch.object(poller, "_gh", side_effect=subprocess.TimeoutExpired("gh", 15)):
        data, err = poller._fetch(RUN_ID)
    assert data is None
    assert err


def test_missing_gh_binary_is_reported() -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_gh", side_effect=FileNotFoundError()):
        data, err = poller._fetch(RUN_ID)
    assert data is None
    assert "gh" in err.lower()


# --- Reuse, not a second copy of the CLI plumbing -----------------------------

def test_gh_helper_is_the_shared_one_not_a_local_reimplementation() -> None:
    poller = _load_poller()
    assert poller._gh.__module__ == "github_pr_op"


def test_error_formatter_is_the_gh_run_op_one() -> None:
    poller = _load_poller()
    assert poller._format_error.__module__ == "github_run_op"
