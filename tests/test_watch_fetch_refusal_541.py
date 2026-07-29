"""Issue #541 — three watch sources swallowed a failed fetch.

`gl-pipeline`, `github-pr` and `gitlab-mr` each ended their `_fetch` failure
path on the same line:

    return [], state  # transient — try again next tick

For a genuine network blip that is correct and this file does not ask to change
it. The defect is that a **permanent** failure was byte-identical to it — an
expired token, a renamed repo, `glab`/`gh` gone from PATH — forever. The watcher
stayed alive, `watches` listed it as running, and the reader concluded "nothing
has happened on my MR" when the truth was "nothing has been observed for six
hours". This repository's house defect: an absence produced by the tool read as
an absence in the world.

The fix is the `gh-run` shape from #524/PR #540, ported:

  * `_fetch` returns `(data, "")` or `(None, why)` — the reason survives
    instead of collapsing to `None`.
  * The first failure of a streak becomes a `*_unreachable` event carrying the
    classified message.
  * It is **edge-triggered** on a `lookup` flag in state, so it fires once per
    outage. A signal that repeats every 30s gets muted, which is the loud
    failure traded for a quiet one by a longer route.
  * State is carried forward, so a change that landed *during* the outage is
    still announced on recovery rather than swallowed as already-seen.

Every test here would pass trivially if the poller did nothing, *except* the
ones that assert an event key — which is the point: the whole bug is the
missing event, and each assertion below names it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
SOURCES = WATCH_DIR / "sources"
sys.path.insert(0, str(WATCH_DIR))

_d_spec = importlib.util.spec_from_file_location("watch_dispatcher_541", WATCH_DIR / "dispatcher.py")
assert _d_spec is not None and _d_spec.loader is not None
dispatcher = importlib.util.module_from_spec(_d_spec)
_d_spec.loader.exec_module(dispatcher)

_f_spec = importlib.util.spec_from_file_location("watch_defaults_541", WATCH_DIR / "defaults.py")
assert _f_spec is not None and _f_spec.loader is not None
defaults = importlib.util.module_from_spec(_f_spec)
_f_spec.loader.exec_module(defaults)


def _load(source: str):
    spec = importlib.util.spec_from_file_location(
        f"poller_541_{source.replace('-', '_')}", SOURCES / source / "poller.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _keys(events):
    return [e["event"] for e in events]


def _ok(data):
    return (data, "")


def _down(msg="ERROR: glab not authenticated. Run: glab auth login"):
    return (None, msg)


# The three sources under fix, with the payload their `_fetch` returns on a good
# poll, the event key a refusal must produce, and a *later* change that has to
# survive the outage and still be announced on recovery.
#
# `state_after` is the state a healthy first poll leaves behind — the baseline
# an outage must not erase.
CASES = {
    "gl-pipeline": {
        "event": "pipeline_unreachable",
        "id": "151111",
        "before": {"id": "151111", "status": "running", "web_url": "https://ex/p/151111"},
        "after": {"id": "151111", "status": "failed", "web_url": "https://ex/p/151111"},
        "recovery_event": "pipeline_failed",
    },
    "github-pr": {
        "event": "pr_unreachable",
        "id": "540",
        "before": {"state": "OPEN", "mergeable": "MERGEABLE", "title": "t",
                   "url": "https://ex/pr/540", "reviewDecision": None,
                   "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
                   "number": 540, "headRefName": "b", "comments": []},
        "after": {"state": "MERGED", "mergeable": "MERGEABLE", "title": "t",
                  "url": "https://ex/pr/540", "reviewDecision": None,
                  "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
                  "number": 540, "headRefName": "b", "comments": []},
        "recovery_event": "merged",
    },
    "gitlab-mr": {
        "event": "mr_unreachable",
        "id": "21803",
        "before": {"iid": 21803, "title": "t", "state": "opened", "has_conflicts": False,
                   "merge_status": "can_be_merged",
                   "head_pipeline": {"id": "9", "status": "running"},
                   "web_url": "https://ex/mr/21803", "user_notes_count": 0},
        "after": {"iid": 21803, "title": "t", "state": "merged", "has_conflicts": False,
                  "merge_status": "can_be_merged",
                  "head_pipeline": {"id": "9", "status": "running"},
                  "web_url": "https://ex/mr/21803", "user_notes_count": 0},
        "recovery_event": "merged",
    },
}

ALL_SOURCES = sorted(CASES)


@pytest.fixture(autouse=True)
def _no_real_cli():
    """gitlab-mr makes a second `_glab_api` call on a red-pipeline transition.

    Nothing in this file exercises it, and letting it through would shell out to
    a real `glab`.
    """
    mod = _load("gitlab-mr")
    with mock.patch.object(mod, "_glab_api", return_value=([], "")):
        yield


# --- 1. A failed fetch produces an event -------------------------------------

@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_failed_fetch_is_reported_not_swallowed(source: str) -> None:
    """RED before the fix: `poll` returned `([], state)` — an empty event list,
    identical to a poll where genuinely nothing happened."""
    case = CASES[source]
    poller = _load(source)
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["before"])):
        _, state = poller.poll({}, {"id": case["id"]})
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        events, new_state = poller.poll(state, {"id": case["id"]})
    assert _keys(events) == [case["event"]]
    assert new_state["lookup"] == "unavailable"


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_the_event_carries_the_classified_reason(source: str) -> None:
    """"Could not tell" without saying why is a second silence. The message is
    the classified one — `glab not authenticated`, not a raw stderr dump."""
    case = CASES[source]
    poller = _load(source)
    msg = "ERROR: glab not authenticated. Run: glab auth login"
    with mock.patch.object(poller, "_fetch", return_value=_down(msg)):
        events, _ = poller.poll({}, {"id": case["id"]})
    assert events[0]["payload"]["error"] == msg
    assert msg in events[0]["notify_message"]
    assert events[0]["notify_title"]


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_the_event_carries_last_known_state_not_a_blank(source: str) -> None:
    """An outage report that shows nothing is only half a report. The reader
    wants to know what the last thing we *could* see was."""
    case = CASES[source]
    poller = _load(source)
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["before"])):
        _, state = poller.poll({}, {"id": case["id"]})
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        events, _ = poller.poll(state, {"id": case["id"]})
    payload = events[0]["payload"]
    last_known = {k: v for k, v in payload.items() if k.startswith("last_known_")}
    assert last_known, f"{source}: no last_known_* field on the payload"
    assert any(v for v in last_known.values()), f"{source}: every last_known_* is blank"


# --- 2. A repeated failure does NOT re-fire -----------------------------------

@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_repeated_failure_is_silent(source: str) -> None:
    """Edge-triggered, not level-triggered. Ten failing polls, one event — an
    alert that repeats every 30s for six hours is one people mute, and a muted
    alert is the original silence by a longer route."""
    case = CASES[source]
    poller = _load(source)
    state: dict = {}
    fired = []
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        for _ in range(10):
            events, state = poller.poll(state, {"id": case["id"]})
            fired.extend(_keys(events))
    assert fired == [case["event"]]


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_second_outage_after_a_recovery_fires_again(source: str) -> None:
    """Once per outage, not once per lifetime. The flag has to be re-armed by a
    successful poll or the second token expiry is silent."""
    case = CASES[source]
    poller = _load(source)
    ctx = {"id": case["id"]}
    fired = []
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        events, state = poller.poll({}, ctx)
        fired.extend(_keys(events))
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["before"])):
        _, state = poller.poll(state, ctx)
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        events, state = poller.poll(state, ctx)
        fired.extend(_keys(events))
    assert fired == [case["event"], case["event"]]


# --- 3. Recovery still announces what changed during the outage ---------------

@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_change_during_the_outage_is_announced_on_recovery(source: str) -> None:
    """The part a naive port breaks. These three carry richer state than
    `gh-run` — MR state, conflicts, notes count, pipeline id — and a failure
    branch that rebuilt state from scratch would either lose the baseline or
    make the post-outage read incomparable, so the transition that happened
    while we were blind would never be announced at all."""
    case = CASES[source]
    poller = _load(source)
    ctx = {"id": case["id"]}
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["before"])):
        _, state = poller.poll({}, ctx)
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        for _ in range(3):
            _, state = poller.poll(state, ctx)
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["after"])):
        events, state = poller.poll(state, ctx)
    assert case["recovery_event"] in _keys(events)
    assert state["lookup"] == "ok"


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_an_outage_over_an_unchanged_world_recovers_quietly(source: str) -> None:
    """The other half of the same guarantee: carrying state forward must not
    re-announce a transition that was already reported before the outage."""
    case = CASES[source]
    poller = _load(source)
    ctx = {"id": case["id"]}
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["before"])):
        _, state = poller.poll({}, ctx)
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        _, state = poller.poll(state, ctx)
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["before"])):
        events, _ = poller.poll(state, ctx)
    assert events == []


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_an_outage_does_not_retire_the_watcher(source: str) -> None:
    """A network blip must not make the watcher terminal — the process would
    exit and the run nobody is now watching would never be reported."""
    case = CASES[source]
    poller = _load(source)
    ctx = {"id": case["id"]}
    with mock.patch.object(poller, "_fetch", return_value=_ok(case["before"])):
        _, state = poller.poll({}, ctx)
    with mock.patch.object(poller, "_fetch", return_value=_down()):
        _, state = poller.poll(state, ctx)
    assert poller.is_terminal(state) is False


# --- 4. `_fetch` classifies rather than collapsing ----------------------------

@pytest.mark.parametrize("source", ALL_SOURCES)
def test_fetch_returns_a_reason_for_every_failure_mode(source: str) -> None:
    """`_fetch` used to answer `None` to a 404, a 401, a timeout, a missing
    binary and unparseable JSON alike. Each now carries its own text."""
    import subprocess

    case = CASES[source]
    poller = _load(source)
    seam = "_glab_api_cli" if source != "github-pr" else "_gh"

    def _proc(returncode: int, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess(args=[], returncode=returncode,
                                           stdout=stdout, stderr=stderr)

    reasons = []
    for side_effect, kwargs in [
        (None, {"return_value": _proc(1, stderr="404 Not Found")}),
        (None, {"return_value": _proc(1, stderr="401 Unauthorized")}),
        (None, {"return_value": _proc(0, stdout="not json")}),
        (FileNotFoundError(), {}),
        (subprocess.TimeoutExpired(cmd="x", timeout=1), {}),
    ]:
        patch_kwargs = dict(kwargs)
        if side_effect is not None:
            patch_kwargs["side_effect"] = side_effect
        with mock.patch.object(poller, seam, **patch_kwargs):
            data, why = poller._fetch(case["id"])
        assert data is None
        assert why, "a failure with no reason is the bug being fixed"
        reasons.append(why)

    assert len(set(reasons)) >= 3, f"{source}: failure modes collapsed to {set(reasons)}"
    assert any("not found" in r.lower() for r in reasons)
    assert any("auth" in r.lower() for r in reasons)


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_missing_cli_binary_does_not_escape_poll(source: str) -> None:
    """`gh`/`glab` gone from PATH raised out of `poll()` for github-pr and out
    of gl-pipeline on a timeout (`TimeoutExpired` is a `SubprocessError`, not an
    `OSError`). The dispatcher caught it into `last_error` and slept — no event,
    no notification, and the state file is not something anyone reads."""
    import subprocess

    case = CASES[source]
    poller = _load(source)
    seam = "_glab_api_cli" if source != "github-pr" else "_gh"
    for exc in (FileNotFoundError(), OSError("boom"),
                subprocess.TimeoutExpired(cmd="x", timeout=1)):
        with mock.patch.object(poller, seam, side_effect=exc):
            events, _ = poller.poll({}, {"id": case["id"]})
        assert _keys(events) == [case["event"]], f"{source}: {type(exc).__name__}"


# --- 5. events.json / only= coverage -----------------------------------------

@pytest.mark.parametrize("source", ALL_SOURCES)
def test_events_json_declares_the_unreachable_key(source: str) -> None:
    """`only=` filters against the declared vocabulary — an event the poller
    emits but events.json omits is unfilterable and undiscoverable."""
    case = CASES[source]
    declared = json.loads((SOURCES / source / "events.json").read_text(encoding="utf-8"))
    keys = [e["key"] for e in declared["events"]]
    assert case["event"] in keys
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_no_existing_event_name_moved(source: str) -> None:
    """The #439/#464 invariant: adding a name must not rename, reorder-away or
    drop one. `only=` strings live in user config and in radar."""
    existing = {
        "gl-pipeline": {"pipeline_succeeded", "pipeline_failed", "pipeline_canceled",
                        "pipeline_running"},
        "github-pr": {"checks_failed", "checks_succeeded", "checks_pending",
                      "review_approved", "review_changes_requested", "comment_added",
                      "merged", "closed", "conflicts_appeared"},
        "gitlab-mr": {"pipeline_failed", "pipeline_succeeded", "pipeline_running",
                      "comment_added", "merged", "closed", "conflicts_appeared"},
    }[source]
    declared = json.loads((SOURCES / source / "events.json").read_text(encoding="utf-8"))
    keys = {e["key"] for e in declared["events"]}
    assert existing <= keys, f"{source}: lost {sorted(existing - keys)}"


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_the_unreachable_event_is_filterable(source: str) -> None:
    """A watcher spawned with an `only=` that excludes the new key must still
    behave — the event is opt-out, not unconditional."""
    case = CASES[source]
    _, _, only = dispatcher._parse_args([source, case["id"], "only=merged"])
    assert case["event"] not in only


def test_mr_unreachable_is_in_default_only() -> None:
    """`DEFAULT_ONLY` is the gitlab-mr filter every "watch everything of mine"
    flow spawns with. A watcher that cannot see is exactly what that reader
    needs told: it is actionable, it is otherwise entirely silent, and it is
    edge-triggered so it costs one line per outage — the same argument the file
    already makes for `conflicts_appeared` and `comment_added`.

    Left out, the default configuration keeps the bug: a radar board full of
    live-looking rows, none of them observing anything."""
    events = defaults.DEFAULT_ONLY.split(",")
    assert "mr_unreachable" in events
    for previously_on in ("pipeline_failed", "pipeline_succeeded", "comment_added",
                          "merged", "closed", "conflicts_appeared"):
        assert previously_on in events
    assert "pipeline_running" not in events


# --- 6. The bootstrap flag survives a cold-start outage -----------------------

def test_a_lookup_only_state_still_counts_as_bootstrap() -> None:
    """#464 keys "this watcher is describing what it found, not what changed"
    on state being empty. A watcher whose *first* poll fails now writes
    `{lookup, error}` — non-empty, but not an observation of anything. Without
    this, the first successful poll after a cold-start outage would report an
    already-red MR as a live transition."""
    assert dispatcher._is_bootstrap_state({}) is True
    assert dispatcher._is_bootstrap_state({"lookup": "unavailable", "error": "x"}) is True
    assert dispatcher._is_bootstrap_state({"lookup": "ok", "mr_state": "opened"}) is False
    assert dispatcher._is_bootstrap_state({"pipeline_status": "running"}) is False
