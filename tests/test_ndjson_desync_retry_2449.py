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
import os
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


# ---------------------------------------------------------------------------
# Review round 2, finding 1: force_respawn kills the ONE shared daemon
# unconditionally, with no way to tell a genuinely healthy, currently
# in-flight OTHER exchange from a desynchronised one -- so THAT caller pays
# for a stranger's disconnect all over again, in a brand new instance of the
# exact class #2449 exists to close. call_with_retry's pid_probe closes it:
# a plain RuntimeError whose daemon pid changed underneath the call is
# treated as collateral damage and gets the same one retry, without calling
# respawn() again (a fresh daemon already exists).
# ---------------------------------------------------------------------------

def test_call_with_retry_retries_a_collateral_runtime_error_when_pid_changed():
    """Unit-level: the daemon this caller was talking to no longer exists by
    the time it fails -- pid_probe says so -- and the caller was never the
    one that broke it, so no second respawn() is warranted or expected."""
    ns = common("ndjson_scan")
    pids = iter([111, 222])
    calls = {"n": 0}

    def do_call():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset by peer")
        return {"ok": True}

    def respawn():
        raise AssertionError("must not respawn again -- someone else already did")

    result = ns.call_with_retry(do_call, respawn, pid_probe=lambda: next(pids))
    assert result == {"ok": True}
    assert calls["n"] == 2


def test_call_with_retry_does_not_retry_a_runtime_error_when_pid_unchanged():
    """Negative control: nothing changed underneath this caller (the same
    pid answers before and after the failure), so this is the ordinary
    unreachable-daemon case -- retrying it would be exactly the
    double-timeout call_with_retry exists to avoid."""
    ns = common("ndjson_scan")
    calls = {"n": 0}

    def do_call():
        calls["n"] += 1
        raise RuntimeError("no bytes received")

    def respawn():
        raise AssertionError("must not respawn on plain silence")

    with pytest.raises(RuntimeError):
        ns.call_with_retry(do_call, respawn, pid_probe=lambda: 111)
    assert calls["n"] == 1


def test_call_with_retry_without_a_pid_probe_behaves_as_before():
    """Omitting pid_probe entirely (the pre-review-round-2 shape) must never
    retry a plain RuntimeError -- the branch is additive, not a change to
    the default."""
    ns = common("ndjson_scan")
    calls = {"n": 0}

    def do_call():
        calls["n"] += 1
        raise RuntimeError("no bytes received")

    def respawn():
        raise AssertionError("must not respawn -- no pid_probe was given")

    with pytest.raises(RuntimeError):
        ns.call_with_retry(do_call, respawn)
    assert calls["n"] == 1


@pytest.mark.parametrize("name", ADAPTERS)
def test_ndjson_call_retries_a_collateral_failure_without_calling_force_respawn(name, monkeypatch):
    """Adapter-level wiring for the same fix: a connection that dies with
    nothing at all (as a SIGKILL mid-exchange from someone else's
    force_respawn would look, from the victim's side) is retried once when
    the daemon's pid changed underneath this call -- and does NOT call
    force_respawn a second time, because whoever replaced the daemon
    already did."""
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)

    real_answer = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"errors": []}}}
    ).encode() + b"\n"
    sockets = iter([_QueueSocket([]), _QueueSocket([real_answer])])
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: next(sockets))

    pids = iter([111, 222])
    monkeypatch.setattr(mod._spawn, "daemon_pid", lambda *a, **k: next(pids))

    def fail_respawn(*a, **k):
        raise AssertionError(
            "must not force_respawn for a collateral failure -- a fresh "
            "daemon already exists at the same socket path")

    monkeypatch.setattr(mod._spawn, "force_respawn", fail_respawn)

    resp = mod.ndjson_call("/fake/sock", "/fake/target.php")
    assert resp["id"] == 2


@pytest.mark.parametrize("name", ADAPTERS)
def test_ndjson_call_does_not_retry_plain_silence_when_pid_unchanged(name, monkeypatch):
    """Positive control, paired with the test above: when the daemon's pid
    is unchanged (the real, unmocked `_spawn.daemon_pid` reading a
    nonexistent pidfile returns 0 both times), plain silence still reports
    NOT CHECKED unretried -- the collateral-damage branch must not turn
    into a general "always retry a RuntimeError" rule."""
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: _QueueSocket([]))

    def fail_respawn(*a, **k):
        raise AssertionError("must not respawn -- nothing indicates collateral damage")

    monkeypatch.setattr(mod._spawn, "force_respawn", fail_respawn)

    with pytest.raises(RuntimeError):
        mod.ndjson_call("/fake/sock", "/fake/target.php")


# ---------------------------------------------------------------------------
# Review round 2, finding 2: the `initialize` id used to be the shared,
# hardcoded literal `1` on every call, so a foreign client's own leftover
# `initialize` reply (also `id: 1`) was indistinguishable from this
# exchange's own -- masking exactly the interleaving where a disconnect
# happens between sending `initialize` and reading its reply. Each adapter
# now draws a random `initialize` id per exchange and passes it as its own
# `own_ids`, so a foreign client's `initialize` reply (drawn independently,
# essentially never colliding) reads as foreign like any other stray frame.
# ---------------------------------------------------------------------------

def test_scan_does_not_confuse_a_foreign_clients_initialize_reply_with_our_own():
    """The exact #2449-review-round-2 scenario: two callers, each with their
    own randomly-drawn initialize id. A leftover reply to the OTHER
    caller's initialize frame is real desync evidence for this one -- it is
    shaped exactly like an initialize response, and the only thing that
    tells it apart from our own is the id, which is why the id has to be
    unique per caller in the first place."""
    ns = common("ndjson_scan")
    my_init_id = 555555
    stranger_init_id = 999999  # some OTHER client's own initialize id
    buf = json.dumps(
        {"jsonrpc": "2.0", "id": stranger_init_id,
         "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
    ).encode()
    obj, saw_foreign = ns._scan(buf, want_id=123, own_ids=frozenset((my_init_id,)))
    assert obj is None
    assert saw_foreign is True, (
        "a foreign client's own initialize reply must not be exempted just "
        "because it LOOKS like an initialize response")


def test_scan_still_exempts_this_callers_own_randomly_drawn_initialize_reply():
    """Positive control for the test above: OUR OWN initialize reply (the id
    we actually generated and sent) must still be exempt, whatever value it
    happens to be -- own_ids is caller-supplied precisely so this is not
    tied to the old shared literal 1 any more."""
    ns = common("ndjson_scan")
    my_init_id = 555555
    buf = json.dumps(
        {"jsonrpc": "2.0", "id": my_init_id,
         "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
    ).encode()
    obj, saw_foreign = ns._scan(buf, want_id=123, own_ids=frozenset((my_init_id,)))
    assert obj is None
    assert saw_foreign is False


@pytest.mark.parametrize("name", ADAPTERS)
def test_ndjson_call_passes_its_own_random_initialize_id_as_own_ids(name, monkeypatch):
    """Wiring-level regression guard for finding 2: assert the adapter no
    longer sends the literal `1` for `initialize` at all, and that whatever
    id it DOES send is exactly what protects it -- a stray reply to some
    OTHER id (simulating a foreign client's leftover initialize reply) is
    still detected as desync even though it is shaped like an initialize
    response."""
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)

    sent = {}

    class _RecordingSocket(_QueueSocket):
        def sendall(self, data):
            msgs = [json.loads(line) for line in data.decode().splitlines() if line]
            sent["init_id"] = next(
                m["id"] for m in msgs if m.get("method") == "initialize")

    # A leftover reply to some id that is neither the pinned req_id (2) nor
    # whatever init_id this exchange drew -- a foreign client's own
    # initialize reply, shaped just like a real one.
    stranger_reply = json.dumps(
        {"jsonrpc": "2.0", "id": 987654,
         "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
    ).encode() + b"\n"
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: _RecordingSocket([stranger_reply]))

    calls = {"n": 0}
    monkeypatch.setattr(mod._spawn, "force_respawn",
                         lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "/fake/sock")

    with pytest.raises(RuntimeError):
        mod.ndjson_call("/fake/sock", "/fake/target.php")

    assert sent.get("init_id") not in (1, None), (
        "the initialize frame must no longer carry the shared literal 1"
    )
    assert calls["n"] == 1, "the foreign initialize-shaped reply must still trigger one respawn+retry"


# ---------------------------------------------------------------------------
# Windows regression: pid_probe must NAME the pidfile, not RESOLVE it.
#
# The first cut of pid_probe read `_paths.socket_pid_paths(WORKING_DIR,
# DAEMON_NAME)[1]` eagerly at the top of `ndjson_call`. That helper reaches
# the path through `runtime_dir()`, which creates and validates the runtime
# directory and `sys.exit`s wherever ownership cannot be checked (#544) --
# i.e. anywhere `os.geteuid` is absent, which is every Windows runner.
# `_paths` states the invariant that keeps that refusal away from the warm
# adapters (they decline for want of AF_UNIX first); resolving a path here
# broke it, and `ndjson_call` became a SystemExit on Windows for every one
# of these tests, including the ones that predate #2449 entirely.
#
# Observed, not reasoned: 56 failed / 21 passed locally with `os.geteuid`
# deleted, matching the 56 reported on all four `pytest (windows-latest, *)`
# legs of PR #2497 while every macOS and Linux leg was green.
# ---------------------------------------------------------------------------

@pytest.fixture
def _no_geteuid(monkeypatch):
    """Windows, as far as `_paths` is concerned: `os.geteuid` does not exist.

    That single attribute is what `_open_runtime_dir` branches on, so
    deleting it reproduces the real refusal through the real code, rather
    than asserting against a stubbed-out copy of it.
    """
    monkeypatch.delattr(os, "geteuid", raising=False)


def test_the_no_geteuid_fixture_actually_disables_ownership_checking(_no_geteuid):
    """Positive control, and the whole reason the guard below means anything.

    The guard is of the form "this does NOT blow up", which also passes when
    the fixture silently did nothing -- if some future refactor stops
    `runtime_dir()` consulting `os.geteuid`, or the `delattr` stops taking
    effect, the guard would go on passing while testing nothing at all. So
    pin the loud half in the same fixture: under this exact fixture,
    resolving a runtime dir MUST still refuse.
    """
    sys.path.insert(0, str(REPO / "presets" / "mcp"))
    import _paths

    with pytest.raises(SystemExit) as excinfo:
        _paths.runtime_dir()
    assert "cannot verify ownership" in str(excinfo.value)


@pytest.mark.parametrize("name", ADAPTERS)
def test_ndjson_call_never_resolves_the_runtime_dir(name, _no_geteuid, monkeypatch):
    """The guard: a whole `ndjson_call` exchange completes on a platform
    where `runtime_dir()` refuses.

    Paired with the positive control above, which proves the fixture is
    live. The socket path handed in is synthetic (`/fake/sock`) exactly as
    a real caller's is already-resolved by `ensure_daemon` upstream, so
    nothing on this path has any business asking the filesystem where the
    runtime directory is.
    """
    mod = adapter(name)
    monkeypatch.setattr(mod.random, "randrange", lambda *a, **k: 2)

    answer = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"errors": []}}}
    ).encode() + b"\n"
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: _QueueSocket([answer]))
    monkeypatch.setattr(mod._spawn, "force_respawn",
                         lambda *a, **k: pytest.fail("no desync here -- must not respawn"))

    assert mod.ndjson_call("/fake/sock", "/fake/target.php")["id"] == 2


def test_pid_path_derivation_agrees_with_socket_pid_paths(tmp_path, monkeypatch):
    """The derivation must not drift from the resolver it replaced.

    `_spawn.pid_path` names the pidfile by string surgery on the socket
    path; `_paths.socket_pid_paths` computes both from `(cwd, name)`. They
    have to agree, or the probe reads a file the daemon never writes -- and
    `daemon_pid` on a missing pidfile returns 0, so the disagreement would
    render as "no daemon running" rather than as an error. Exactly the
    absence-produced-by-the-tool shape this repo keeps filing.

    Run where ownership IS checkable, since it needs the real resolver.
    """
    if not hasattr(os, "geteuid"):
        pytest.skip("needs a platform where runtime_dir() resolves at all")
    sys.path.insert(0, str(REPO / "presets" / "mcp"))
    import _paths
    import _spawn

    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    sock, pid = _paths.socket_pid_paths("/some/project", "phpstan-warm")
    assert _spawn.pid_path(sock) == pid
