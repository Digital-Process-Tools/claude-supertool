"""End-to-end tests for the `claude-channel` event size cap (issue #605).

#554 made the transport survive a malformed event. This is the event that is
*well-formed* and costs the session the thing the transport exists to protect:
measured on `4da713f`, one event carrying two 400 KB strings left the server as
`title` 400,000 chars, `url` 400,000, `content` 800,034 — 1,600,261 bytes on the
wire, roughly twice an entire 200K-token context window.

The contract these tests pin is a size one, and it has two halves that must hold
together — a cap that shrinks an event without saying so would pass the first
half and be a worse bug than the one it fixed:

    no event leaves this server larger than the cap, AND every event that was
    reduced says so in its own body and attributes — never a silently shortened
    value, never a truncated prefix that reads as a complete one.

Three shapes are covered because they fail differently and only one of them is
in the issue's own repro:

  * one event, two enormous attributes   — the reported 1.6 MB
  * one event, 2,000 small attributes    — 425 KB measured, and a per-attribute
                                           cap alone does not touch it
  * bytes with no newline, ever          — never becomes an event at all; 50 MB
                                           pushed a real server from 74 MB to
                                           770 MB RSS with nothing logged

Coverage: same `notifiers` job as `test_notifiers_claude_channel_554.py`.
"""
from __future__ import annotations

import json
import socket as _socket
import shutil
import time

import pytest

from _toolchain_gate import js_promised, require_or_skip
from test_notifiers_claude_channel_554 import NODE_MODULES, Channel  # noqa: F401

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

# Mirrors the constants in channel.ts. Duplicated rather than imported because
# these tests assert on what crosses the wire, not on what the module believes.
ATTR_MAX = 2048
EVENT_MAX = 8192

# The largest payload observed across ten live watchers on this machine was 488
# characters, its longest single value 117. A "normal" event here is deliberately
# fatter than anything real, so a false clamp would show up as a failure.
NORMAL_TITLE = "feat: a fairly wordy merge request title " + "x" * 200


@pytest.fixture()
def channel():
    ch = Channel()
    try:
        yield ch
    finally:
        ch.close()


def _meta(msg: dict) -> dict:
    return msg["params"]["meta"]


def _wire_size(msg: dict) -> int:
    return len(json.dumps(msg))


def test_the_reported_event_is_bounded_on_the_wire(channel: Channel) -> None:
    """The issue's exact repro: two 400 KB strings, measured at 1,600,261 bytes.

    The assertion is on the whole notification, not on one attribute, because
    the cost to the window is the whole notification.
    """
    big = "A" * 400_000
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "99999",
        "event": "pipeline_failed", "payload": {"title": big, "url": big},
    })
    msg = channel.next_message()
    size = _wire_size(msg)
    assert size < 4 * EVENT_MAX, f"event left the server at {size} bytes"
    assert _meta(msg)["watcher_source"] == "gitlab-mr", "routing must survive the clamp"
    assert _meta(msg)["id"] == "99999"
    assert _meta(msg)["event"] == "pipeline_failed"


def test_an_oversized_attribute_is_withheld_whole_not_truncated(channel: Channel) -> None:
    """No shortened value may reach the session — that is the trap, not the fix.

    A 400 KB title has no honest short form, exactly as a structured value has
    no honest string form (`asAttr`). It is omitted, not clipped: nothing that
    arrives can be misread as the complete title, because no prefix arrives.
    """
    big = "A" * 400_000
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "1",
        "event": "pipeline_failed",
        "payload": {"title": big, "url": "https://example/mr/1", "pipeline_id": "139928"},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert "title" not in meta, f"oversized attribute survived as {len(meta.get('title', ''))} chars"
    assert not meta.get("title", "").startswith("AAAA"), "a truncated prefix was delivered"
    assert "AAAAAAAA" not in msg["params"]["content"], "the clipped value leaked into the body"
    # Attributes that fit are untouched — the clamp is targeted, not a blanket.
    assert meta["url"] == "https://example/mr/1"
    assert meta["pipeline_id"] == "139928"


def test_a_clamped_event_says_so_in_its_attributes_and_its_body(channel: Channel) -> None:
    """Disclosure is the half of the contract that a size assertion cannot see.

    The user must be able to tell a clamped event from a complete one *from the
    event itself*. `clamped` names what went and why; the body carries it too,
    because the body is what Claude reads as the narrative.
    """
    big = "A" * 400_000
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "2",
        "event": "pipeline_failed", "payload": {"title": big, "url": big},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert "clamped" in meta, f"event was reduced in silence: {sorted(meta)}"
    disclosure = meta["clamped"]
    assert "title" in disclosure and "url" in disclosure, f"clamp did not name the attributes: {disclosure}"
    assert "400000" in disclosure, f"clamp did not state the real size: {disclosure}"
    assert len(disclosure) <= ATTR_MAX, "the disclosure must itself respect the cap"
    body = msg["params"]["content"]
    assert "withheld" in body, f"the body does not disclose the clamp: {body}"
    assert "title" in body, f"the body does not name what went: {body}"


def test_many_small_attributes_are_bounded_too(channel: Channel) -> None:
    """2,000 well-formed 200-char attributes measured 425,123 bytes delivered.

    Every value here is far under any per-attribute cap, so this is the shape
    that proves the cap is per *event*. A per-attribute limit alone passes this
    payload through untouched.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "3",
        "event": "pipeline_failed",
        "payload": {f"k{i}": "x" * 200 for i in range(2000)},
    })
    msg = channel.next_message()
    size = _wire_size(msg)
    assert size < 4 * EVENT_MAX, f"2,000 small attributes left the server at {size} bytes"
    assert "clamped" in _meta(msg), "attributes were dropped without a word"


def test_a_normal_event_is_delivered_whole_and_says_nothing(channel: Channel) -> None:
    """The cap must be invisible to every event anyone actually sends.

    A `clamped` attribute on a 500-char event would be noise on the one surface
    that has to stay trustworthy, and would train a reader to ignore it.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "33173",
        "event": "pipeline_failed",
        "payload": {"title": NORMAL_TITLE, "url": "https://gitlab.example.com/x/-/merge_requests/33173",
                    "pipeline_id": "154177", "observed_failed_jobs": "test_unit_1,test_unit_2"},
    })
    meta = _meta(channel.next_message())
    assert "clamped" not in meta, f"a normal event was reported as clamped: {meta.get('clamped')}"
    assert meta["title"] == NORMAL_TITLE, "a normal title was altered"
    assert meta["observed_failed_jobs"] == "test_unit_1,test_unit_2"


def test_an_unroutable_giant_is_dropped_loudly_not_delivered_blank(channel: Channel) -> None:
    """When the routing key itself is the giant, there is no event left to send.

    Withholding `event` would deliver a notification that says nothing about
    what happened. `drop()` is the existing loud path for an event the server
    refuses outright, and this is one.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "4",
        "event": "B" * 400_000, "payload": {"title": "ok"},
    })
    channel.expect_no_message()
    joined = "\n".join(channel.stderr_lines)
    assert "dropped event" in joined, f"refused in silence; stderr={channel.stderr_lines}"


def test_a_stream_that_never_sends_a_newline_is_refused_not_accumulated(channel: Channel) -> None:
    """The line buffer is the same defect one layer earlier, and it is louder.

    Measured on `4da713f`: 50 MB with no newline took a real server from 74 MB
    to 770 MB RSS, super-linearly (1.0s → 13.7s for equal increments, because
    every chunk rescans the whole buffer), delivering nothing and logging
    nothing. A producer that dies mid-line does this by accident.

    Refusing must also *resync*: the bytes after the overflow are the tail of a
    line nobody can parse, so they are discarded to the next newline — and the
    next real event still arrives. A cap that wedged the connection shut would
    trade a memory leak for a delivery gap, which is #554 again.
    """
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.connect(channel.sock_path)
    s.settimeout(20)
    s.sendall(b"C" * 4_000_000)          # 4 MB, no newline
    time.sleep(1.0)
    joined = "\n".join(channel.stderr_lines)
    assert "dropped" in joined, f"unterminated stream absorbed in silence; stderr={channel.stderr_lines}"

    # …and the connection resyncs: a real event on the same socket still lands.
    s.sendall(b"\n" + json.dumps({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "5",
        "event": "pipeline_succeeded", "payload": {"title": "after the flood"},
    }).encode() + b"\n")
    msg = channel.next_message(timeout=10.0)
    assert _meta(msg)["id"] == "5", "the connection did not resync after refusing a giant line"
    s.close()
