"""`watches` told the operator that `unwatch` + `watch` forks a fresh poller
with an *empty* state, re-announcing everything as new on its first tick
(#2697). `cmd_unwatch` never touched the state file, and the fresh poller
`watch` forks reads that file back at its first tick -- so it resumes the old
`source_state` and, correctly, stays quiet unless the world actually changed.
The text was wrong, not the code: an operator who read "empty state" and then
saw silence read the silence as "nothing to report", which was right here
only by accident.

Five sites made the same false claim: `cmd_reload`'s docstring, its
dispatcher.py-fix note, `cmd_list`'s STALE and RELOADED footers, and a module
comment above `RELOAD_SIGNAL` -- plus `docs/presets/watch.md` and this
sibling test's own module docstring (`test_watch_reload_2212.py`). This pins
the operator-facing surfaces (the two `cmd_list` footers) so they describe
what `cmd_unwatch` + a fresh fork actually do: resume `source_state` from
disk, not start empty.

Every "must say resumed" case here has a "must not still say empty" partner:
the old wording is asserted absent from the same output the new wording is
asserted present in, so a revert of only half the change fails loudly rather
than passing on the half nobody re-checked.
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


transport = _module("watch_transport_2697", WATCH_DIR / "transport.py")
dispatcher = _module("watch_dispatcher_2697", WATCH_DIR / "dispatcher.py")
dispatcher.transport = transport

SOURCE = "slack"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == os.getpid())
    return tmp_path


def _watcher(root: Path, wid: str, *, fingerprint=None, reloaded_at=None,
             reloaded_fingerprint=None) -> None:
    (root / f"supertool-watch-{SOURCE}__{wid}.pid").write_text(str(os.getpid()))
    state: dict = {"last_event": {"event": "poked", "ts": "2026-09-24T00:00:00Z"}}
    if fingerprint is not None:
        state["forked_fingerprint"] = fingerprint
    if reloaded_at is not None:
        state["reloaded_at"] = reloaded_at
    if reloaded_fingerprint is not None:
        state["reloaded_fingerprint"] = reloaded_fingerprint
    (root / f"supertool-watch-{SOURCE}__{wid}.state.json").write_text(
        json.dumps(state), encoding="utf-8")


def test_stale_footer_says_state_is_resumed_not_empty(fleet, capsys) -> None:
    _watcher(fleet, "OLD", fingerprint="0.000000")
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "marked STALE in VERSION" in out
    assert "resumes the same state from disk" in out
    assert "empty state" not in out
    assert "empty `state`" not in out


def test_reloaded_footer_says_state_is_resumed_not_empty(fleet, capsys) -> None:
    current, _why = transport.source_fingerprint()
    _watcher(fleet, "RLD", fingerprint="0.000000",
             reloaded_at="2026-09-24T00:00:00Z", reloaded_fingerprint=current)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "marked RELOADED in VERSION" in out
    assert "resumes the prior state from disk" in out
    assert "empty state" not in out
    assert "empty `state`" not in out
