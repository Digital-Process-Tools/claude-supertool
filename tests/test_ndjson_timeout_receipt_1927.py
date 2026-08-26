"""A validator timeout says only that it waited, never what it received (#1927).

Follow-up to #1924 (buffer scanning) and deliberately scoped away from it: this
is about what an MCP-adapter timeout *reports*, not how a response glued to
noise is found. The incident: an adapter waited out the full 300s call budget
for a response that had, per the daemon's own log, left the server 2 seconds
in and never reached the client — an HTML error page leaked into the protocol
stream and the client blocked on `recv()` for the rest of the deadline with
no clue any of that had happened.

Four things this pins, one per test group:

1. Silence (`no bytes received`) and garbage (bytes arrived, none decoded to
   the awaited id) are different failures and say different things — the
   second quotes what arrived.
2. The receipt includes the daemon's own `<sock>.log` tail when readable.
3. `receive_until` gives up `idle_timeout` after the *last* byte, not after
   the full `call_timeout`, once any byte has arrived — the mechanism that
   turns a 300s hang into a few seconds. Silence with zero bytes ever
   received still waits the full `call_timeout`; there is no last byte to
   measure idleness from, so nothing changes there (the negative control).
4. The elapsed time is stated explicitly, not just the fact of giving up.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"

ADAPTERS = ["phpunit-mcp", "phpstan-mcp", "phpmd-mcp", "rector-mcp"]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def common(name: str):
    path = str(VALIDATORS / "common")
    if path not in sys.path:
        sys.path.insert(0, path)
    return _load(VALIDATORS / "common" / f"{name}.py", "_c1927_" + name)


def adapter(name: str):
    return _load(VALIDATORS / name / f"{name}.py", "_a1927_" + name.replace("-", "_"))


@pytest.fixture(autouse=True)
def _af_unix_shim(monkeypatch):
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)


# ---------------------------------------------------------------------------
# 1 & 4: describe_timeout — silence vs garbage, elapsed time always stated.
# ---------------------------------------------------------------------------

def test_describe_timeout_on_pure_silence_says_no_bytes_and_the_elapsed_time():
    ns = common("ndjson_scan")
    msg = ns.describe_timeout(b"", 42, 12.3, "/nonexistent/sock")
    assert "no bytes received" in msg
    assert "12.3s" in msg
    assert "first bytes" not in msg


def test_describe_timeout_on_garbage_quotes_what_arrived():
    """Bytes did arrive and none matched — the second failure #1927 names.
    Would still pass if the code merely counted bytes without quoting them,
    which is exactly the gap this asserts against: `first bytes` must name
    the actual content, not just that content existed."""
    ns = common("ndjson_scan")
    noise = b"<!DOCTYPE html><html>some fatal error page not json-rpc at all"
    msg = ns.describe_timeout(noise, 7, 4.0, "/nonexistent/sock")
    assert "no bytes received" not in msg
    assert "4.0s" in msg
    assert "<!DOCTYPE html>" in msg, msg


# ---------------------------------------------------------------------------
# 2: the daemon's own log tail, read best-effort.
# ---------------------------------------------------------------------------

def test_read_daemon_log_tail_returns_the_last_lines(tmp_path):
    ns = common("ndjson_scan")
    sock_path = str(tmp_path / "d.sock")
    log_path = sock_path + ".log"
    Path(log_path).write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")
    tail = ns.read_daemon_log_tail(sock_path, n_lines=3)
    assert "line 18" in tail and "line 19" in tail and "line 20" in tail
    assert "line 1\n" not in tail  # earliest lines dropped, not just present somewhere


def test_read_daemon_log_tail_missing_file_is_silent_not_a_second_failure(tmp_path):
    ns = common("ndjson_scan")
    sock_path = str(tmp_path / "no-such-daemon.sock")
    assert ns.read_daemon_log_tail(sock_path) == ""


def test_describe_timeout_includes_the_log_tail_when_present(tmp_path):
    ns = common("ndjson_scan")
    sock_path = str(tmp_path / "d.sock")
    Path(sock_path + ".log").write_text("request received\nresponse sent at t=2s\n")
    msg = ns.describe_timeout(b"", 1, 300.0, sock_path)
    assert "response sent at t=2s" in msg


def test_describe_timeout_omits_log_tail_section_when_nothing_to_read(tmp_path):
    ns = common("ndjson_scan")
    sock_path = str(tmp_path / "no-log-here.sock")
    msg = ns.describe_timeout(b"", 1, 1.0, sock_path)
    assert "daemon log tail" not in msg


# ---------------------------------------------------------------------------
# 3: idle deadline — the mechanism that turns a hang into a fast failure.
# ---------------------------------------------------------------------------

class _StepSocket:
    """Yields `chunks` one per `recv()`, then hangs (raises `socket.timeout`
    on every call after) — matching a daemon that stopped talking rather
    than one that closed the connection."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def settimeout(self, t):
        pass

    def recv(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        raise socket.timeout("timed out")


def test_receive_until_gives_up_on_idle_well_before_the_call_timeout(monkeypatch):
    """The core regression: one byte of noise arrives, then nothing more.
    `idle_timeout` is far shorter than `call_timeout`; the call must fail at
    (approximately) the idle deadline, not the call deadline. Would still
    pass if `receive_until` only respected `call_timeout` UNLESS the fake
    clock proves it gave up early — which is exactly what this checks by
    asserting the reported elapsed time, not just that it eventually raised.
    """
    ns = common("ndjson_scan")
    noise = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"
    sock = _StepSocket([noise])

    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    monkeypatch.setattr(ns.time, "monotonic", fake_monotonic)

    # Drive the clock forward as recv() is called: first call returns the
    # noise chunk at t=0; every call after simulates 1s passing with no more
    # bytes, until the idle deadline (5s after the last byte) is crossed.
    real_recv = sock.recv

    def ticking_recv(n):
        result = real_recv(n)
        clock["t"] += 1.0
        return result

    sock.recv = ticking_recv

    call_timeout = 300.0   # the old, slow ceiling
    idle_timeout = 5.0     # #1927's short one

    with pytest.raises(RuntimeError) as excinfo:
        ns.receive_until(sock, 999, call_timeout, "/nonexistent/sock",
                          idle_timeout=idle_timeout)

    # Must have given up nowhere near the 300s call_timeout.
    assert clock["t"] < 10.0, (
        f"receive_until waited until t={clock['t']}, which is not an idle "
        f"deadline of {idle_timeout}s after the last byte")
    assert "no bytes received" not in str(excinfo.value)


def test_receive_until_still_waits_the_full_call_timeout_on_pure_silence(monkeypatch):
    """Negative control for the test above: with zero bytes ever received,
    there is no 'last byte' to measure idleness from, so the short idle
    deadline must NOT cut the wait short — it should still run out the full
    call_timeout. Without this, a version of the fix that (wrongly) idles
    out on total silence too would pass the test above and this file would
    never catch it."""
    ns = common("ndjson_scan")

    class NeverSocket:
        def settimeout(self, t):
            pass

        def recv(self, n):
            raise socket.timeout("timed out")

    clock = {"t": 0.0}
    monkeypatch.setattr(ns.time, "monotonic", lambda: clock["t"])

    call_timeout = 30.0
    idle_timeout = 5.0  # shorter than call_timeout, must not apply here

    # settimeout is called with `remaining`; simulate the socket call itself
    # consuming that much wall-clock time before raising, same as a real
    # blocking recv() would.
    sock = NeverSocket()
    orig_settimeout = sock.settimeout

    def advancing_settimeout(remaining):
        clock["t"] += remaining
        return orig_settimeout(remaining)

    sock.settimeout = advancing_settimeout

    with pytest.raises(RuntimeError) as excinfo:
        ns.receive_until(sock, 999, call_timeout, "/nonexistent/sock",
                          idle_timeout=idle_timeout)

    assert "no bytes received" in str(excinfo.value)
    assert clock["t"] >= call_timeout


# ---------------------------------------------------------------------------
# Each adapter, wired: a real timeout receipt names elapsed time and the
# received bytes, not just "timed out".
# ---------------------------------------------------------------------------

class _NoiseThenHangSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def connect(self, p):
        pass

    def sendall(self, data):
        pass

    def settimeout(self, t):
        pass

    def recv(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        raise socket.timeout("timed out")


@pytest.mark.parametrize("name", ADAPTERS)
def test_adapter_timeout_receipt_names_elapsed_time_and_received_bytes(name, monkeypatch):
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)
    garbage = b"<!DOCTYPE html>fatal error, not a jsonrpc frame at all"
    fake = _NoiseThenHangSocket([garbage])
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: fake)

    clock = {"t": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

    real_recv = fake.recv

    def ticking_recv(n):
        result = real_recv(n)
        clock["t"] += 20.0
        return result

    fake.recv = ticking_recv

    with pytest.raises(RuntimeError) as excinfo:
        mod.ndjson_call("/fake/sock", "/fake/target")

    msg = str(excinfo.value)
    assert "<!DOCTYPE html>" in msg, msg
    # Gave up on idle, well short of the adapter's own (120-300s) call budget.
    assert clock["t"] < mod.CALL_TIMEOUT_SEC, msg
