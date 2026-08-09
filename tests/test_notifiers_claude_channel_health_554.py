"""The consumer publishes its own counters (#554, request 3).

`presets/watch/channel.py` can tell whether *anything* is listening, and that is
the whole of what a producer can know: `mcp.notification()` has no response and
`channel.ts` writes nothing back to the producer connection, so no ack exists to
read. The strongest positive fact in the system is therefore the consumer's own
count of what it forwarded — and a fact nobody publishes is a fact nobody has.

These tests drive the real server and assert on the file it writes beside its
socket. They pin the properties that make the file evidence rather than
decoration:

- it exists from the moment the socket is bound, so "no counters" means "no
  claude-channel", not "started too recently to have any";
- `forwarded` advances only for events that actually reached the transport, and
  `dropped` for the ones refused, so the two are not one number wearing a hat;
- `updated` refreshes on a heartbeat with no traffic at all, which is what lets
  `channel:health` tell an idle consumer from a wedged one;
- the word is `forwarded`, never `delivered`.

Coverage matches the rest of the notifier suite: the `notifiers` job in
`.github/workflows/tests.yml` runs this for real on ubuntu and macOS with bun
installed; the twelve-leg pytest matrix skips it with the reason printed.
"""
from __future__ import annotations

import json
import shutil
import socket as _socket
import time
from pathlib import Path

import pytest

from _toolchain_gate import js_promised, require_or_skip
from test_notifiers_claude_channel_554 import Channel

REPO = Path(__file__).resolve().parents[1]
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

WELL_FORMED = {
    "ts": "2026-08-09T10:00:00Z", "source": "gitlab-mr", "id": "33173",
    "event": "pipeline_failed", "payload": {"title": "t"},
}


@pytest.fixture()
def channel():
    ch = Channel()
    try:
        yield ch
    finally:
        ch.close()


def _health_path(ch: Channel) -> Path:
    return Path(ch.sock_path + ".health.json")


def _await_health(ch: Channel, predicate, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = json.loads(_health_path(ch).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"health file never satisfied the predicate; last={last}")


def test_the_health_file_exists_as_soon_as_the_socket_is_bound(channel: Channel) -> None:
    """Zero counters published beats no file. Absence has to mean "not
    claude-channel", or `channel:health` cannot distinguish a fresh consumer
    from one that publishes nothing."""
    record = _await_health(channel, lambda r: "forwarded" in r)
    assert record["forwarded"] == 0
    assert record["dropped"] == 0
    assert record["pid"] == channel.proc.pid
    assert record["sock_path"] == channel.sock_path
    assert record["started"]


def test_forwarded_advances_only_for_events_that_reach_the_transport(channel: Channel) -> None:
    channel.emit(WELL_FORMED)
    channel.next_message()
    record = _await_health(channel, lambda r: r.get("forwarded") == 1)
    assert record["dropped"] == 0
    assert record["lines_read"] == 1
    assert record["last_forwarded"]


def test_a_dropped_event_counts_as_dropped_and_not_as_forwarded(channel: Channel) -> None:
    """The distinction the whole file rests on. A consumer that counted every
    line it read as forwarded would republish the defect: a number that advances
    whether or not anything was delivered."""
    channel.emit_raw("{not json")
    record = _await_health(channel, lambda r: r.get("dropped") == 1)
    assert record["forwarded"] == 0
    assert record["last_forwarded"] is None


def test_counters_refresh_on_a_heartbeat_with_no_traffic(channel: Channel) -> None:
    """An idle radar and a wedged one look identical without this. The stamp has
    to advance on a timer, not only on an event."""
    first = _await_health(channel, lambda r: "updated" in r)
    second = _await_health(
        channel, lambda r: r.get("updated") != first["updated"], timeout=30.0,
    )
    assert second["forwarded"] == 0, "a heartbeat must not invent traffic"


def test_the_file_never_uses_the_word_delivered(channel: Channel) -> None:
    """`delivered` would be a claim about a Claude session, which this process
    cannot observe. Pinned on the wire rather than in prose, because the prose
    is what drifts."""
    raw = _await_health(channel, lambda r: "forwarded" in r)
    assert "delivered" not in json.dumps(raw)


def test_a_stopped_consumer_stops_claiming_to_be_one(channel: Channel) -> None:
    """On a clean shutdown the file must not be left behind saying `forwarded:
    N` under a pid that is gone — a frozen counter reads as health forever."""
    _await_health(channel, lambda r: "forwarded" in r)
    channel.proc.terminate()
    channel.proc.wait(timeout=10)
    deadline = time.time() + 10
    while time.time() < deadline:
        if not _health_path(channel).exists():
            return
        time.sleep(0.05)
    raise AssertionError(
        f"health file survived a clean shutdown: "
        f"{_health_path(channel).read_text(encoding='utf-8')}"
    )
