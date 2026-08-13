"""`channel:health` could not tell the socket-holder from the health-file writer (#1192).

Split out of #1187/#1184. The audit called the forgery acceptable because the
report says `self-reported` — but that word covers the **pid line** only, and
the forgeable thing is the whole `FORWARDING` verdict: a same-uid process binds
the socket, writes a health file naming its own live pid with a fresh stamp, and
the op prints `channel: FORWARDING`.

**Peer credentials do not close that hole, and this file does not claim they
do.** `SO_PEERCRED` / `LOCAL_PEERPID` yield the pid of the process holding the
socket. A same-uid attacker that binds the socket *and* writes the health file
is its own peer, so the two agree and the verdict stands. What the check buys is
the **mismatch**: a health file naming a process that is not the one holding the
socket — a forged or stale file left beside a legitimate consumer — which
previously read as `FORWARDING` with no objection at all. The documented ceiling
in `docs/presets/watch.md` stays regardless.

**Three states, never two.** They match; they contradict each other, which is a
live impersonation and is said loudly; or the credentials could not be obtained
here, which is named rather than folded into either neighbour. The third is not
hypothetical — Windows has no equivalent, and neither does FreeBSD, whose
`LOCAL_PEERCRED` returns a `struct xucred` with a uid and no pid.

**A contradiction is not `CANNOT DETERMINE`.** It is the opposite: something was
determined. Folding it into the existing unknown would be this repo's own defect
— a finding rendered as an absence of findings — so it has its own verdict line
and its own exit code.

**Platforms.** The tests that need a real peer pid are gated on
`channel.peer_credentials_supported()`, which is measured rather than assumed.
The unavailable-credentials arm is exercised everywhere, by stubbing the getter,
because that arm is the one no CI platform of ours reaches naturally.
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
    """macOS caps AF_UNIX paths near 104 bytes and pytest's `tmp_path` is long,
    so the socket goes in the system temp dir. Measured the hard way: with the
    socket under `tmp_path`, every test here skipped with "this platform cannot
    bind an AF_UNIX socket" on the one platform that certainly can — a skip that
    reads exactly like a platform limit and was a path length.

    `gettempdir()` rather than a literal `/tmp`, because this module runs on
    every leg of the matrix and `/tmp` on Windows is a drive-root path that need
    not exist.
    """
    return str(Path(tempfile.gettempdir())
               / f"st1192-{os.getpid()}-{time.time_ns()}.sock")


def _can_bind_af_unix() -> bool:
    """Measured once, not guessed from `os.name`: `hasattr(socket, "AF_UNIX")`
    is True on Windows builds of CPython and the bind may still fail."""
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
        for leftover in (path, f"{path}.health.json"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _write_health(path: str, pid: int) -> None:
    """A health file that clears every objection `_health_objection` makes, so
    the only thing left for the verdict to turn on is who holds the socket."""
    Path(f"{path}.health.json").write_text(json.dumps({
        "pid": pid,
        "started": "2026-08-09T10:00:00Z",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lines_read": 12,
        "forwarded": 11,
        "dropped": 1,
        "last_forwarded": "2026-08-09T10:00:05Z",
    }), encoding="utf-8")


#: Alive, and not the process holding the socket in these tests. A literal
#: constant would be a coin flip: any pid may belong to somebody by the time
#: this runs, and `_health_objection` refuses a dead one before the verdict is
#: ever reached.
def _other_live_pid() -> int:
    parent = os.getppid()
    assert parent and parent != os.getpid()
    return parent


needs_peer_creds = pytest.mark.skipif(
    not channel.peer_credentials_supported(),
    reason="this platform exposes no peer pid for an AF_UNIX socket",
)


# --- the primitive ---------------------------------------------------------

@needs_peer_creds
def test_the_peer_pid_of_a_socket_we_hold_ourselves_is_our_own(sock):
    """The listener in the fixture is this process, so this is the one case
    where the right answer is known independently of the op."""
    pid, why = channel.peer_pid(sock)
    assert pid == os.getpid(), (pid, why)
    assert why == ""


def test_no_socket_is_a_named_refusal_and_never_a_pid():
    pid, why = channel.peer_pid(_sock_path())
    assert pid is None
    assert why, "a refusal that says nothing is the state this op exists to remove"


# --- the three states of the verdict ---------------------------------------

@needs_peer_creds
def test_a_health_file_naming_another_process_contradicts_the_socket_holder(sock):
    """The whole issue. Before this, the op printed FORWARDING with no
    objection: nothing compared the two pids, so nothing could disagree."""
    _write_health(sock, _other_live_pid())
    rc, report = channel.health(sock)
    assert report.splitlines()[0] == "channel: CONTRADICTED", report
    assert rc == channel.RC_CONTRADICTED


@needs_peer_creds
def test_a_contradiction_is_not_the_cannot_determine_bucket(sock):
    """`CANNOT DETERMINE` means nothing was established. Here something was:
    the file was written by a process that is not holding this socket."""
    _write_health(sock, _other_live_pid())
    rc, _ = channel.health(sock)
    assert rc not in (channel.RC_UNKNOWN, channel.RC_FORWARDING, channel.RC_NOT_DELIVERING)


@needs_peer_creds
def test_a_contradiction_names_both_pids_so_it_can_be_acted_on(sock):
    _write_health(sock, _other_live_pid())
    _, report = channel.health(sock)
    assert str(_other_live_pid()) in report, report
    assert str(os.getpid()) in report, report


@pytest.fixture(autouse=True)
def _subscribed(monkeypatch):
    """Pin the #1543 subscription probe to `subscribed`.

    That probe reads the process that spawned the socket-holder, which here is
    whatever launched pytest, and answers `CANNOT DETERMINE` for anything it
    does not recognise as a session. This file is about peer identity, so the
    verdict it asserts must not move with the machine the suite runs on. The
    probe's own three states are pinned in
    `tests/test_watch_channel_subscription_1543.py`.
    """
    monkeypatch.setattr(
        channel, "subscription",
        lambda *args, **kwargs: channel.Subscription(channel.SUB_SUBSCRIBED, []))


@needs_peer_creds
def test_a_health_file_naming_the_socket_holder_still_forwards(sock):
    """The check must not cost the healthy answer."""
    _write_health(sock, os.getpid())
    rc, report = channel.health(sock)
    assert report.splitlines()[0] == "channel: FORWARDING", report
    assert rc == channel.RC_FORWARDING


@needs_peer_creds
def test_a_matching_verdict_says_the_holder_was_checked_rather_than_assumed(sock):
    """A verified match that renders identically to an unchecked one teaches a
    reader that FORWARDING always meant this, which it did not."""
    _write_health(sock, os.getpid())
    _, report = channel.health(sock)
    assert "socket-holder" in report, report


def test_credentials_that_could_not_be_obtained_are_named_not_folded(sock, monkeypatch):
    """The Windows arm, and the reason the disclosure stays. Neither a match
    nor a contradiction was established, and the report must not read like
    either."""
    monkeypatch.setattr(
        channel, "peer_pid",
        lambda _path: (None, "peer credentials are not available on plan9"))
    _write_health(sock, _other_live_pid())
    rc, report = channel.health(sock)
    assert report.splitlines()[0] == "channel: FORWARDING", report
    assert rc == channel.RC_FORWARDING
    assert "plan9" in report, report


def test_the_forgeable_ceiling_is_still_disclosed(sock, monkeypatch):
    """Narrowing the hole is not closing it: a same-uid process that binds the
    socket *and* writes the health file is its own peer, so the two agree."""
    monkeypatch.setattr(channel, "peer_pid", lambda _path: (os.getpid(), ""))
    _write_health(sock, os.getpid())
    _, report = channel.health(sock)
    assert channel.CEILING in report


# --- documentation ---------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(1192)
