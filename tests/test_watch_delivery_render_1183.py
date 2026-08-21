"""`radar` and `watches` read `last_emit`, so a fleet emitting into a dead
socket cannot render as a healthy one (issue #1183).

#1173 recorded, per watcher, what the socket write actually meant. Exactly one
surface consumed it — `channel:health` — and the two an operator actually looks
at did not. `watches` said a poller was alive; `radar` printed a board. Both
conditions hold for a poller whose every event has been landing in a socket
nobody reads, so the render invited the reader to conclude the opposite of the
truth.

Two things this file pins beyond the render:

* **A quiet fleet and a stranded one are distinguishable, and no threshold is
  involved.** A watcher with nothing to report never called `emit_event`, so it
  has no `last_emit` at all; a watcher shouting into a dead socket has one
  saying `no-listener`. The age of the record is not consulted anywhere, and an
  unreadable state file is a third answer rather than either of the first two.
* **Nothing added here acts.** The survey neither prunes a pid file nor records
  a death nor stops a process. A render that could get a healthy poller reaped
  would be worse than the bug it fixes — see #511, where two live watchers were
  killed because a board invited a reasonable reading that they were duplicates.
"""
from __future__ import annotations

import importlib.util
import json
import os
import types
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


radar = _module("watch_radar_1183", WATCH_DIR / "radar.py")
dispatcher = radar.dispatcher
transport = radar.transport

ACCEPTED = {"state": "accepted", "ts": "2026-08-09T09:00:00Z"}
NO_LISTENER = {"state": "no-listener", "ts": "2026-08-09T09:00:00Z"}
UNSETTLED = {"state": "unknown", "ts": "2026-08-09T09:00:00Z"}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A state directory of this test's own, and a fleet that is all ours.

    `poller_census` is stubbed empty so no poller on the developer's machine can
    wander onto the board, and `_pid_alive` answers only for PIDs this file
    planted. It is the census and not `scan_poller_pids` since #1881: the board
    renders three buckets and that function is one of them, so the narrow stub
    left the other two reading the real process table.
    """
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == os.getpid())
    return tmp_path


def _watcher(root: Path, source: str, wid: str, last_emit=None, *, pid=None) -> None:
    """One live watcher: a pid file naming this process, and a state file."""
    if pid is None:
        pid = os.getpid()
    (root / f"supertool-watch-{source}__{wid}.pid").write_text(f"{pid}\n")
    state = {"last_event": {"event": "mr_updated", "ts": "2026-08-09T09:00:00Z"}}
    if last_emit is not None:
        state["last_emit"] = last_emit
    (root / f"supertool-watch-{source}__{wid}.state.json").write_text(
        json.dumps(state), encoding="utf-8")


def _row(out: str, wid: str) -> str:
    """The one rendered table row naming `wid`, asserted to be unique."""
    hits = [line for line in out.splitlines() if line.split()[1:2] == [wid]]
    assert len(hits) == 1, f"expected one row for {wid}, got {hits}"
    return hits[0]


# --------------------------------------------------------------------------
# the classifier
# --------------------------------------------------------------------------

def test_no_last_emit_is_its_own_answer_not_a_delivery() -> None:
    """A watcher that has never emitted has not failed to deliver."""
    assert transport.delivery_of(None) == transport.DELIVERY_NO_EMIT


def test_an_unreadable_state_file_outranks_whatever_it_seemed_to_say() -> None:
    assert transport.delivery_of(ACCEPTED, "it is a symlink") == transport.EMIT_UNKNOWN


def test_an_emit_state_this_build_does_not_know_is_unknown() -> None:
    """The failure mode to avoid is a future or forged value reading as fine."""
    assert transport.delivery_of({"state": "sort-of", "ts": "t"}) == transport.EMIT_UNKNOWN
    assert transport.delivery_of("not-a-dict") == transport.EMIT_UNKNOWN


def test_the_three_recorded_states_pass_through() -> None:
    assert transport.delivery_of(ACCEPTED) == transport.EMIT_ACCEPTED
    assert transport.delivery_of(NO_LISTENER) == transport.EMIT_NO_LISTENER
    assert transport.delivery_of(UNSETTLED) == transport.EMIT_UNKNOWN


# --------------------------------------------------------------------------
# watches
# --------------------------------------------------------------------------

def test_watches_names_the_watcher_whose_last_emit_found_nobody(fleet, capsys) -> None:
    _watcher(fleet, "gitlab-mr", "33311", NO_LISTENER)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "DELIVERY" in out
    assert "NO LISTENER" in _row(out, "33311")


def test_watches_tells_a_quiet_watcher_from_a_stranded_one(fleet, capsys) -> None:
    """The judgement call #1183 turns on. Both have an old record; only one is
    a delivery failure, and the difference is in the field, not in a clock."""
    _watcher(fleet, "gitlab-mr", "quiet", None)
    _watcher(fleet, "gitlab-mr", "stranded", NO_LISTENER)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "NO LISTENER" in _row(out, "stranded")
    assert "NO LISTENER" not in _row(out, "quiet")
    assert "no emit" in _row(out, "quiet")


def test_watches_says_unknown_rather_than_guessing_from_an_unread_file(
        fleet, monkeypatch, capsys) -> None:
    _watcher(fleet, "gitlab-mr", "hostile", NO_LISTENER)
    monkeypatch.setattr(transport, "read_state_checked",
                        lambda *a: (None, "it is a symlink and was not followed"))
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    row = _row(out, "hostile")
    assert "unknown" in row
    assert "NO LISTENER" not in row


def test_watches_says_accepted_when_a_listener_took_the_bytes(fleet, capsys) -> None:
    _watcher(fleet, "gitlab-mr", "fine", ACCEPTED)
    assert dispatcher.cmd_list() == 0
    assert "accepted" in _row(capsys.readouterr().out, "fine")


# --------------------------------------------------------------------------
# radar
# --------------------------------------------------------------------------

def _tiers(monkeypatch) -> None:
    """Radar refuses with no tiers configured; give it one that says nothing."""
    tier = types.SimpleNamespace(
        RADAR_OPTIONS=set(), RADAR_QUIET_DEFAULT=True,
        radar_report=lambda opts: ([], True),
        radar_state=lambda opts: [])
    monkeypatch.setenv(radar.TIERS_ENV, '{"fake": {}}')
    monkeypatch.setattr(radar, "_tier_module", lambda n: tier if n == "fake" else None)


def test_radar_board_cannot_render_a_stranded_fleet_as_a_quiet_one(
        fleet, monkeypatch, capsys) -> None:
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "33311", NO_LISTENER)
    _watcher(fleet, "gitlab-mr", "33312", NO_LISTENER)
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "nobody listening" in out
    assert "2 of 2" in out


def test_radar_state_carries_the_same_verdict(fleet, monkeypatch, capsys) -> None:
    """`radar:--state` is the read-only route (#859); it is also the one an
    operator reaches for when they suspect something, so it must answer too."""
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "33311", NO_LISTENER)
    assert radar.state_main("") == 0
    assert "nobody listening" in capsys.readouterr().out


def test_radar_does_not_call_a_never_emitting_fleet_delivering(
        fleet, monkeypatch, capsys) -> None:
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "quiet", None)
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "no watcher has recorded an emit" in out
    assert "nobody listening" not in out


def test_radar_reports_the_worst_state_not_the_average(
        fleet, monkeypatch, capsys) -> None:
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "ok", ACCEPTED)
    _watcher(fleet, "gitlab-mr", "bad", NO_LISTENER)
    assert radar.main(["radar"]) == 0
    assert "1 of 2" in capsys.readouterr().out


def test_radar_says_it_could_not_tell_rather_than_nothing(
        fleet, monkeypatch, capsys) -> None:
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "cannot", UNSETTLED)
    assert radar.main(["radar"]) == 0
    assert "cannot say" in capsys.readouterr().out


# --------------------------------------------------------------------------
# report-only — the trade this must not make
# --------------------------------------------------------------------------

def test_the_survey_neither_prunes_a_pid_file_nor_records_a_death(
        fleet, monkeypatch) -> None:
    """`delivery_survey` reads state files and nothing else.

    `list_active_pids` unlinks stale pid files and writes a death into the
    ledger on its way past. Both are correct there and both are actions, so
    routing radar's new header through it would have made *looking* mutate the
    fleet — the #859 property, undone by the fix for #1183.
    """
    _watcher(fleet, "gitlab-mr", "gone", NO_LISTENER, pid=9999999)
    pid_file = fleet / "supertool-watch-gitlab-mr__gone.pid"

    def _explode(*a, **k):  # pragma: no cover - the point is that it never runs
        raise AssertionError("the delivery survey must not record a death")

    monkeypatch.setattr(transport, "record_death", _explode)
    rows = transport.delivery_survey()

    assert rows == [("gitlab-mr", "gone", transport.EMIT_NO_LISTENER)]
    assert pid_file.exists()


def test_radar_state_still_stops_no_process(fleet, monkeypatch, capsys) -> None:
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "33311", NO_LISTENER)

    def _explode(pid):  # pragma: no cover - the point is that it never runs
        raise AssertionError(f"radar:--state stopped PID {pid}")

    monkeypatch.setattr(dispatcher, "_stop_pid", _explode)
    assert radar.state_main("") == 0
    assert "nobody listening" in capsys.readouterr().out


def test_radar_does_not_round_a_partly_silent_fleet_up_to_all_accepted(
        fleet, monkeypatch, capsys) -> None:
    """The aggregate has the same failure mode as the row it summarises.

    With nothing lost and nothing unsettled, a single accepted emit used to
    carry the whole fleet: `all 3 watcher state file(s) had their last emit
    accepted`, said over two watchers that had never emitted at all. That is
    the absence read as a clean result — the defect #1183 was filed about,
    reintroduced one level up, in the fix for it.
    """
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "spoke", ACCEPTED)
    _watcher(fleet, "gitlab-mr", "silent-a", None)
    _watcher(fleet, "gitlab-mr", "silent-b", None)
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "all 3" not in out
    assert "1 of 3" in out
    assert "2" in out and "not emitted" in out


def test_radar_says_all_accepted_only_when_every_file_says_so(
        fleet, monkeypatch, capsys) -> None:
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "a", ACCEPTED)
    _watcher(fleet, "gitlab-mr", "b", ACCEPTED)
    assert radar.main(["radar"]) == 0
    assert "all 2" in capsys.readouterr().out
