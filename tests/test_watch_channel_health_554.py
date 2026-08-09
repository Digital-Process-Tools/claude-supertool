"""Delivery observability for the watch -> claude-channel bridge (#554, request 3).

The issue's own words: process-alive, socket-held and write-succeeds all read
identical whether delivery is working or not. Requests 1 and 2 shipped in
`6affddb`; this pins request 3.

The finding these tests encode is that **delivery into a Claude session is not
observable from any process except that session**. `mcp.notification()` is a
JSON-RPC notification: no id, no response, nothing to wait on. `channel.ts`
never writes back to the producer connection either, so a poller cannot learn
anything by reading. So there is no true/false answer available here, and a
green `rc=0` meaning "I wrote bytes" is the absence-read-as-presence defect.

What is genuinely observable, and what these tests therefore require:

- from the producer: whether anything was *listening* — a definite negative when
  the socket is absent or refuses, and `accepted` (not `delivered`) otherwise;
- from the consumer: its own counters, published to a health file beside the
  socket, so `forwarded` is a fact somebody can read rather than an inference;
- everywhere else: `unknown`, said out loud with the reason.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCH_DIR = str(REPO / "presets" / "watch")
PRESETS_DIR = str(REPO / "presets")
CHANNEL_OP = REPO / "presets" / "watch" / "channel.py"

for _dir in (WATCH_DIR, PRESETS_DIR):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import _proc  # noqa: E402
import channel  # noqa: E402
import transport  # noqa: E402


def _can_bind_af_unix() -> bool:
    """Measured once, not guessed from `os.name`.

    `hasattr(socket, "AF_UNIX")` is True on Windows builds of CPython, and
    whether a bind then succeeds depends on the OS build rather than on Python.
    A platform branch here would make these tests pass vacuously on the leg
    least like the author's machine, which is worse than skipping them.
    """
    if not hasattr(socket, "AF_UNIX"):
        return False
    probe = str(Path(tempfile.gettempdir()) / f"st554probe-{os.getpid()}.sock")
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


CAN_BIND = _can_bind_af_unix()

needs_socket = pytest.mark.skipif(
    not CAN_BIND, reason="this platform cannot bind an AF_UNIX socket"
)

# Exit codes are the three states. A single non-zero for both "nothing is
# listening" and "cannot tell" would put the two answers the issue is about
# back into one bucket.
RC_FORWARDING = 0
RC_NOT_DELIVERING = 1
RC_UNKNOWN = 3


def _sock_path(tmp_path: Path) -> str:
    # macOS caps AF_UNIX paths near 104 bytes and pytest's tmp_path is long, so
    # the socket goes in the system temp dir rather than under tmp_path.
    # `gettempdir()` rather than a literal "/tmp": this module runs on every leg
    # of the matrix, and "/tmp" on Windows resolves to a drive-root path that
    # need not exist.
    return str(Path(tempfile.gettempdir()) / f"st554h-{os.getpid()}-{time.time_ns()}.sock")


def _listener(path: str) -> socket.socket:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    return srv


def _run_health(sock: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # `PYTHONIOENCODING` because that is what supertool itself exports before
    # dispatching a preset op (`_supertool.py`, #415): the report contains em
    # dashes, and on a cp1252 Windows runner the child would otherwise die with
    # UnicodeEncodeError printing its own verdict. Running the op under a
    # different environment from the one it ships in would test something the
    # operator never runs.
    env = {**os.environ, "SUPERTOOL_WATCH_SOCK": sock, "PYTHONIOENCODING": "utf-8"}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(CHANNEL_OP), "health"],
        capture_output=True, text=True, env=env, timeout=30,
        # Explicit, not the locale codec: the report carries em dashes and the
        # Windows runners decode by cp1252, where the failure surfaces as a
        # TypeError naming nothing (#856).
        encoding="utf-8", errors="replace",
    )


#: Reaped children, kept alive as objects on purpose. On Windows a pid stays
#: reserved only while some handle to it is open, and `Popen` holds one until it
#: is garbage-collected — dropping the object would let the OS hand the pid to
#: somebody else and turn a "definitely dead" fixture into a coin flip.
_REAPED: list[subprocess.Popen] = []


def _reaped_pid() -> int:
    """A pid that has definitely exited, rather than one assumed to be free.

    This fixture used to be a literal `pid=2`. That is nothing on macOS and
    nothing on Windows, but on a Linux container it is `kthreadd` — so on the
    four ubuntu legs the test handed the op a *live stranger's* pid and then
    asserted the verdict for a dead one. The premise has to be constructed.

    `_proc.pid_alive` is the same probe the op uses, and using it as the
    precondition is deliberate: the subject under test is what `health()`
    concludes about a pid the OS reports as gone, so "gone" has to mean exactly
    what the op means by it.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    _REAPED.append(proc)
    return proc.pid


def _write_health(sock: str, **fields) -> None:
    record = {
        "pid": os.getpid(),
        "started": "2026-08-09T09:00:00Z",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sock_path": sock,
        "lines_read": 0,
        "forwarded": 0,
        "dropped": 0,
        "last_forwarded": None,
    }
    record.update(fields)
    Path(sock + ".health.json").write_text(json.dumps(record), encoding="utf-8")


# --- producer side: emit_socket stops swallowing what it knows ---------------

def test_emit_socket_reports_a_definite_negative_when_there_is_no_socket(monkeypatch, tmp_path):
    """No socket file: nothing could have received this. That is knowable."""
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "absent.sock"))
    verdict = transport.emit_socket({"event": "probe"})
    assert verdict.state == transport.EMIT_NO_LISTENER
    assert "no socket" in verdict.detail.lower()


@needs_socket
def test_emit_socket_reports_a_definite_negative_when_the_socket_is_stale(monkeypatch, tmp_path):
    """The file survives a crashed consumer. connect() refuses; that is knowable too,
    and it is the state that reads green from `lsof` and from `pgrep`."""
    path = _sock_path(tmp_path)
    srv = _listener(path)
    srv.close()  # leaves the path behind, nothing listening
    monkeypatch.setattr(transport, "SOCK_PATH", path)
    try:
        verdict = transport.emit_socket({"event": "probe"})
        assert verdict.state == transport.EMIT_NO_LISTENER
    finally:
        os.unlink(path)


@needs_socket
def test_emit_socket_says_accepted_and_never_delivered(monkeypatch, tmp_path):
    """A listener took the bytes. That is the ceiling of what the producer can
    observe, and the word for it is not "delivered"."""
    path = _sock_path(tmp_path)
    srv = _listener(path)
    monkeypatch.setattr(transport, "SOCK_PATH", path)
    try:
        verdict = transport.emit_socket({"event": "probe"})
        assert verdict.state == transport.EMIT_ACCEPTED
        assert "deliver" not in verdict.state
    finally:
        srv.close()
        os.unlink(path)


def test_emit_socket_declines_instead_of_raising_where_there_is_no_af_unix(monkeypatch, tmp_path):
    """`probe_socket` has carried this guard since it was written; its producer
    twin never adopted it, so on an interpreter without AF_UNIX the next line
    was an AttributeError — not an OSError, so not caught — out of the one
    function documented never to kill a watcher.

    `delattr` rather than a branch on `os.name`: this then exercises the arm on
    every leg of the matrix, instead of on the one leg least like this machine.
    """
    path = _sock_path(tmp_path)
    monkeypatch.setattr(transport, "SOCK_PATH", path)
    monkeypatch.setattr(transport.os.path, "exists", lambda p: p == path)
    monkeypatch.delattr(transport.socket, "AF_UNIX", raising=False)
    verdict = transport.emit_socket({"event": "probe"})
    assert verdict.state == transport.EMIT_UNKNOWN
    assert "AF_UNIX" in verdict.detail


@needs_socket
def test_emit_socket_does_not_call_a_vanished_socket_refused(monkeypatch, tmp_path):
    """Same misreport one file away: the `except` covers FileNotFoundError too,
    so a socket that was unlinked mid-write was reported as one that refused."""
    path = _sock_path(tmp_path)  # short, for the macOS AF_UNIX path cap
    monkeypatch.setattr(transport, "SOCK_PATH", path)
    monkeypatch.setattr(transport.os.path, "exists", lambda p: p == path)
    verdict = transport.emit_socket({"event": "probe"})
    assert verdict.state == transport.EMIT_NO_LISTENER
    assert "refused" not in verdict.detail
    assert "vanished" in verdict.detail


def test_emit_event_records_the_verdict_in_the_state_file(monkeypatch, tmp_path):
    """A watcher emitting into a dead socket must be inspectable afterwards. This
    is the silent window the issue describes, made into a readable fact."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "absent.sock"))
    transport.emit_event("gitlab-mr", "33173", "pipeline_failed", {"title": "t"})
    state = transport.read_state("gitlab-mr", "33173")
    assert state["last_emit"]["state"] == transport.EMIT_NO_LISTENER
    assert state["last_emit"]["ts"]


# --- the op: three states, three exit codes ---------------------------------

def test_health_is_a_definite_negative_when_no_socket_exists(tmp_path):
    result = _run_health(str(tmp_path / "absent.sock"))
    assert result.returncode == RC_NOT_DELIVERING, result.stdout + result.stderr
    assert "NOT DELIVERING" in result.stdout


@needs_socket
def test_health_is_a_definite_negative_when_the_socket_is_stale(tmp_path):
    path = _sock_path(tmp_path)
    _listener(path).close()
    try:
        result = _run_health(path)
        assert result.returncode == RC_NOT_DELIVERING, result.stdout + result.stderr
        assert "NOT DELIVERING" in result.stdout
    finally:
        os.unlink(path)


@needs_socket
def test_health_declines_when_a_consumer_is_bound_but_publishes_no_counters(tmp_path):
    """Bytes are accepted and nothing more is knowable. This must not read as
    green: it is exactly the state that produced a confidently wrong diagnosis
    in both directions in #554."""
    path = _sock_path(tmp_path)
    srv = _listener(path)
    try:
        result = _run_health(path)
        assert result.returncode == RC_UNKNOWN, result.stdout + result.stderr
        assert "CANNOT DETERMINE" in result.stdout
        assert "counters" in result.stdout
    finally:
        srv.close()
        os.unlink(path)


@needs_socket
def test_health_reports_the_consumers_own_forwarded_count(tmp_path):
    path = _sock_path(tmp_path)
    srv = _listener(path)
    _write_health(path, lines_read=41, forwarded=39, dropped=2,
                  last_forwarded=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        result = _run_health(path)
        assert result.returncode == RC_FORWARDING, result.stdout + result.stderr
        assert "FORWARDING" in result.stdout
        # The whole rendered line, not three substrings. `"2" in stdout` is true
        # of a timestamp, of `2048`, and of a report that never printed a
        # dropped count at all — an assertion that a broken renderer passes.
        assert "41 lines read, 39 forwarded, 2 dropped" in result.stdout
    finally:
        srv.close()
        os.unlink(path)
        os.unlink(path + ".health.json")


@needs_socket
def test_health_declines_when_the_consumers_counters_have_gone_stale(tmp_path):
    """A consumer that stopped refreshing is alive from every angle a session can
    check and may be wedged. Stale counters are not evidence of forwarding."""
    path = _sock_path(tmp_path)
    srv = _listener(path)
    _write_health(path, updated="2026-01-01T00:00:00Z", forwarded=39)
    try:
        result = _run_health(path)
        assert result.returncode == RC_UNKNOWN, result.stdout + result.stderr
        assert "CANNOT DETERMINE" in result.stdout
    finally:
        srv.close()
        os.unlink(path)
        os.unlink(path + ".health.json")


@needs_socket
def test_health_declines_when_the_health_file_belongs_to_a_dead_process(tmp_path):
    """A file left behind by a consumer that died is a frozen count that reads as
    health forever. The pid it names must be gone for that to be the case under
    test, so the pid is a reaped child rather than a number picked off a list."""
    pid = _reaped_pid()
    if _proc.pid_alive(pid):
        pytest.skip(f"the OS reused pid {pid} before the assertion could run")
    path = _sock_path(tmp_path)
    srv = _listener(path)
    _write_health(path, pid=pid, forwarded=39)
    try:
        result = _run_health(path)
        assert result.returncode == RC_UNKNOWN, result.stdout + result.stderr
        assert "CANNOT DETERMINE" in result.stdout
        assert str(pid) in result.stdout
    finally:
        srv.close()
        os.unlink(path)
        os.unlink(path + ".health.json")


@needs_socket
def test_health_never_presents_the_published_pid_as_a_verified_identity(tmp_path):
    """The case the ubuntu legs actually exercised, kept rather than deleted.

    Nothing checks that the pid in the health file is the process holding the
    socket, and nothing can: pids are reusable, so a live pid is not evidence
    that the consumer that wrote the file is the consumer that is bound. The op
    still reports FORWARDING — the counters are fresh, and something is
    refreshing them — but the report must attribute the pid to the file rather
    than assert it as a fact it established.
    """
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    path = _sock_path(tmp_path)
    srv = _listener(path)
    _write_health(path, pid=stranger.pid, forwarded=39)
    try:
        result = _run_health(path)
        assert result.returncode == RC_FORWARDING, result.stdout + result.stderr
        assert "self-reported" in result.stdout
        assert "pids are reusable" in result.stdout
    finally:
        stranger.kill()
        stranger.wait()
        srv.close()
        os.unlink(path)
        os.unlink(path + ".health.json")


@needs_socket
def test_health_declines_when_the_published_forwarded_count_is_unreadable(tmp_path):
    """`forwarded` is the number FORWARDING is named for. Absent, it used to
    render as `0 forwarded` — a quiet morning and an unreadable file printed as
    the same sentence, which is this op's own defect on its own headline."""
    path = _sock_path(tmp_path)
    srv = _listener(path)
    _write_health(path, forwarded=None)
    try:
        result = _run_health(path)
        assert result.returncode == RC_UNKNOWN, result.stdout + result.stderr
        assert "CANNOT DETERMINE" in result.stdout
        assert "0 forwarded" not in result.stdout
    finally:
        srv.close()
        os.unlink(path)
        os.unlink(path + ".health.json")


@needs_socket
def test_probe_socket_does_not_call_a_vanished_socket_refused(monkeypatch, tmp_path):
    """`connect()` raises FileNotFoundError when the path went away between the
    existence check and the call. Still nobody listening, but "refused" names a
    consumer that answered and said no, which is a different thing to go and
    look for."""
    # `_sock_path`, not `tmp_path`, even though nothing is ever bound here: the
    # macOS AF_UNIX path cap is ~104 bytes and pytest's tmp_path blows it, so a
    # long path fails ENAMETOOLONG and never reaches the arm under test.
    path = _sock_path(tmp_path)
    monkeypatch.setattr(channel.os.path, "exists", lambda p: p == path)
    state, detail = channel.probe_socket(path)
    assert state == "no-listener"
    assert "refused" not in detail
    assert "vanished" in detail


@needs_socket
def test_health_never_claims_delivery_into_the_session(tmp_path):
    """The ceiling has to be stated on the surface that would otherwise be read
    as proof. `forwarded` means handed to the MCP transport, and the report says
    so in the healthy case, not only in the broken ones."""
    path = _sock_path(tmp_path)
    srv = _listener(path)
    _write_health(path, forwarded=39,
                  last_forwarded=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        out = _run_health(path).stdout
        assert "not observable" in out
        assert "session" in out
    finally:
        srv.close()
        os.unlink(path)
        os.unlink(path + ".health.json")


def test_health_names_every_watcher_still_emitting_into_a_dead_socket(tmp_path):
    """The negative is only actionable with the blast radius attached: which
    watchers were writing into the void, and since when."""
    path = str(tmp_path / "absent.sock")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "supertool-watch-gitlab-mr__33173.state.json").write_text(json.dumps({
        "sock_path": path,
        "last_emit": {"ts": "2026-08-09T09:00:00Z", "state": "no-listener",
                      "detail": "no socket at " + path},
    }), encoding="utf-8")
    result = _run_health(path, {"SUPERTOOL_WATCH_STATE_DIR": str(state_dir)})
    assert result.returncode == RC_NOT_DELIVERING
    assert "gitlab-mr" in result.stdout and "33173" in result.stdout
