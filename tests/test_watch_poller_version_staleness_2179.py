"""A long-lived poller runs the code it was forked with, and nothing said
which version that was (issue #2179).

`8e9ac260` (#2090) landed a day after a still-running Slack poller forked.
The poller kept running its own five-day-old logic, `watches` printed
`STARTED` and nothing else, and no board anywhere compared that timestamp
against the code actually on disk. A stale poller occupies its slot exactly
like a current one, so the board read as covered while it was wrong.

Three states pin this file the same way #1183 pinned DELIVERY:

* `current` -- the poller's own recorded fork-time fingerprint matches the
  source on disk right now.
* `STALE` -- it does not: something under `presets/watch/` changed since this
  poller started.
* `unknown` -- the comparison could not be made at all (no fingerprint
  recorded, a fingerprint that could not be read, or this render's own read
  of its source failing). `unknown` must never render as `current` -- a
  poller nobody can prove is stale is not the same claim as one proven so.
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


transport = _module("watch_transport_2179", WATCH_DIR / "transport.py")
dispatcher = _module("watch_dispatcher_2179", WATCH_DIR / "dispatcher.py")
dispatcher.transport = transport


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == os.getpid())
    return tmp_path


def _watcher(root: Path, source: str, wid: str, *, fingerprint=None,
             fp_error="") -> None:
    (root / f"supertool-watch-{source}__{wid}.pid").write_text(str(os.getpid()))
    state: dict = {"last_event": {"event": "poked", "ts": "2026-09-02T00:00:00Z"}}
    if fingerprint is not None:
        state["forked_fingerprint"] = fingerprint
    if fp_error:
        state["forked_fingerprint_error"] = fp_error
    (root / f"supertool-watch-{source}__{wid}.state.json").write_text(
        json.dumps(state), encoding="utf-8")


def _row(out: str, wid: str) -> str:
    hits = [line for line in out.splitlines() if line.split()[1:2] == [wid]]
    assert len(hits) == 1, f"expected one row for {wid}, got {hits}"
    return hits[0]


# --------------------------------------------------------------------------
# source_fingerprint / version_state_of
# --------------------------------------------------------------------------

def test_source_fingerprint_answers_a_value_on_this_real_tree() -> None:
    value, why = transport.source_fingerprint()
    assert why == ""
    assert value


def test_source_fingerprint_declines_rather_than_guessing_on_an_empty_root(
        monkeypatch, tmp_path) -> None:
    empty = tmp_path / "empty_source_root"
    empty.mkdir()
    monkeypatch.setattr(transport, "Path", lambda *a, **k: empty)
    value, why = transport.source_fingerprint()
    assert value is None
    assert why


def test_matching_fingerprint_is_current() -> None:
    current, _why = transport.source_fingerprint()
    state, why = transport.version_state_of(current, "")
    assert state == transport.VERSION_CURRENT
    assert why == ""


def test_mismatched_fingerprint_is_stale() -> None:
    state, why = transport.version_state_of("0.000000", "")
    assert state == transport.VERSION_STALE
    assert "0.000000" in why


def test_no_recorded_fingerprint_is_unknown_never_current() -> None:
    """The pre-#2179 poller: no field at all. Must not read as `current`."""
    state, _why = transport.version_state_of(None, "")
    assert state == transport.VERSION_UNKNOWN


def test_a_recorded_fingerprint_error_is_unknown_never_current() -> None:
    state, why = transport.version_state_of(None, "could not stat")
    assert state == transport.VERSION_UNKNOWN
    assert "could not stat" in why


# --------------------------------------------------------------------------
# watches render
# --------------------------------------------------------------------------

def test_watches_marks_a_stale_poller(fleet, capsys) -> None:
    _watcher(fleet, "slack", "OLD", fingerprint="0.000000")
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "VERSION" in out
    assert "STALE" in _row(out, "OLD")
    assert "marked STALE in VERSION" in out  # named in the summary paragraph too


def test_watches_marks_a_current_poller(fleet, capsys) -> None:
    current, _why = transport.source_fingerprint()
    _watcher(fleet, "slack", "NEW", fingerprint=current)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "current" in _row(out, "NEW")
    assert "STALE" not in _row(out, "NEW")


def test_watches_marks_an_unlabelled_poller_unknown_not_current(fleet, capsys) -> None:
    """No fingerprint recorded at all -- a poller from before #2179."""
    _watcher(fleet, "slack", "PRE2179")
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    row = _row(out, "PRE2179")
    assert "unknown" in row
    assert "current" not in row
    assert "STALE" not in row


def test_stale_and_current_are_distinguishable_side_by_side(fleet, capsys) -> None:
    current, _why = transport.source_fingerprint()
    _watcher(fleet, "slack", "STALE1", fingerprint="0.000000")
    _watcher(fleet, "slack", "FRESH1", fingerprint=current)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "STALE" in _row(out, "STALE1")
    assert "STALE" not in _row(out, "FRESH1")
    assert "current" in _row(out, "FRESH1")
