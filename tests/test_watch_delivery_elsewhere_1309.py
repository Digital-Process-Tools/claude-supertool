"""`accepted` is not `accepted by the socket this session reads` (issue #1309).

#1309 asked whether two sessions can both be woken by the radar. Two of its
three layers do not hold — `claude-channel` refuses to steal a live socket and
names the override (#550, pinned end to end in
`tests/test_notifiers_claude_channel_550.py`), and the poller slot is namespaced
by `SUPERTOOL_WATCH_STATE_DIR`, not by the machine — so a second session already
gets a radar of its own by exporting both variables.

What does not hold is the render. A session that exports only
`SUPERTOOL_WATCH_SOCK` shares `/tmp` with the first, so its slots are held by
pollers that captured the *other* socket at spawn. Those pollers are alive,
their emits are accepted, and `radar` said `all N watcher state file(s) had
their last emit accepted by a listener` — true, and read in the second session
as a statement about a socket nothing had ever written to. The datum that
settles it (`sock_path`, written beside `last_emit` since #581) had no reader.

Three states here as everywhere in this preset: the recorded path is this
session's, it is somebody else's, or the watcher emitted and recorded no path
at all — which is not agreement and must not render as any.
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


radar = _module("watch_radar_1309", WATCH_DIR / "radar.py")
transport = radar.transport

ACCEPTED = {"state": "accepted", "ts": "2026-08-11T09:00:00Z"}

#: Two socket paths that differ in the field, never in the separator: these are
#: compared as opaque strings by the product, and a test that built them with a
#: hardcoded "/" would assert POSIX rather than the comparison (#1004).
MINE = str(Path("sock-mine") / "w.sock")
THEIRS = str(Path("sock-theirs") / "w.sock")


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A state directory of this test's own, and a socket path of its own."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", MINE)
    monkeypatch.setattr(transport, "scan_poller_pids", lambda: ({}, True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == os.getpid())
    return tmp_path


def _watcher(root: Path, source: str, wid: str, last_emit=None, sock=None) -> None:
    """One watcher's pair of files. `sock` None means the key is absent."""
    (root / f"supertool-watch-{source}__{wid}.pid").write_text(f"{os.getpid()}\n")
    state = {"last_event": {"event": "mr_updated", "ts": "2026-08-11T09:00:00Z"}}
    if last_emit is not None:
        state["last_emit"] = last_emit
    if sock is not None:
        state["sock_path"] = sock
    (root / f"supertool-watch-{source}__{wid}.state.json").write_text(
        json.dumps(state), encoding="utf-8")


def _tiers(monkeypatch) -> None:
    """Radar refuses with no tiers configured; give it one that says nothing."""
    tier = types.SimpleNamespace(
        RADAR_OPTIONS=set(), RADAR_QUIET_DEFAULT=True,
        radar_report=lambda opts: ([], True),
        radar_state=lambda opts: [])
    monkeypatch.setenv(radar.TIERS_ENV, '{"fake": {}}')
    monkeypatch.setattr(radar, "_tier_module", lambda n: tier if n == "fake" else None)


# --------------------------------------------------------------------------
# the survey
# --------------------------------------------------------------------------

def test_the_survey_reports_the_socket_each_watcher_last_wrote_to(fleet) -> None:
    _watcher(fleet, "gitlab-mr", "elsewhere", ACCEPTED, sock=THEIRS)
    assert transport.emit_destinations() == [("gitlab-mr", "elsewhere", THEIRS)]


def test_a_watcher_that_recorded_no_socket_reports_an_empty_string(fleet) -> None:
    """The absence, kept distinct from a path — including from this one's."""
    _watcher(fleet, "gitlab-mr", "old-build", ACCEPTED, sock=None)
    assert transport.emit_destinations() == [("gitlab-mr", "old-build", "")]


def test_the_survey_neither_prunes_nor_records_a_death(fleet, monkeypatch) -> None:
    """Same guarantee `delivery_survey` carries: looking must not act (#859)."""
    def _explode(*a, **k):  # pragma: no cover - the point is that it never runs
        raise AssertionError("the destination survey must not record a death")

    monkeypatch.setattr(transport, "record_death", _explode)
    _watcher(fleet, "gitlab-mr", "gone", ACCEPTED, sock=THEIRS)
    pid_file = fleet / "supertool-watch-gitlab-mr__gone.pid"
    assert transport.emit_destinations() == [("gitlab-mr", "gone", THEIRS)]
    assert pid_file.exists()


# --------------------------------------------------------------------------
# the render
# --------------------------------------------------------------------------

def test_radar_names_a_fleet_delivering_to_another_sessions_socket(
        fleet, monkeypatch, capsys) -> None:
    """The defect: `all N accepted`, said to the session receiving none of it."""
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "a", ACCEPTED, sock=THEIRS)
    _watcher(fleet, "gitlab-mr", "b", ACCEPTED, sock=THEIRS)
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "2 of 2" in out
    assert "this session does not read" in out
    assert THEIRS in out
    assert MINE in out


def test_radar_says_nothing_extra_when_every_watcher_writes_here(
        fleet, monkeypatch, capsys) -> None:
    """Agreement is not news, and a line per healthy fleet is noise."""
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "a", ACCEPTED, sock=MINE)
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "all 1" in out
    assert "this session does not read" not in out
    assert "do not record" not in out


def test_an_emitter_with_no_recorded_socket_is_its_own_answer(
        fleet, monkeypatch, capsys) -> None:
    """Not this session's socket, not another's — unsettled, and said so."""
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "old-build", ACCEPTED, sock=None)
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "do not record which socket" in out
    assert "this session does not read" not in out


def test_a_fleet_that_never_emitted_gets_no_destination_line(
        fleet, monkeypatch, capsys) -> None:
    """A watcher with nothing to report has no destination to disagree about.

    Without this, every quiet fleet on earth would grow a second header saying
    its sockets were unrecorded — which is true, uninformative, and exactly the
    kind of line that trains a reader to skip the block (#1183's footnote).
    """
    _tiers(monkeypatch)
    _watcher(fleet, "gitlab-mr", "quiet", None, sock=None)
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "no watcher has recorded an emit yet" in out
    assert "do not record which socket" not in out
    assert "this session does not read" not in out
