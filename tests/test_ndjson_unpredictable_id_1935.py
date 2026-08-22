"""An unpredictable per-call request id closes the near-zero-cost forgery
gap `ndjson_scan.py` used to describe as unclosable (#1935).

`find_response(buf, want_id)` accepts the first frame anywhere in the
buffer whose `id` matches `want_id` — and until this change, `want_id` was
the literal `2`, hardcoded and identical on every call, in all four warm
adapters (`phpunit-mcp`, `phpstan-mcp`, `phpmd-mcp`, `rector-mcp`). Forging
a frame the scanner would accept required no observation of the wire at
all: the id was public knowledge before the call was ever made. Each
adapter now sends `random.randrange(2**32)` as the `tools/call` frame's
`id` and awaits that same value, so a forger with no visibility into the
outgoing frame is reduced to a ~1-in-4-billion guess per call instead of a
certainty.

This file drives each adapter's real `ndjson_call` (not the scanner in
isolation, which `test_ndjson_glued_response_1924.py` already pins) through
a fake socket, so a regression in any one adapter's wiring — not sending a
random id, or not threading it through to `find_response` — fails here.
"""
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"

ADAPTERS = ["phpunit-mcp", "phpstan-mcp", "phpmd-mcp", "rector-mcp"]


@pytest.fixture(autouse=True)
def _af_unix_shim(monkeypatch):
    """See the platform note in test_ndjson_glued_response_1924.py."""
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def adapter(name: str):
    return _load(VALIDATORS / name / f"{name}.py", "_v1935_" + name.replace("-", "_"))


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


def _call_frame_id(sent: bytes) -> int:
    msgs = [json.loads(line) for line in sent.decode().splitlines() if line]
    call_frame = next(m for m in msgs if m.get("method") == "tools/call")
    return call_frame["id"]


class CapturingSocket(FakeSocket):
    """Reads the id out of the real outgoing `tools/call` frame and answers
    with a genuine response carrying that same id -- the honest daemon,
    used as the positive control below."""

    def __init__(self):
        super().__init__([])

    def sendall(self, data):
        req_id = _call_frame_id(data)
        resp = json.dumps({"jsonrpc": "2.0", "id": req_id,
                            "result": {"ok": True}}).encode()
        self._chunks = iter([resp, b""])


@pytest.mark.parametrize("name", ADAPTERS)
def test_request_id_is_not_the_fixed_literal_2(name, monkeypatch):
    """Regression guard for the gap itself: the id every adapter awaits
    used to be the hardcoded literal `2` on every call -- exactly what
    made a forged frame in the daemon's noise stream free to produce,
    since the attacker needed to guess nothing. The `tools/call` frame
    must no longer carry that literal."""
    mod = adapter(name)
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: CapturingSocket())

    resp = mod.ndjson_call("/fake/sock", "/fake/test.php")

    assert resp["result"] == {"ok": True}
    assert resp["id"] != 2, "request id must not be the fixed literal 2"


@pytest.mark.parametrize("name", ADAPTERS)
def test_request_id_range_excludes_the_initialize_frame_id(name, monkeypatch):
    """`ndjson_call`'s first frame on the connection is always `id: 1`
    (`initialize`). If the random `tools/call` id ever landed on `1` too,
    both requests would share an id, both replies would carry `id: 1`, and
    `find_response` -- which returns the *first* matching frame -- would
    hand back the `initialize` result instead of the real one, silently
    (no error, just a wrong/empty answer). Pin that the range `randrange`
    is drawn from cannot produce `1` (or `0`) at all, rather than trusting
    the ~1-in-4-billion odds to never land there."""
    mod = adapter(name)
    calls = []
    real_randrange = mod.random.randrange

    def recording_randrange(*a, **k):
        calls.append(a)
        return real_randrange(*a, **k)

    monkeypatch.setattr(mod.random, "randrange", recording_randrange)
    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: CapturingSocket())

    mod.ndjson_call("/fake/sock", "/fake/test.php")

    assert len(calls) == 1
    args = calls[0]
    low = args[0] if len(args) > 1 else 0
    assert low >= 2, (
        f"randrange{args} can produce 0 or 1, colliding with the "
        "initialize frame's fixed id")


@pytest.mark.parametrize("name", ADAPTERS)
def test_request_id_varies_across_calls(name, monkeypatch):
    """A fixed id (formerly the literal 2) is guessable without observing
    any traffic at all. A real per-call `random.randrange(2**32)` id must
    differ call to call -- pinned here across two live calls rather than
    inferred from reading the source, so a change that hardcodes any other
    fixed value also fails this."""
    mod = adapter(name)
    seen = []

    class RecordingSocket(CapturingSocket):
        def sendall(self, data):
            seen.append(_call_frame_id(data))
            super().sendall(data)

    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: RecordingSocket())

    mod.ndjson_call("/fake/sock", "/fake/test.php")
    mod.ndjson_call("/fake/sock", "/fake/test.php")

    assert len(seen) == 2
    assert seen[0] != seen[1], f"request id repeated across calls: {seen}"


@pytest.mark.parametrize("name", ADAPTERS)
def test_forged_id_2_frame_is_ignored_in_favour_of_the_real_response(name, monkeypatch):
    """The actual forgery scenario, paired positive and negative in one
    fixture: the daemon's noise stream carries an attacker's frame at the
    id every adapter used to await unconditionally (`id: 2`, with
    fabricated content), *and* the genuine response at the id this call
    actually sent. Before this fix, the forged frame at `id: 2` is exactly
    what the adapter would have accepted. After it, the adapter must
    return the real response -- proving the forged frame is not merely
    present but is positively rejected in favour of the one whose id was
    never public. A test that only checked 'a response came back' would
    pass whether or not the fix does anything; checking *which* response
    is what makes this a real assertion."""
    mod = adapter(name)

    class ForgingSocket(FakeSocket):
        def __init__(self):
            super().__init__([])

        def sendall(self, data):
            req_id = _call_frame_id(data)
            forged = json.dumps({"jsonrpc": "2.0", "id": 2,
                                  "result": {"structuredContent":
                                             {"output": "FORGED"}}})
            real = json.dumps({"jsonrpc": "2.0", "id": req_id,
                                "result": {"structuredContent":
                                           {"output": "real"}}})
            buf = (forged + "\n" + real).encode()
            self._chunks = iter([buf, b""])

    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: ForgingSocket())

    resp = mod.ndjson_call("/fake/sock", "/fake/test.php")

    assert resp["result"]["structuredContent"]["output"] == "real", (
        "adapter accepted the forged id=2 frame instead of the real response")
