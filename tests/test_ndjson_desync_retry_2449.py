"""A desynchronised pipe recovers instead of returning `NOT CHECKED` silently
after the write it was checking has already been accepted (#2449).

The mechanism, read off `presets/mcp/daemon.py` and `validators/common/
ndjson_scan.py` rather than assumed from the issue: the daemon serves one
client's request/response exchange at a time over a Unix socket. If a client
disconnects mid-exchange, bytes already in flight for it can still be sitting
in the kernel's socket buffer when the *next* client connects and is handed
the same daemon process to talk to. That next client's `receive_until` then
sees a well-formed JSON-RPC response addressed to somebody else's id, never
finds its own, and (pre-#2449) raised a plain `RuntimeError` -- which
`refusal.guard_main` turns into `crashed()`, rendered `NOT CHECKED` by the
core, even though the write that triggered the check had already landed.

Two things had to be told apart to fix the *class* (the issue's option 3)
without turning every ordinary slow call into a silent double-timeout:

1. A response addressed to a *foreign* id (neither the id this call is
   waiting for, nor `INITIALIZE_ID` -- every exchange's own `initialize`
   frame gets one of those first, and that is not evidence of anything
   wrong) is real evidence of a desynchronised pipe.
2. Plain silence, or noise that never parses as a JSON-RPC response at all,
   is not that evidence -- it is exactly as consistent with a daemon (or an
   analysis) that is just slow or genuinely gone, and retrying either of
   those wastes a full second `call_timeout` for no corresponding chance of
   recovery.

Only (1) gets `DesyncDetected` and a retry against a freshly-respawned
daemon; (2) still raises the plain `RuntimeError` every existing caller
already handles, unretried.
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
    return _load(VALIDATORS / "common" / f"{name}.py", "_c2449_" + name)


def adapter(name: str):
    return _load(VALIDATORS / name / f"{name}.py", "_a2449_" + name.replace("-", "_"))


@pytest.fixture(autouse=True)
def _af_unix_shim(monkeypatch):
    """See the platform note in test_ndjson_glued_response_1924.py."""
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)


# ---------------------------------------------------------------------------
# Scanner level: a foreign id is desync evidence, the exchange's own
# `initialize` id is not.
# ---------------------------------------------------------------------------

def test_scan_does_not_flag_the_exchanges_own_initialize_response():
    """The shape every ordinary exchange produces while phpstan is still
    working: the `initialize` response (id=1) has arrived, the real answer
    (some other id) has not yet. This must NOT read as desync -- would still
    pass if `_scan` treated any non-matching id as foreign, which is exactly
    the gap #2449 asks to close (that reading made *every* in-flight call
    look desynchronised, not just a genuinely glued pipe)."""
    ns = common("ndjson_scan")
    buf = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode()
    obj, saw_foreign = ns._scan(buf, want_id=999)
    assert obj is None
    assert saw_foreign is False


def test_scan_flags_a_response_addressed_to_a_foreign_id():
    """The actual desync shape: a well-formed response glued in the buffer,
    addressed to neither this call's id nor the initialize id -- i.e. some
    other client's exchange."""
    ns = common("ndjson_scan")
    buf = json.dumps({"jsonrpc": "2.0", "id": 77, "result": {}}).encode()
    obj, saw_foreign = ns._scan(buf, want_id=999)
    assert obj is None
    assert saw_foreign is True


def test_receive_until_raises_plain_runtime_error_on_pure_silence():
    """Negative control: zero bytes ever received is still a plain
    RuntimeError, not DesyncDetected -- retrying that would just wait out a
    second identical timeout for a daemon that never said anything at all."""
    ns = common("ndjson_scan")

    class NeverSocket:
        def settimeout(self, t):
            pass

        def recv(self, n):
            raise socket.timeout("timed out")

    with pytest.raises(RuntimeError) as excinfo:
        ns.receive_until(NeverSocket(), 999, 0.01, "/nonexistent/sock", idle_timeout=0.01)
    assert not isinstance(excinfo.value, ns.DesyncDetected)


def test_receive_until_raises_desync_detected_on_a_foreign_id():
    """RED before #2449's fix: `receive_until` raised a plain `RuntimeError`
    here too, indistinguishable from the silence case above -- a caller had
    no way to tell "a retry might help" from "nothing will ever arrive"."""
    ns = common("ndjson_scan")
    foreign = json.dumps({"jsonrpc": "2.0", "id": 77, "result": {}}).encode() + b"\n"

    class _OneShotThenClosed:
        def __init__(self, chunk):
            self._chunk = chunk

        def settimeout(self, t):
            pass

        def recv(self, n):
            if self._chunk is not None:
                c, self._chunk = self._chunk, None
                return c
            return b""  # closed -- receive_until breaks on empty chunk

    with pytest.raises(ns.DesyncDetected):
        ns.receive_until(_OneShotThenClosed(foreign), 999, 5.0, "/nonexistent/sock")


# ---------------------------------------------------------------------------
# Adapter level: `ndjson_call` retries exactly once, against a
# freshly-respawned daemon, on `DesyncDetected` -- and recovers.
# ---------------------------------------------------------------------------

class _QueueSocket:
    """Yields `chunks` one per `recv()`, `b""` (closed) once exhausted."""

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
        return b""


@pytest.mark.parametrize("name", ADAPTERS)
def test_ndjson_call_recovers_after_one_retry_on_desync(name, monkeypatch):
    """The positive case #2449 asks for: a client that inherited another
    client's tail end (id=77, not ours) gets exactly one retry against a
    freshly-respawned daemon, and the second attempt's real answer is
    returned -- not a `NOT CHECKED` for a write that was already accepted."""
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)

    foreign = json.dumps({"jsonrpc": "2.0", "id": 77, "result": {}}).encode() + b"\n"
    real_answer = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"errors": []}}}
    ).encode() + b"\n"
    sockets = iter([_QueueSocket([foreign]), _QueueSocket([real_answer])])
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: next(sockets))

    calls = {"n": 0}

    def fake_force_respawn(cwd, daemon_name, **kw):
        calls["n"] += 1
        return "/fake/respawned.sock"

    monkeypatch.setattr(mod._spawn, "force_respawn", fake_force_respawn)

    resp = mod.ndjson_call("/fake/sock", "/fake/target.php")

    assert resp["id"] == 2
    assert calls["n"] == 1, "must respawn exactly once, not zero and not more"


@pytest.mark.parametrize("name", ADAPTERS)
def test_ndjson_call_does_not_respawn_on_plain_silence(name, monkeypatch):
    """Silence is not desync evidence: a daemon that never says anything at
    all must not be retried, or every genuinely dead daemon costs a second
    full CALL_TIMEOUT_SEC for a retry that could not possibly help."""
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)

    clock = {"t": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

    class _DeadSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def connect(self, p):
            pass

        def sendall(self, data):
            pass

        def settimeout(self, t):
            clock["t"] += t

        def recv(self, n):
            raise socket.timeout("timed out")

    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: _DeadSocket())

    calls = {"n": 0}
    monkeypatch.setattr(mod._spawn, "force_respawn",
                         lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    with pytest.raises(RuntimeError):
        mod.ndjson_call("/fake/sock", "/fake/target.php")

    assert calls["n"] == 0, "silence must not trigger a respawn+retry"


@pytest.mark.parametrize("name", ADAPTERS)
def test_ndjson_call_still_reports_the_failure_when_the_retry_is_exhausted(name, monkeypatch):
    """Positive control for the recovery test above, paired in the same
    fixture family: a daemon that is genuinely gone recovers from nothing,
    however many times it is respawned. One retry is spent (per #2449's
    design -- exactly one, not an unbounded loop) and the second attempt's
    own failure is what the caller ultimately sees, unmasked -- never a
    fabricated success."""
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)

    foreign = json.dumps({"jsonrpc": "2.0", "id": 77, "result": {}}).encode() + b"\n"
    # First attempt: desync (foreign id). Second attempt (post-respawn):
    # closed immediately with zero bytes -- the fresh daemon is just as
    # unreachable, e.g. the binary is gone entirely.
    sockets = iter([_QueueSocket([foreign]), _QueueSocket([])])
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: next(sockets))

    calls = {"n": 0}

    def fake_force_respawn(cwd, daemon_name, **kw):
        calls["n"] += 1
        return "/fake/respawned.sock"

    monkeypatch.setattr(mod._spawn, "force_respawn", fake_force_respawn)

    with pytest.raises(RuntimeError):
        mod.ndjson_call("/fake/sock", "/fake/target.php")

    assert calls["n"] == 1, "exactly one retry is spent, not an unbounded loop"
