"""SUPERTOOL_WATCH_NO_DESKTOP silences the macOS ping without cutting the wire (#2170).

`only=event1,event2` filters what a poller emits at all -- socket included --
so it cannot silence only the desktop transport while keeping the wire events
flowing to the `claude-channel` MCP server. This is the dedicated opt-out:
read once in `desktop_notify`, before the `shutil.which` probe, and disclosed
on `watches`/`radar` through `channel_disclosure()` so a silent desktop is a
stated configuration rather than an unexplained absence -- the issue's own
words for this repo's defect class.

Positive control lives beside every negative one: opting out must not touch
the socket emit, and leaving it unset must not touch the opt-out disclosure.
"""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile

import pytest

import presets.watch.transport as transport
from _changelog_findable import assert_change_is_findable


def _short_sock_path():
    """A macOS AF_UNIX path has a ~104-byte limit; pytest's own `tmp_path`
    fixture is routinely longer than that once nested under a worker's own
    directory, and `bind()` raises rather than truncating (measured here)."""
    fd, path = tempfile.mkstemp(prefix="st2170-", suffix=".sock")
    os.close(fd)
    os.unlink(path)
    return path


def _can_bind_af_unix() -> bool:
    """Same probe as `test_watch_channel_probe_1593.py`, for the same reason:
    `hasattr(socket, "AF_UNIX")` is True on Windows builds of CPython, and
    whether a bind then succeeds depends on the OS build -- an `os.name`
    branch here would pass vacuously on the leg least like the author's own
    machine, which is worse than skipping."""
    if not hasattr(socket, "AF_UNIX"):
        return False
    path = _short_sock_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(path)
        return True
    except OSError:
        return False
    finally:
        s.close()
        try:
            os.unlink(path)
        except OSError:
            pass


needs_socket = pytest.mark.skipif(
    not _can_bind_af_unix(), reason="this platform cannot bind an AF_UNIX socket")


def _osascript_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(transport.sys, "platform", "darwin")
    monkeypatch.setattr(transport.shutil, "which", lambda _n: "/usr/bin/osascript")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    return calls


# --- the predicate -----------------------------------------------------------

def test_unset_is_not_opted_out(monkeypatch):
    monkeypatch.delenv(transport.NO_DESKTOP_ENV, raising=False)
    assert transport.desktop_notify_disabled() is False


def test_hand_exported_one_opts_out(monkeypatch):
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "1")
    assert transport.desktop_notify_disabled() is True


def test_config_exported_lowercase_true_opts_out(monkeypatch):
    """The generic op-config-to-env export stringifies a JSON bool with
    `json.dumps`, so a `.supertool.json` `watch_no_desktop: true` (under
    `ops.watch`/`ops.radar`) would arrive here as the string "true", not
    "True" and not "1"."""
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "true")
    assert transport.desktop_notify_disabled() is True


def test_on_opts_out_too(monkeypatch):
    """Matches `presets/git/diff._plain()`'s reading of SUPERTOOL_PLAIN, the
    closest existing boolean env knob -- an operator reaching for "on" by
    analogy with that one must not find this knob silently inert."""
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "on")
    assert transport.desktop_notify_disabled() is True


def test_false_and_zero_are_not_opted_out(monkeypatch):
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "false")
    assert transport.desktop_notify_disabled() is False
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "0")
    assert transport.desktop_notify_disabled() is False


# --- desktop_notify itself: must-not-fire, paired with must-fire ------------

def test_opted_out_desktop_notify_does_not_shell_out(monkeypatch):
    calls = _osascript_calls(monkeypatch)
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "1")
    transport.desktop_notify("t", "m")
    assert calls == [], "opted out, so osascript must not run"


def test_not_opted_out_desktop_notify_still_shells_out(monkeypatch):
    """The positive control for the test above: on the same darwin/osascript
    fixture, with the knob unset, the ping still fires. Without this, the
    silence test could be passing because nothing here fires at all."""
    calls = _osascript_calls(monkeypatch)
    monkeypatch.delenv(transport.NO_DESKTOP_ENV, raising=False)
    transport.desktop_notify("t", "m")
    assert len(calls) == 1, "expected exactly one osascript invocation"


# --- emit_event: the wire is unaffected by the opt-out ----------------------

@needs_socket
def test_opted_out_still_emits_to_the_socket(monkeypatch, tmp_path):
    """The whole point of #2170: silencing the ping must not silence the wire.

    Uses the real `desktop_notify` (only `osascript` is stubbed, as in the
    tests above) rather than replacing `desktop_notify` itself — stubbing it
    would remove the very opt-out check this test exists to exercise, and
    would pass whether or not `emit_event` still routed through it."""
    calls = _osascript_calls(monkeypatch)
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "1")
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    sock_path = _short_sock_path()
    monkeypatch.setattr(transport, "SOCK_PATH", sock_path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    try:
        transport.emit_event(
            "gh-pr", "1", "checks_failed", {"x": 1},
            notify_title="title", notify_message="message",
        )
        conn, _ = srv.accept()
        data = conn.recv(4096)
    finally:
        srv.close()
        os.unlink(sock_path)

    assert data, "the socket must still receive the event when desktop is opted out"
    assert calls == [], "osascript must not have run"


@needs_socket
def test_not_opted_out_notifies_and_still_emits(monkeypatch, tmp_path):
    """Positive control for the test above: unset, both transports fire."""
    calls = _osascript_calls(monkeypatch)
    monkeypatch.delenv(transport.NO_DESKTOP_ENV, raising=False)
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    sock_path = _short_sock_path()
    monkeypatch.setattr(transport, "SOCK_PATH", sock_path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    try:
        transport.emit_event(
            "gh-pr", "1", "checks_failed", {"x": 1},
            notify_title="title", notify_message="message",
        )
        conn, _ = srv.accept()
        data = conn.recv(4096)
    finally:
        srv.close()
        os.unlink(sock_path)

    assert data, "the socket must receive the event"
    assert len(calls) == 1, "osascript must have run when not opted out"


# --- the disclosure: watches/radar must state the opt-out, not go quiet -----

def test_channel_disclosure_states_the_opt_out(monkeypatch):
    monkeypatch.setenv(transport.NO_DESKTOP_ENV, "1")
    blob = "\n".join(transport.channel_disclosure())
    assert transport.NO_DESKTOP_ENV in blob, blob
    assert "desktop" in blob.lower(), blob


def test_channel_disclosure_says_nothing_extra_when_not_opted_out(monkeypatch):
    monkeypatch.delenv(transport.NO_DESKTOP_ENV, raising=False)
    blob = "\n".join(transport.channel_disclosure())
    assert transport.NO_DESKTOP_ENV not in blob, blob


def test_the_change_is_findable() -> None:
    assert_change_is_findable(2170)
