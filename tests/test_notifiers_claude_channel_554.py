"""End-to-end tests for the `claude-channel` notifier (issue #554).

One malformed event on the watch socket used to put an invalid value on the
JSON-RPC wire: `params.meta.ts` left the server as a JSON *number* because the
dispatch guard never checked `ts` and `buildMeta` assigned it verbatim. The
receiver (Claude Code) validates `_meta` values as strings, threw a ZodError
inside its notification handler, and dropped the STDIO connection.

The throw is on the *receiving* side — `channel.ts` itself never failed, never
logged, and stayed alive. So the contract these tests pin is an outbound one:

    every value in `params.meta` that leaves this server is a JSON string,
    or the event is dropped with a line on stderr — never emitted malformed.

Coverage: the `notifiers` job in `.github/workflows/tests.yml` installs bun and
the channel's `node_modules` and runs this file for real on ubuntu and macOS
(#557). It sets `SUPERTOOL_REQUIRE_JS=1`, which turns a missing prerequisite
into a collection error instead of a skip — so this file cannot go back to being
counted green without running. The twelve-leg pytest matrix still installs no JS
runtime, so it still skips there, with the reason printed.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket as _socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _toolchain_gate import js_promised, require_or_skip

REPO = Path(__file__).resolve().parents[1]
CHANNEL_TS = REPO / "notifiers" / "claude-channel" / "channel.ts"
NODE_MODULES = REPO / "notifiers" / "claude-channel" / "node_modules"

pytestmark = [
    require_or_skip(
        hasattr(_socket, "AF_UNIX"),
        "claude-channel binds an AF_UNIX socket — not available on this platform",
        promised=js_promised(),
    ),
    require_or_skip(
        shutil.which("bun") is not None,
        "claude-channel runs under bun; no bun on PATH",
        promised=js_promised(),
    ),
    require_or_skip(
        NODE_MODULES.exists(),
        "channel deps not installed — run notifiers/claude-channel/install.sh",
        promised=js_promised(),
    ),
]

CHANNEL_METHOD = "notifications/claude/channel"


class Channel:
    """A live `channel.ts` process plus a minimal MCP client over its stdio."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        # `env` overrides the server's own tunables (the `capFromEnv` caps).
        # Tests that assert on a *default* keep it None and get the shipped
        # numbers; tests that assert on a *mechanism* pin small ones, so the
        # fixture does not have to push 65 KB through a socket to reach a
        # threshold. The socket path is set last and is never overridable —
        # a test that redirected it would silently share a real one.
        # macOS caps AF_UNIX paths at ~104 bytes; keep it under /tmp, not tmp_path.
        self._dir = tempfile.mkdtemp(prefix="st554-", dir="/tmp")
        self.sock_path = os.path.join(self._dir, "w.sock")
        self.proc = subprocess.Popen(
            ["bun", str(CHANNEL_TS)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(CHANNEL_TS.parent),
            env={**os.environ, **(env or {}), "SUPERTOOL_WATCH_SOCK": self.sock_path},
            text=True,
            bufsize=1,
        )
        self.messages: "queue.Queue[dict]" = queue.Queue()
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._handshake()
        self._await_socket()

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    def _pump_stderr(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self.stderr_lines.append(line.rstrip())

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]

    def _handshake(self) -> None:
        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"experimental": {"claude/channel": {}}},
                "clientInfo": {"name": "test-554", "version": "0"},
            },
        })
        result = self.next_message(timeout=15.0, method=None)
        assert "result" in result, f"initialize failed: {result}"
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _await_socket(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.sock_path):
                return
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"channel.ts exited {self.proc.returncode}: {self.stderr_lines}")
            time.sleep(0.02)
        raise AssertionError(f"socket never appeared at {self.sock_path}")

    def emit(self, event: dict) -> None:
        """Write one NDJSON line to the watch socket, exactly as a poller does."""
        self.emit_raw(json.dumps(event))

    def emit_raw(self, line: str) -> None:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(self.sock_path)
        s.sendall((line + "\n").encode("utf-8"))
        s.close()

    def next_message(self, timeout: float = 5.0, method: str | None = CHANNEL_METHOD) -> dict:
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError(
                    f"no message (method={method}) within {timeout}s; "
                    f"stderr={self.stderr_lines}")
            try:
                msg = self.messages.get(timeout=remaining)
            except queue.Empty:
                continue
            if method is None or msg.get("method") == method:
                return msg

    def expect_no_message(self, timeout: float = 1.5) -> None:
        try:
            msg = self.next_message(timeout=timeout)
        except AssertionError:
            return
        raise AssertionError(f"expected no notification, got {msg}")

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)
        shutil.rmtree(self._dir, ignore_errors=True)


@pytest.fixture()
def channel():
    ch = Channel()
    try:
        yield ch
    finally:
        ch.close()


def _meta(msg: dict) -> dict:
    return msg["params"]["meta"]


def test_float_ts_is_coerced_to_a_string(channel: Channel) -> None:
    """The issue's exact repro: `"ts": time.time()`.

    Before the fix the server put a JSON number on the wire, which is what the
    receiver rejected. The event must be delivered, with `ts` a string.
    """
    stamp = 1785362036.766711
    channel.emit({
        "ts": stamp, "source": "gitlab-mr", "id": "99999",
        "event": "pipeline_succeeded",
        "payload": {"title": "probe", "url": "https://example/probe"},
    })
    meta = _meta(channel.next_message())
    assert isinstance(meta["ts"], str), f"ts left the server as {type(meta['ts'])}: {meta['ts']!r}"
    assert meta["ts"] == str(stamp)
    assert meta["watcher_source"] == "gitlab-mr"


def test_every_meta_value_on_the_wire_is_a_string(channel: Channel) -> None:
    """The invariant, checked over a payload of mixed scalar types.

    `_meta` is a string map on the receiving end; nothing else may be emitted.
    """
    channel.emit({
        "ts": 1785362036, "source": "gitlab-mr", "id": 21803,
        "event": "pipeline_failed", "first_tick": True,
        "payload": {"pipeline_id": 139928, "retried": False, "coverage": 91.5,
                    "title": "feat: do the thing"},
    })
    meta = _meta(channel.next_message())
    non_strings = {k: v for k, v in meta.items() if not isinstance(v, str)}
    assert not non_strings, f"non-string _meta values on the wire: {non_strings}"
    assert meta["id"] == "21803"
    assert meta["pipeline_id"] == "139928"
    assert meta["retried"] == "false"
    assert meta["first_tick"] == "true"


def test_numeric_id_is_delivered_not_silently_dropped(channel: Channel) -> None:
    """A poller that forwards GitLab's integer `iid` must still be heard.

    The old guard required `typeof id === "string"`, so a numeric id was
    dropped without a trace — the same invisible delivery gap as the crash,
    minus the crash.
    """
    channel.emit({
        "ts": "2026-07-29T21:45:00Z", "source": "gitlab-mr", "id": 12345,
        "event": "pipeline_failed", "payload": {"title": "numeric id"},
    })
    msg = channel.next_message()
    assert _meta(msg)["id"] == "12345"
    assert msg["params"]["content"].startswith("gitlab-mr 12345: pipeline_failed")


def test_non_scalar_payload_value_is_dropped_not_stringified(channel: Channel) -> None:
    """`String({})` is `"[object Object]"` — garbage delivered as if it were data.

    A structured payload value has no honest string form, so the attribute is
    omitted; the event itself still gets through.
    """
    channel.emit({
        "ts": "2026-07-29T21:45:00Z", "source": "gitlab-mr", "id": "1",
        "event": "pipeline_failed",
        "payload": {"title": "ok", "jobs": {"a": 1}, "stages": ["build", "test"]},
    })
    meta = _meta(channel.next_message())
    assert meta["title"] == "ok"
    assert "jobs" not in meta, f"structured value stringified: {meta.get('jobs')!r}"
    assert "stages" not in meta


def test_malformed_events_are_dropped_loudly_and_the_server_survives(channel: Channel) -> None:
    """A bad line must cost one event, not the connection or the process.

    Each rejected line gets a stderr note — the old code dropped them in
    silence, which is what made the delivery gap unobservable from inside a
    session.
    """
    channel.emit_raw("{not json at all")
    channel.emit({"ts": "2026-07-29T21:45:00Z", "source": {"nested": "object"},
                  "id": "1", "event": "x", "payload": {}})
    channel.emit({"ts": "2026-07-29T21:45:00Z", "id": "1", "payload": {}})
    channel.expect_no_message()

    channel.emit({
        "ts": "2026-07-29T21:46:00Z", "source": "gitlab-mr", "id": "77",
        "event": "pipeline_succeeded", "payload": {"title": "after"},
    })
    assert _meta(channel.next_message())["id"] == "77"
    assert channel.proc.poll() is None, "channel.ts died on a malformed event"

    time.sleep(0.2)
    dropped = [ln for ln in channel.stderr_lines if "dropped" in ln]
    assert len(dropped) >= 3, f"malformed lines dropped silently; stderr={channel.stderr_lines}"


def test_multiple_events_in_one_write_are_framed_independently(channel: Channel) -> None:
    """NDJSON framing: one bad line in a batch must not eat its neighbours."""
    good = {"ts": "2026-07-29T21:45:00Z", "source": "gitlab-mr", "id": "A",
            "event": "e", "payload": {}}
    other = dict(good, id="B")
    channel.emit_raw(json.dumps(good) + "\n" + "{broken" + "\n" + json.dumps(other))
    ids = {_meta(channel.next_message())["id"] for _ in range(2)}
    assert ids == {"A", "B"}
