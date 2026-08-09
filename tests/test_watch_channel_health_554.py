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
CHANNEL_OP = REPO / "presets" / "watch" / "channel.py"

if WATCH_DIR not in sys.path:
    sys.path.insert(0, WATCH_DIR)

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
        assert "39" in result.stdout and "2" in result.stdout
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
    path = _sock_path(tmp_path)
    srv = _listener(path)
    _write_health(path, pid=2, forwarded=39)  # pid 2 is not our consumer
    try:
        result = _run_health(path)
        assert result.returncode == RC_UNKNOWN, result.stdout + result.stderr
    finally:
        srv.close()
        os.unlink(path)
        os.unlink(path + ".health.json")


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
