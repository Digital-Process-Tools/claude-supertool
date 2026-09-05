"""Two channel servers configured for one session collide by construction, and
every diagnostic reads healthy while the channel is dead (#2133).

Split out of #2051 and measured first-hand, 2026-09-01: `.mcp.json` declares
`claude-channel`, the launcher separately tags `server:oss-channel`, both
resolve the same socket, one binds and the other refuses (#550, correctly),
the harness reports **both** as CONNECTION_CLOSED, and the bound process keeps
forwarding into a transport nothing is connected to. `channel:health` read
`FORWARDING`, `session : subscribed`. Zero events arrived.

Two remedies, both in scope here (a third -- dropping one of the two server
declarations -- is a decision about this repository's own conventions and is
explicitly out of scope):

1. **The refusal is readable.** The losing process already detects the
   collision exactly (`channel.ts`'s `refuse()`); its message used to go only
   to stderr, which nobody past `CONNECTION_CLOSED` ever reads. It now also
   writes a small JSON marker beside the socket it lost, and the *winning*
   consumer clears any such marker the instant it (re)binds -- so what remains
   is evidence from *this run*, not a stale leftover from days ago.

2. **`channel:health`'s positive verdict stops overclaiming when that
   evidence exists.** `subscription()`'s `SUB_SUBSCRIBED` answer was, and
   remains, a claim about *configuration* only -- `claude mcp get` cannot
   establish *connection*, and #1558 already proved why not: probing it
   spawns a second process, and a live singleton legitimately fails that
   second connect every time, so "Failed to connect" from the lookup itself is
   not a usable signal (that finding stands; this change does not touch
   `_configured`). What newly changes the verdict is the *marker from remedy
   1* -- session-scoped, first-hand evidence, from the same run, that a rival
   was refused this exact socket. When that evidence exists, `subscription()`
   lands in the third state rather than the positive one, exactly as the
   issue asks: "the answer is not to fake it ... it is to stop saying
   subscribed ... and land in the third state."
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
import _symlink  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

needs_nofollow = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="this platform has no O_NOFOLLOW, so the guard cannot be enforced",
)

CONSUMER_ARGV = "bun /Users/x/notifiers/claude-channel/channel.ts"
SESSION_PID = 4242
TAGGED = ("claude /oss:tick "
          "--dangerously-load-development-channels server:oss-channel")

REFUSAL_SUFFIX = ".refused.json"


def _sock_path() -> str:
    """System temp dir, not `tmp_path`: macOS caps an AF_UNIX path near 104
    bytes and pytest's is long enough to turn every test here into a skip."""
    return str(Path(tempfile.gettempdir())
               / f"st2133-{os.getpid()}-{time.time_ns()}.sock")


def _can_bind_af_unix() -> bool:
    if not hasattr(socket, "AF_UNIX"):
        return False
    probe = _sock_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(probe)
        return True
    except OSError:
        return False
    finally:
        sock.close()
        try:
            os.unlink(probe)
        except OSError:
            pass


@pytest.fixture()
def forwarding(monkeypatch):
    """The exact state #2133 was filed from, minus the subscription question:
    a live socket held by this process, publishing fresh counters."""
    if not _can_bind_af_unix():
        pytest.skip("this platform cannot bind an AF_UNIX socket")
    path = _sock_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(f"{path}{channel.HEALTH_SUFFIX}").write_text(json.dumps({
        "pid": os.getpid(),
        "started": "2026-09-01T05:26:44Z",
        "updated": now,
        "last_forwarded": now,
        "lines_read": 19,
        "forwarded": 19,
        "dropped": 0,
    }), encoding="utf-8")
    monkeypatch.setattr(channel, "peer_pid", lambda _p: (os.getpid(), ""))
    try:
        yield path
    finally:
        srv.close()
        for leftover in (path, f"{path}{channel.HEALTH_SUFFIX}",
                          f"{path}{REFUSAL_SUFFIX}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _process_table(monkeypatch, session_argv: str, *, session_pid: int = SESSION_PID):
    table = {
        os.getpid(): (session_pid, CONSUMER_ARGV, ""),
        session_pid: (1, session_argv, ""),
    }

    def fields(pid: int):
        return table.get(pid, (None, "", f"no process {pid}"))

    monkeypatch.setattr(channel, "_ps_fields", fields)


def _configured(monkeypatch, answer, why: str = ""):
    monkeypatch.setattr(channel, "_configured",
                         lambda _name, _budget=None: (answer, why))


def _write_refusal(path: str, *, pid: int = 9999,
                    reason: str = "another claude-channel server is listening there",
                    ts: str = "2026-09-01T05:26:00Z") -> None:
    Path(f"{path}{REFUSAL_SUFFIX}").write_text(json.dumps({
        "pid": pid, "ts": ts, "reason": reason, "sock_path": path,
    }), encoding="utf-8")


# --- read_refusal: the reader ------------------------------------------------

def test_no_marker_is_a_named_absence(forwarding):
    record, why = channel.read_refusal(forwarding)
    assert record is None, record
    assert why, "an absent marker must say why it is absent, not just return None"


def test_a_written_marker_is_read_back(forwarding):
    _write_refusal(forwarding, pid=9999,
                    reason="another claude-channel server is listening there")
    record, why = channel.read_refusal(forwarding)
    assert record is not None, why
    assert record["pid"] == 9999, record
    assert "listening" in record["reason"], record


@needs_nofollow
def test_an_unreadable_marker_is_not_the_same_absence_as_no_marker(forwarding):
    """An absent marker and an unreadable one both return `(None, ...)` from
    `read_refusal` -- but they must not say the same thing. A same-uid symlink
    at the predictable `.refused.json` name is exactly the attack #1184/#1187
    already guard the health file against; folding "refused, but I could not
    read it" into "nobody refused" would let that attack hide the one piece of
    evidence this whole file exists to surface."""
    _symlink.require_symlink()
    os.symlink(str(Path(forwarding).parent / "nowhere.json"),
               f"{forwarding}{REFUSAL_SUFFIX}")
    record, why = channel.read_refusal(forwarding)
    assert record is None, record
    assert "symlink" in why, why
    assert why != "no rival consumer has recorded losing this socket", why


# --- remedy 1: the collision is readable from `channel:health` -------------

def test_health_surfaces_a_refusal_recorded_during_this_run(forwarding, monkeypatch):
    """The refusal #2133 measured going only to a process nobody reads is now
    also on the one surface an operator actually opens."""
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, True)
    _write_refusal(forwarding, pid=9999,
                    reason="another claude-channel server is listening there")
    _, report = channel.health(forwarding)
    # A "must fire" positive control (#2301): a real refusal was recorded, so
    # its render prefix must be present. `"refused  :"` is `_refusal_lines`'s
    # own label, unique to it -- see the sibling negative control below for
    # why the label rather than the bare digits is what is checked, on
    # either side of this pairing.
    assert "refused  :" in report, report
    assert "pid 9999" in report, report
    assert "listening there" in report, report


def test_health_says_nothing_about_a_refusal_when_none_was_recorded(
        forwarding, monkeypatch):
    """The positive control for the assertion above: a clean run must not grow
    a `refused` line out of nothing.

    Not a bare "9999" substring (#2301): `forwarding`'s own socket path is
    built from a nanosecond timestamp (`_sock_path`) and can coincidentally
    contain that same four-digit run -- observed for real on CI,
    `.../8575413370999984.sock`. A first fix narrowed this to "pid 9999",
    but that is not collision-proof either: `_holder_lines` also renders
    `f"pid {holder}"` for this *process's own* real OS pid whenever it holds
    the socket (`forwarding` monkeypatches `peer_pid` to return
    `os.getpid()`), so a worker whose real pid happened to be 9999 -- or to
    contain that run -- would trip the same false positive with no refusal
    ever written. The only string `_refusal_lines` renders that a marker-free
    report can never contain, independent of any digit, is the `"refused  :"`
    label prefix itself.
    """
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, True)
    _, report = channel.health(forwarding)
    assert "refused  :" not in report, report
    assert "listening there" not in report, report


def test_health_says_nothing_about_a_refusal_when_the_socket_path_coincidentally_contains_9999(
        monkeypatch):
    """Regression for #2301, forcing the sentinel digits into the socket path
    deterministically rather than waiting on `time.time_ns()` to reproduce it:
    a path that always contains "9999", with no refusal ever recorded. A bare
    `"9999" not in report` control would have failed here for a reason that
    has nothing to do with any refusal marker.

    `time.time_ns()` still supplies the per-run uniqueness `_sock_path()`
    relies on -- a fixed literal name would leave a leftover from a crashed
    prior run at this exact path on the next run with the same reused pid
    (common in CI containers), turning that stale collision into an
    unrelated `EADDRINUSE`. "9999" replaces digits of the timestamp rather
    than appending to it, so this path is no longer than `_sock_path()`'s own
    (see its docstring: macOS caps an AF_UNIX path near 104 bytes). `bind()`
    is still wrapped below rather than assumed to succeed, because nothing
    about this shape can prove it fits under `sun_path` on every platform in
    the CI matrix ahead of time.
    """
    if not _can_bind_af_unix():
        pytest.skip("this platform cannot bind an AF_UNIX socket")
    path = str(Path(tempfile.gettempdir())
               / f"st2133-{os.getpid()}-9999{time.time_ns() % 10 ** 12}.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
    except OSError as exc:
        pytest.skip(f"could not bind the forced-collision socket path: {exc}")
    srv.listen(8)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(f"{path}{channel.HEALTH_SUFFIX}").write_text(json.dumps({
        "pid": os.getpid(),
        "started": "2026-09-01T05:26:44Z",
        "updated": now,
        "last_forwarded": now,
        "lines_read": 19,
        "forwarded": 19,
        "dropped": 0,
    }), encoding="utf-8")
    monkeypatch.setattr(channel, "peer_pid", lambda _p: (os.getpid(), ""))
    try:
        _process_table(monkeypatch, TAGGED)
        _configured(monkeypatch, True)
        _, report = channel.health(path)
        assert "9999" in report, "the coincidence must actually land in this fixture, or it proves nothing"
        # Not "pid 9999" either (#2301 review): `_holder_lines` renders this
        # process's own real OS pid with a "pid " prefix too, whenever it
        # holds the socket, which `forwarding`-style fixtures always arrange.
        # "refused  :" is `_refusal_lines`'s own label and the only string it
        # renders that no other path in `channel.health()` ever produces.
        assert "refused  :" not in report, report
        assert "pid 9999" not in report, report
        assert "listening there" not in report, report
    finally:
        srv.close()
        for leftover in (path, f"{path}{channel.HEALTH_SUFFIX}",
                          f"{path}{REFUSAL_SUFFIX}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


@needs_nofollow
def test_health_says_something_when_the_marker_could_not_be_read(
        forwarding, monkeypatch):
    """The third state, at the render boundary: `_refusal_lines` must not
    render an unreadable marker identically to no marker at all. Silence here
    is what a same-uid attacker -- or ordinary corruption -- gets for free if
    the two are folded together, on the exact surface #2133 built to stop
    exactly that shape of silence."""
    _symlink.require_symlink()
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, True)
    _, absent_report = channel.health(forwarding)

    os.symlink(str(Path(forwarding).parent / "nowhere.json"),
               f"{forwarding}{REFUSAL_SUFFIX}")
    _, unreadable_report = channel.health(forwarding)
    assert "symlink" in unreadable_report, unreadable_report
    assert unreadable_report != absent_report, (
        "an unreadable marker must not render identically to no marker")


# --- remedy 2: `subscribed` stops overclaiming -------------------------------

def test_a_recorded_collision_demotes_subscribed_to_cannot_determine(
        forwarding, monkeypatch):
    """The issue's own prescription: "the answer is not to fake it ... it is
    to stop saying subscribed ... and land in the third state." A tag naming a
    *configured* server is no longer enough once this run has first-hand
    evidence that a rival was refused this same socket -- `claude mcp get`
    cannot tell a live singleton from a dead one (#1558), so this must not
    read as delivery."""
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, True)
    _write_refusal(forwarding)
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: CANNOT DETERMINE", report
    assert rc == channel.RC_UNKNOWN, report
    assert "2133" in report, report


def test_a_configured_tag_with_no_recorded_collision_is_still_forwarding(
        forwarding, monkeypatch):
    """Positive control: remove the marker and the known-good state from
    #1543 is unchanged -- this is not a general demotion of `subscribed`."""
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, True)
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: FORWARDING", report
    assert rc == channel.RC_FORWARDING, report
    assert "subscribed" in report, report


@needs_nofollow
def test_an_unreadable_marker_does_not_get_read_as_no_collision(
        forwarding, monkeypatch):
    """The sharper case of the same bug, in the verdict rather than the
    report: `subscription()` must not take an unreadable marker as licence to
    return `subscribed`. It cannot rule the collision in, and on this file's
    own terms it must therefore not rule it out either -- `CANNOT DETERMINE`,
    not the positive answer, exactly as an ordinary lookup failure already
    does a few lines above this one."""
    _symlink.require_symlink()
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, True)
    os.symlink(str(Path(forwarding).parent / "nowhere.json"),
               f"{forwarding}{REFUSAL_SUFFIX}")
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] != "channel: FORWARDING", report
    assert rc != channel.RC_FORWARDING, report


def test_subscription_called_with_no_path_is_unaffected(monkeypatch):
    """Backward compatibility: `subscription()` gained an optional `path`
    argument. Every existing caller that does not pass one -- #1543's own
    direct tests among them -- must see exactly the old behaviour, because
    with no path there is nothing to check a marker against."""
    monkeypatch.setattr(channel, "_ps_fields", lambda pid: (
        (7, CONSUMER_ARGV, "") if pid == 99 else (1, TAGGED, "")))
    monkeypatch.setattr(channel, "_configured", lambda _n, _b=None: (True, ""))
    sub = channel.subscription(99)
    assert sub.state == channel.SUB_SUBSCRIBED, sub


# --- documentation ------------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(2133)
