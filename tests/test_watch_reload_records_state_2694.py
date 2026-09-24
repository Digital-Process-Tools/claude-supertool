"""After a confirmed `:reload`, `watches` kept a poller STALE forever and
recommended `:reload` again as the fix (#2694).

`_reload_poller` (dispatcher.py:1263) re-imports the source's own
`poller.py` and emits `watcher_reloaded`, but never touched the state file
beyond that event -- `forked_fingerprint`, recorded once at fork, never
moved. `version_state_of` compares only that fork-time value against the
source on disk, so a reloaded poller stayed `STALE` no matter how many
times the recommended remedy ran.

The fix: `_reload_poller` now records `reloaded_at` and a fresh
`reloaded_fingerprint` on the state file when the reload succeeds, and
`version_state_of` gets a fourth state, `RELOADED`, for exactly that case --
poller.py is current as of the reload, but dispatcher.py/transport.py in
that running process are still the fork-time copies and cannot be swapped
by another `:reload`. `unwatch` + `watch` is the only way back to fully
current.

Every "must record" case here has a "must not" partner: a reload that
failed (either failure shape) must never write `reloaded_at`, the same way
it must never re-point `current` at the new module.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


transport = _module("watch_transport_2694", WATCH_DIR / "transport.py")
dispatcher = _module("watch_dispatcher_2694", WATCH_DIR / "dispatcher.py")
dispatcher.transport = transport

SOURCE = "gitlab-mr"
WATCHER = "2694"


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def emitted(monkeypatch):
    calls: list[dict] = []

    def _emit(source, watcher_id, event_key, payload, **kw):
        calls.append({"source": source, "id": watcher_id,
                      "event": event_key, "payload": payload, **kw})

    monkeypatch.setattr(dispatcher.transport, "emit_event", _emit)
    return calls


# ---------------------------------------------------------------------------
# `_reload_poller` records the reload on the state file
# ---------------------------------------------------------------------------

def test_a_successful_reload_records_reloaded_at_and_a_fingerprint(
        monkeypatch, emitted) -> None:
    old, new = object(), object()
    monkeypatch.setattr(dispatcher, "_load_source", lambda _n: new)
    result = dispatcher._reload_poller(SOURCE, WATCHER, old)
    assert result is new
    state = transport.read_state(SOURCE, WATCHER)
    assert state.get("reloaded_at"), "a successful reload must record when it happened"
    assert state.get("reloaded_fingerprint"), (
        "a successful reload must record a fingerprint of the source it just re-imported")


def test_a_failed_reload_never_records_reloaded_at(monkeypatch, emitted) -> None:
    """Must-not-fire twin: an import that raises must not claim the reload
    happened on the state file, the same way it must not swap the module."""
    old = object()

    def load_source(_n):
        raise SyntaxError("broken edit")

    monkeypatch.setattr(dispatcher, "_load_source", load_source)
    result = dispatcher._reload_poller(SOURCE, WATCHER, old)
    assert result is old
    state = transport.read_state(SOURCE, WATCHER)
    assert "reloaded_at" not in state, (
        "a reload that raised must not record reloaded_at -- nothing changed")


def test_a_reload_whose_source_no_longer_resolves_never_records_reloaded_at(
        monkeypatch, emitted) -> None:
    old = object()
    monkeypatch.setattr(dispatcher, "_load_source", lambda _n: None)
    dispatcher._reload_poller(SOURCE, WATCHER, old)
    state = transport.read_state(SOURCE, WATCHER)
    assert "reloaded_at" not in state


# ---------------------------------------------------------------------------
# `version_state_of` -- the fourth state
# ---------------------------------------------------------------------------

def test_a_reloaded_poller_with_a_matching_fingerprint_reads_as_reloaded_not_stale() -> None:
    """The bug's exact shape: a confirmed reload must never render as if it
    had never happened."""
    current, _why = transport.source_fingerprint()
    state, why = transport.version_state_of(
        "0.000000", "", reloaded_at="2026-09-24T08:30:55Z",
        reloaded_fingerprint=current)
    assert state == transport.VERSION_RELOADED
    assert state != transport.VERSION_STALE
    assert why


def test_a_reloaded_poller_whose_source_drifted_again_is_still_reloaded_not_stale() -> None:
    """Even if presets/watch/ changed again after the reload, the process is
    still mixed (dispatcher.py/transport.py are fork-time regardless) -- it
    must not silently fall back to plain STALE, which would recommend
    `:reload` again, the exact remedy the issue names as broken."""
    state, why = transport.version_state_of(
        "0.000000", "", reloaded_at="2026-09-24T08:30:55Z",
        reloaded_fingerprint="0.111111")
    assert state == transport.VERSION_RELOADED
    assert "0.111111" in why


def test_a_never_reloaded_poller_is_unaffected_by_the_new_parameters() -> None:
    """Must-fire control for the old behaviour: with no reload recorded at
    all, a stale fork-time fingerprint is still plain STALE."""
    state, _why = transport.version_state_of("0.000000", "")
    assert state == transport.VERSION_STALE


def test_a_never_reloaded_current_poller_is_still_current() -> None:
    current, _why = transport.source_fingerprint()
    state, _why = transport.version_state_of(current, "")
    assert state == transport.VERSION_CURRENT


# ---------------------------------------------------------------------------
# `watches` render
# ---------------------------------------------------------------------------

@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == os.getpid())
    return tmp_path


def _watcher(root: Path, source: str, wid: str, *, reloaded_at=None,
             reloaded_fingerprint=None) -> None:
    (root / f"supertool-watch-{source}__{wid}.pid").write_text(str(os.getpid()))
    state: dict = {
        "last_event": {"event": "poked", "ts": "2026-09-02T00:00:00Z"},
        "forked_fingerprint": "0.000000",
    }
    if reloaded_at is not None:
        state["reloaded_at"] = reloaded_at
    if reloaded_fingerprint is not None:
        state["reloaded_fingerprint"] = reloaded_fingerprint
    (root / f"supertool-watch-{source}__{wid}.state.json").write_text(
        json.dumps(state), encoding="utf-8")


def _row(out: str, wid: str) -> str:
    hits = [line for line in out.splitlines() if line.split()[1:2] == [wid]]
    assert len(hits) == 1, f"expected one row for {wid}, got {hits}"
    return hits[0]


def test_watches_marks_a_reloaded_poller_reloaded_not_stale(fleet, capsys) -> None:
    current, _why = transport.source_fingerprint()
    _watcher(fleet, "slack", "RELOADED1", reloaded_at="2026-09-24T08:30:55Z",
             reloaded_fingerprint=current)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    row = _row(out, "RELOADED1")
    assert "RELOADED" in row
    assert "STALE" not in row


def test_watches_footer_recommends_unwatch_watch_not_reload_for_a_reloaded_row(
        fleet, capsys) -> None:
    """The exact loop the issue reports: the STALE footer's `:reload`
    remedy must not be printed for a row that already reloaded."""
    current, _why = transport.source_fingerprint()
    _watcher(fleet, "slack", "RELOADED2", reloaded_at="2026-09-24T08:30:55Z",
             reloaded_fingerprint=current)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "RELOADED" in out
    assert "unwatch" in out and "watch" in out
    assert "marked STALE in VERSION" not in out


def test_watches_still_marks_a_never_reloaded_stale_poller_with_the_reload_remedy(
        fleet, capsys) -> None:
    """Must-fire control: a row that never reloaded still gets the original
    STALE footer naming `:reload` as the remedy."""
    _watcher(fleet, "slack", "STALE1")
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    row = _row(out, "STALE1")
    assert "STALE" in row
    assert "marked STALE in VERSION" in out


def test_the_change_is_findable():
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2694)
