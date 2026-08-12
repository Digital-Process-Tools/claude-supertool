"""`channel:health` never asked who held the socket in its two early arms (#1476).

The mechanism is one call site that did not adopt a mechanism shipped in its own
file. `peer_pid()` and `peer_credentials_supported()` landed in
`presets/watch/channel.py` with #1192 (PR #1208), and `health()` called them at
exactly one place — after `read_health()` had already returned a record. The two
arms that return before it, `record is None` and `_health_objection`, printed
`CANNOT DETERMINE` without ever asking a question the platform could answer.

That is the arm that fires in the case this was filed from: the consumer is a
Claude session launched with the channel flag, it publishes no health file, and
the report said nothing about a holder it could have named.

**Two states were collapsed into one.** `nothing is consuming this socket` and
`another session is consuming it` call for opposite actions — launch a consumer,
or accept that delivery works and this session is simply not the listener — and
both printed the same two lines.

**The verdict does not move.** Naming the holder does not establish delivery, so
these arms stay `CANNOT DETERMINE` / `RC_UNKNOWN` and the ceiling stays printed.
What changes is that the reader is told who has the socket, or which probe was
tried and what it returned. A bare `CANNOT DETERMINE` is the thing being fixed.

**Platforms.** Anything needing a real peer pid is gated on
`peer_credentials_supported()`, measured rather than assumed (FreeBSD and Windows
both land in the unable arm). The unable arm itself is exercised everywhere by
stubbing `peer_pid`, because no CI platform of ours reaches it naturally.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402


def _sock_path() -> str:
    """System temp dir, not `tmp_path`: macOS caps an AF_UNIX path near 104
    bytes and pytest's is long enough to turn every test here into a skip that
    reads exactly like a platform limit (#1192 paid for this one)."""
    return str(Path(tempfile.gettempdir())
               / f"st1476-{os.getpid()}-{time.time_ns()}.sock")


def _can_bind_af_unix() -> bool:
    if not hasattr(socket, "AF_UNIX"):
        return False
    probe = _sock_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(probe)
        return True
    except OSError:
        return False
    finally:
        s.close()
        try:
            os.unlink(probe)
        except OSError:
            pass


@pytest.fixture()
def sock():
    """A live listener held by this process, and the path it is bound to."""
    if not _can_bind_af_unix():
        pytest.skip("this platform cannot bind an AF_UNIX socket")
    path = _sock_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    try:
        yield path
    finally:
        srv.close()
        for leftover in (path, f"{path}{channel.HEALTH_SUFFIX}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _other_live_pid() -> int:
    """Alive, and not this process. A literal would be a coin flip, and
    `_health_objection` refuses a dead pid before the verdict is reached."""
    parent = os.getppid()
    assert parent and parent != os.getpid()
    return parent


def _write_stale_health(path: str, pid: int) -> None:
    """Clears every objection except staleness, so the report lands in the
    `_health_objection` arm rather than the `record is None` one."""
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - (channel.STALE_AFTER_SECS + 120)))
    Path(f"{path}{channel.HEALTH_SUFFIX}").write_text(json.dumps({
        "pid": pid,
        "started": "2026-08-12T09:00:00Z",
        "updated": old,
        "lines_read": 3,
        "forwarded": 3,
        "dropped": 0,
    }), encoding="utf-8")


needs_peer_creds = pytest.mark.skipif(
    not channel.peer_credentials_supported(),
    reason="this platform exposes no peer pid for an AF_UNIX socket",
)


# --- the arm this was filed from: bound, no counters ------------------------

def test_a_bound_consumer_with_no_health_file_names_the_socket_holder(sock, monkeypatch):
    """The reported case. Another session holds the socket, publishes no health
    file, and the old report said only `bound, but ... no counters`."""
    other = _other_live_pid()
    monkeypatch.setattr(channel, "peer_pid", lambda _path: (other, ""))
    _, report = channel.health(sock)
    assert "socket-holder" in report, report
    assert f"pid {other}" in report, report


def test_a_holder_that_is_not_this_process_is_said_in_those_words(sock, monkeypatch):
    """`not my listener` is the whole decision the reader is making, so the
    report has to make it for them rather than print a pid and stop."""
    other = _other_live_pid()
    monkeypatch.setattr(channel, "peer_pid", lambda _path: (other, ""))
    _, report = channel.health(sock)
    assert "not this process" in report, report
    assert str(os.getpid()) in report, report


@needs_peer_creds
def test_a_holder_that_is_this_process_is_said_too(sock):
    """The fixture's listener is this process, so this is the one case where
    the right answer is known independently of the op."""
    _, report = channel.health(sock)
    assert "socket-holder" in report, report
    assert f"pid {os.getpid()}" in report, report
    assert "this process" in report, report
    assert "not this process" not in report, report


def test_an_unresolvable_holder_names_the_probe_and_what_it_returned(sock, monkeypatch):
    """The third state. `cannot resolve the holder` must carry the refusal it
    got, never collapse into the two lines that were there before."""
    monkeypatch.setattr(
        channel, "peer_pid",
        lambda _path: (None, "peer credentials are not available on plan9"))
    _, report = channel.health(sock)
    assert "plan9" in report, report


# --- the second early arm: counters present, objected to --------------------

def test_the_objected_counters_arm_names_the_holder_too(sock, monkeypatch):
    """`_health_objection` returns before the peer check as well, and a stale
    health file beside a live socket is the same two-states-in-one."""
    other = _other_live_pid()
    _write_stale_health(sock, other)
    monkeypatch.setattr(channel, "peer_pid", lambda _path: (other, ""))
    rc, report = channel.health(sock)
    assert "not refreshed" in report or "wedged" in report, report
    # `pid {other}` alone would pass on the objection sentence, which names the
    # health file's writer and never asked who holds the socket. The holder
    # vocabulary is what distinguishes the two claims.
    assert "socket-holder" in report, report
    assert "not this process" in report, report


def test_the_objected_counters_arm_names_an_unresolvable_holder_too(sock, monkeypatch):
    _write_stale_health(sock, _other_live_pid())
    monkeypatch.setattr(
        channel, "peer_pid",
        lambda _path: (None, "peer credentials are not available on plan9"))
    _, report = channel.health(sock)
    assert "plan9" in report, report


# --- what must not move -----------------------------------------------------

def test_naming_the_holder_does_not_promote_the_verdict(sock, monkeypatch):
    """Knowing who holds a socket is not knowing that events arrive. Both arms
    stay `CANNOT DETERMINE`."""
    monkeypatch.setattr(channel, "peer_pid", lambda _path: (_other_live_pid(), ""))
    rc_no_file, report_no_file = channel.health(sock)
    assert rc_no_file == channel.RC_UNKNOWN
    assert report_no_file.splitlines()[0] == "channel: CANNOT DETERMINE", report_no_file
    _write_stale_health(sock, _other_live_pid())
    rc_objected, report_objected = channel.health(sock)
    assert rc_objected == channel.RC_UNKNOWN
    assert report_objected.splitlines()[0] == "channel: CANNOT DETERMINE", report_objected


def test_the_ceiling_is_still_the_last_thing_both_arms_say(sock, monkeypatch):
    monkeypatch.setattr(channel, "peer_pid", lambda _path: (_other_live_pid(), ""))
    _, report = channel.health(sock)
    assert channel.CEILING in report
    _write_stale_health(sock, _other_live_pid())
    _, report = channel.health(sock)
    assert channel.CEILING in report


def test_the_socket_could_not_be_probed_arm_claims_no_holder(monkeypatch):
    """`state == "unknown"` means the connect itself failed. `peer_pid` connects
    too, so there is nothing there to ask and this arm must not pretend to."""
    monkeypatch.setattr(
        channel, "probe_socket",
        lambda _path: ("unknown", "OSError connecting to /nope"))
    rc, report = channel.health("/nope")
    assert rc == channel.RC_UNKNOWN
    assert "holder" not in report, report


# --- documentation ----------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(1476)
