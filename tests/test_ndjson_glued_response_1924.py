"""A JSON-RPC response glued to noise with no line separator (#1924).

`ndjson_call` in the four warm MCP adapters (`phpunit-mcp`, `phpstan-mcp`,
`phpmd-mcp`, `rector-mcp`) used to parse its socket buffer one LF-delimited
line at a time. A forked test process fataling makes the host application
render an HTML error page into the same stream, and the daemon's real
JSON-RPC response then lands glued to the end of the last HTML line with no
separator:

    </html>{"jsonrpc":"2.0","id":2,"result":{...}}

`json.loads` on a line starting with `</html>` raises, the line is skipped,
and the adapter blocks on `recv()` until the full `CALL_TIMEOUT_SEC` even
though the answer was already in the buffer. Measured (#1924): buffer 46608
bytes over 512 lines, answer present 2s in, adapter gave up 5 minutes later.

`validators/common/ndjson_scan.find_response` is the fix: it scans the whole
buffer for the response rather than requiring it to open a line. This file
pins the scanner directly (fast, no socket) and then drives each of the four
adapters' `ndjson_call` through a fake socket carrying the exact glued shape,
so a regression in any one adapter's wiring — not just the shared scanner —
fails here.

No 300-second wait anywhere: every adapter test injects `time.monotonic` so
the "genuinely never answers" pair proves the deadline is honoured without
the test itself paying for it.

**Platform note.** `ndjson_call` opens with
`socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)`, and Windows Python
builds do not expose `socket.AF_UNIX` at all (`validators/common/refusal.py`,
`daemon_transport_reason`). That is not a gap this file leaves untested: in
production, `refusal.daemon_transport_reason()` makes `ensure_daemon` raise
`DaemonUnavailable` before `ndjson_call` is ever called on such a build, so
the real UDS connection this line makes genuinely never happens there —
`test_windows_warm_validators_544.py` is where that gate itself is pinned.
But `socket.AF_UNIX` is evaluated as a plain attribute lookup to build the
call's *argument*, which happens before Python decides which `socket.socket`
implementation to invoke — so it raises `AttributeError` even though every
test below immediately replaces `socket.socket` with a fake that ignores its
arguments entirely. The `_af_unix_shim` fixture below plugs in a placeholder
only when the platform has none, exactly so these tests can exercise
`ndjson_call`'s own control flow — buffering, scanning, the deadline loop —
on every CI platform, the same way `find_response` itself already is
(`test_find_response_locates_the_glued_frame` and friends touch no socket at
all). It does not claim AF_UNIX sockets work on Windows; nothing here routes
through a real one.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"

ADAPTERS = ["phpunit-mcp", "phpstan-mcp", "phpmd-mcp", "rector-mcp"]


@pytest.fixture(autouse=True)
def _af_unix_shim(monkeypatch):
    """See the platform note in the module docstring. A no-op wherever
    `socket.AF_UNIX` already exists; every adapter test below patches
    `socket.socket` itself, so the placeholder value is never dereferenced."""
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def adapter(name: str):
    return _load(VALIDATORS / name / f"{name}.py", "_v1924_" + name.replace("-", "_"))


def common(name: str):
    path = str(VALIDATORS / "common")
    if path not in sys.path:
        sys.path.insert(0, path)
    return _load(VALIDATORS / "common" / f"{name}.py", "_c1924_" + name)


# ---------------------------------------------------------------------------
# The shared scanner, pinned directly — no socket, no adapter, no timing.
# ---------------------------------------------------------------------------

def _glued_buffer(id_2_result: dict) -> bytes:
    """The exact shape from the issue: an error frame, a warning line, ~45KB
    of HTML, then the id=2 response appended to the final `</html>` line."""
    error_frame = json.dumps({"jsonrpc": "2.0", "id": 1,
                               "result": {"protocolVersion": "2024-11-05"}})
    warning = "PHP Warning:  some warning text on stderr-adjacent stdout"
    html = "<html><body>" + ("x" * 400 + "\n") * 110 + "</html>"
    response = json.dumps({"jsonrpc": "2.0", "id": 2, "result": id_2_result})
    # No separator between the HTML's last line and the response — the bug.
    return (error_frame + "\n" + warning + "\n" + html + response).encode()


def test_find_response_locates_the_glued_frame():
    ns = common("ndjson_scan")
    buf = _glued_buffer({"structuredContent": {"output": "{}"}})
    obj = ns.find_response(buf, 2)
    assert obj is not None
    assert obj["id"] == 2
    assert obj["result"]["structuredContent"]["output"] == "{}"


def test_find_response_does_not_match_a_different_id():
    ns = common("ndjson_scan")
    buf = _glued_buffer({"structuredContent": {}})
    assert ns.find_response(buf, 99) is None


def test_find_response_skips_an_undecodable_brace_inside_a_string():
    """A `{` inside a quoted string must not be mistaken for a real object
    boundary that happens to decode into something claiming the awaited id."""
    ns = common("ndjson_scan")
    noise = json.dumps({"jsonrpc": "2.0", "id": 1,
                         "result": {"msg": 'contains a brace: { not json'}})
    real = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"ok": True}})
    buf = (noise + real).encode()
    obj = ns.find_response(buf, 2)
    assert obj is not None and obj["result"] == {"ok": True}


def test_describe_buffer_distinguishes_empty_from_unmatched():
    ns = common("ndjson_scan")
    assert "no bytes" in ns.describe_buffer(b"", 2)
    noise = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
    msg = ns.describe_buffer(noise, 2)
    assert "no bytes" not in msg
    assert str(len(noise)) in msg


# ---------------------------------------------------------------------------
# Each adapter's ndjson_call, driven through a fake UDS socket.
# ---------------------------------------------------------------------------

class FakeSocket:
    """Just enough of `socket.socket` for `ndjson_call`'s call shape."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def settimeout(self, t):
        pass

    def connect(self, p):
        pass

    def sendall(self, data):
        pass

    def recv(self, n):
        return next(self._chunks, b"")


@pytest.mark.parametrize("name", ADAPTERS)
def test_adapter_returns_the_glued_response_instead_of_timing_out(name, monkeypatch):
    """The reproduction: one `recv()` chunk carries the whole glued buffer,
    and the adapter must return the id=2 response from *that* chunk — not
    loop back into another `recv()` that would (on the real bug) block until
    the deadline."""
    mod = adapter(name)
    # #1935 sends a random per-call id instead of the fixed literal `2` this
    # buffer is glued around; pin it back to `2` here since this test is
    # about the buffer-scanning mechanism, not id unpredictability (that is
    # tests/test_ndjson_unpredictable_id_1935.py's job).
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)
    buf = _glued_buffer({"structuredContent": {"output": "{}"}, "exit_code": 0})
    fake = FakeSocket([buf, b""])
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: fake)

    calls = {"n": 0}
    real_monotonic = time.monotonic

    def counting_monotonic():
        calls["n"] += 1
        return real_monotonic()

    monkeypatch.setattr(mod.time, "monotonic", counting_monotonic)

    resp = mod.ndjson_call("/fake/sock", "/fake/test.php")

    assert resp["id"] == 2
    # Found on the chunk that carried it — no second `recv()` needed to
    # re-discover data already in `buf`. `FakeSocket` only has two chunks
    # queued (the glued buffer, then EOF), so a second `recv()` would have
    # returned `b""` and (pre-fix) raised instead of returning.
    assert calls["n"] <= 3, "resolved without spinning back through recv()"


@pytest.mark.parametrize("name", ADAPTERS)
def test_adapter_does_not_hang_and_does_not_fabricate_when_id_never_arrives(
        name, monkeypatch):
    """The negative control for the fix above: a buffer that never carries
    the awaited id must still raise, and must do so without the test paying
    for the real 120-300s deadline — `time.monotonic` is advanced past the
    adapter's own `CALL_TIMEOUT_SEC` only after one chunk has been read, so
    the run actually reaches the scanner with real bytes in `buf` rather
    than exiting before the first `recv()`. The error must say bytes were
    received with no matching frame — not the "no bytes received" shape a
    buffer that never got that far would also raise with."""
    mod = adapter(name)
    noise = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"
    fake = FakeSocket([noise, b""])
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: fake)

    # [0]: deadline = monotonic() + CALL_TIMEOUT_SEC. [1]: first `remaining`
    # check — still 0.0, so the loop is entered and `noise` is read. [2]:
    # second `remaining` check, now past the deadline, so the loop exits
    # with `buf` holding `noise` rather than empty.
    times = iter([0.0, 0.0, mod.CALL_TIMEOUT_SEC + 1])
    monkeypatch.setattr(mod.time, "monotonic",
                         lambda: next(times, mod.CALL_TIMEOUT_SEC + 2))

    with pytest.raises(RuntimeError) as excinfo:
        mod.ndjson_call("/fake/sock", "/fake/test.php")

    msg = str(excinfo.value)
    assert "no bytes received" not in msg, msg
    assert str(len(noise)) in msg, msg


@pytest.mark.parametrize("name", ADAPTERS)
def test_adapter_recv_timeout_is_caught_not_left_to_propagate_raw(name, monkeypatch):
    """`recv()` itself can time out — a real socket does this when
    `remaining` runs out mid-call, not just when the deadline check between
    calls catches it first. Each adapter now retimes `s.settimeout(remaining)`
    every iteration and wraps `s.recv()` in `except (socket.timeout,
    TimeoutError)`; this drives that branch directly, with a socket that
    raises on `recv()` rather than one whose absence is only inferred from
    the deadline arithmetic in the test above. Uncaught, `socket.timeout`
    would propagate past `ndjson_call` as a different exception type than
    the `RuntimeError` every caller of this function expects."""
    mod = adapter(name)

    class TimingOutSocket(FakeSocket):
        def recv(self, n):
            raise socket.timeout("timed out")

    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: TimingOutSocket([]))
    # One `monotonic()` call for the deadline; every call after that falls
    # back to a value still short of it, so the loop reaches `recv()` (and
    # only `recv()`, since the `except` breaks the loop on the first raise)
    # rather than exiting on the deadline check before ever calling it.
    monkeypatch.setattr(mod.time, "monotonic", lambda: 0.0)

    with pytest.raises(RuntimeError) as excinfo:
        mod.ndjson_call("/fake/sock", "/fake/test.php")

    assert "no bytes received" in str(excinfo.value)
