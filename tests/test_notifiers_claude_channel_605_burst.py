"""End-to-end tests for the `claude-channel` burst budget (issue #605, remainder).

#608 shipped the two axes that bound *one* event: a per-attribute cap and a
per-event cap, both withhold-and-disclose. It named the third axis and
deliberately left it open, in the issue and in both READMEs:

    Forty 40 KB events cost the window what one 1.6 MB event does. [...] forty
    legal 8 KB events still cost 320 KB, and that stays open.

That is this file. The reason #608 declined it was a real one and it constrains
the fix rather than excusing it: dropping event #41 because #1–40 were chatty
refuses an event on grounds unrelated to its own content, and a limiter that
silently eats a `pipeline_failed` is a worse radar than a chatty one.

So the contract here is deliberately *not* "events stop arriving". It has three
halves, and a test that checks only the first would pass on the silent limiter
that #608 was right to refuse:

    a burst is bounded, AND every event still arrives with its routing intact
    — `watcher_source`/`id`/`event` is the whole product of this bridge — AND
    an event that was reduced, or a gap where events were suppressed, says so
    in its own attributes and its own body.

Three shapes, failing differently:

  * forty legal 8 KB events   — 320 KB measured on 235b377, every attribute
                                under every cap #608 shipped
  * a reduced event           — routing survives, payload is withheld whole,
                                `burst` names the budget that withheld it
  * a hard-limit gap          — events suppressed rather than reduced, counted,
                                and the count disclosed on the next delivery,
                                so the absence is visible from inside a session

Coverage: same `notifiers` job as `test_notifiers_claude_channel_554.py`.
"""
from __future__ import annotations

import json
import os
import shutil
import socket as _socket
import subprocess
import time

import pytest

from _toolchain_gate import js_promised, require_or_skip
from test_notifiers_claude_channel_554 import CHANNEL_TS, NODE_MODULES, Channel  # noqa: F401

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

# Mirrors channel.ts. Duplicated rather than imported because these tests assert
# on what crosses the wire, not on what the module believes about itself.
WINDOW_MAX = 65536
EXIT_BAD_CAP = 4

# Four attributes of 1,900 chars: every one under the 2,048 per-attribute cap,
# the event under the 8,192 per-event cap. Nothing #608 shipped touches this
# event — which is the point. It is legal, and forty of them are 320 KB.
LEGAL_FAT = {f"field_{n}": "x" * 1900 for n in range(4)}


def _meta(msg: dict) -> dict:
    return msg["params"]["meta"]


def _wire_size(msg: dict) -> int:
    return len(json.dumps(msg))


def _drain(ch: Channel, expected: int, timeout: float = 20.0) -> list[dict]:
    """Every notification the server sends, up to `expected`, then silence."""
    out: list[dict] = []
    deadline = time.time() + timeout
    while len(out) < expected and time.time() < deadline:
        try:
            out.append(ch.next_message(timeout=1.5))
        except AssertionError:
            break
    return out


def test_a_burst_of_legal_events_is_bounded_and_loses_no_routing() -> None:
    """Forty events every cap #608 shipped considers perfect. 320 KB measured.

    The size assertion alone is not the contract and would be satisfied by a
    limiter that simply stopped delivering — the thing #608 refused to build.
    So the id set is asserted too: all forty events still arrive, because what
    an event is *for* is `gitlab-mr 33173: pipeline_failed`, and that is sixty
    bytes no burst can justify losing.
    """
    ch = Channel()
    try:
        for n in range(40):
            ch.emit({
                "ts": "2026-08-05T00:00:00Z", "source": "gitlab-mr", "id": str(n),
                "event": "pipeline_failed", "payload": dict(LEGAL_FAT),
            })
        msgs = _drain(ch, expected=40)
        total = sum(_wire_size(m) for m in msgs)
        ids = {_meta(m)["id"] for m in msgs}

        assert ids == {str(n) for n in range(40)}, (
            f"routing lost for {40 - len(ids)} events — a burst limiter that "
            "drops events is worse than the burst"
        )
        assert total < 3 * WINDOW_MAX, (
            f"forty legal events cost {total} bytes of the window"
        )
        assert any("burst" in _meta(m) for m in msgs), (
            "the burst was bounded without a single event saying so"
        )
    finally:
        ch.close()


def test_a_reduced_event_keeps_routing_withholds_whole_and_names_the_budget() -> None:
    """The disclosure half, asserted apart from the size half on purpose.

    A cap that reduces an event without saying so satisfies every byte
    assertion above and is a worse bug than the one being fixed: it converts
    "the radar is over budget" into "that MR had no title".
    """
    ch = Channel(env={
        "SUPERTOOL_CHANNEL_WINDOW_SECS": "60",
        "SUPERTOOL_CHANNEL_WINDOW_MAX": "200",
        "SUPERTOOL_CHANNEL_WINDOW_HARD": "100000",
    })
    try:
        for n in range(2):
            ch.emit({
                "ts": "2026-08-05T00:00:00Z", "source": "gitlab-mr", "id": str(n),
                "event": "pipeline_failed",
                "payload": {"title": "T" * 1500, "url": "https://example/mr/" + str(n)},
            })
        msgs = _drain(ch, expected=2)
        assert len(msgs) == 2, f"an event went missing: {msgs}"
        second = msgs[1]
        meta = _meta(second)
        body = second["params"]["content"]

        assert meta["watcher_source"] == "gitlab-mr"
        assert meta["id"] == "1"
        assert meta["event"] == "pipeline_failed"
        assert "title" not in meta, "payload survived a reduced event"
        assert "url" not in meta
        assert "TTTT" not in json.dumps(second), (
            "a truncated prefix reached the session — withhold whole, never clip"
        )
        assert "burst" in meta, "a reduced event that does not say it was reduced"
        assert "200" in meta["burst"], f"the budget is not named: {meta['burst']}"
        assert "[claude-channel]" in body and "burst" in body.lower(), (
            f"the body does not disclose the reduction: {body!r}"
        )
    finally:
        ch.close()


def test_events_past_the_hard_limit_are_counted_and_the_gap_is_disclosed() -> None:
    """The one place events really are refused, and the absence made visible.

    Routing-only is ~120 chars, so reduction alone leaves a spinning producer
    ~50x cheaper and still unbounded — the DoS surface narrowed, not closed.
    Past the hard limit events are suppressed, which is a loss; what makes it
    honest rather than the #554 delivery gap is that the count is carried on
    the next event that gets through.
    """
    ch = Channel(env={
        "SUPERTOOL_CHANNEL_WINDOW_SECS": "1",
        "SUPERTOOL_CHANNEL_WINDOW_MAX": "200",
        "SUPERTOOL_CHANNEL_WINDOW_HARD": "800",
    })
    try:
        for n in range(6):
            ch.emit({
                "ts": "2026-08-05T00:00:00Z", "source": "gitlab-mr", "id": str(n),
                "event": "pipeline_failed", "payload": {"title": "T" * 1500},
            })
        delivered = _drain(ch, expected=6, timeout=6.0)
        missing = 6 - len(delivered)
        assert missing > 0, "the hard limit never engaged; tighten the fixture"
        assert any("burst hard limit" in ln for ln in ch.stderr_lines), (
            f"suppression was silent on stderr: {ch.stderr_lines[-5:]}"
        )

        time.sleep(1.6)  # let the 1s window drain
        ch.emit({
            "ts": "2026-08-05T00:00:01Z", "source": "gitlab-mr", "id": "later",
            "event": "pipeline_succeeded", "payload": {"title": "ok"},
        })
        after = ch.next_message(timeout=5.0)
        meta = _meta(after)
        assert meta["id"] == "later", "the window never drained"
        assert "suppressed" in meta, (
            "events vanished and the next delivery said nothing — that is #554"
        )
        assert str(missing) in meta["suppressed"], (
            f"{missing} events were suppressed; disclosure says {meta['suppressed']!r}"
        )
        assert "suppressed" in after["params"]["content"], "the body does not carry the gap"
    finally:
        ch.close()


def test_normal_traffic_is_delivered_whole_and_says_nothing() -> None:
    """The over-reach guard. Passes before and after, deliberately.

    A patch that reduced everything, or annotated every event, would satisfy
    every assertion above and fail here instead of reading as progress. Ten
    live watchers produced a largest payload of 488 chars; this is that shape.
    """
    ch = Channel()
    try:
        for n in range(5):
            ch.emit({
                "ts": "2026-08-05T00:00:00Z", "source": "gitlab-mr", "id": str(n),
                "event": "pipeline_failed",
                "payload": {
                    "title": "feat: a fairly wordy merge request title " + "x" * 200,
                    "url": f"https://example/mr/{n}",
                },
            })
        msgs = _drain(ch, expected=5)
        assert len(msgs) == 5
        for m in msgs:
            meta = _meta(m)
            assert "burst" not in meta, f"a 488-char event was reduced: {meta}"
            assert "suppressed" not in meta
            assert meta["title"].startswith("feat: a fairly wordy")
            assert meta["url"].startswith("https://example/mr/")
    finally:
        ch.close()


def test_a_hard_limit_below_the_soft_limit_refuses_to_start(tmp_path) -> None:
    """Two thresholds that cannot both hold is a cap that is not in force.

    With HARD <= MAX the reduction stage is unreachable and every over-budget
    event is suppressed instead — a strictly louder failure than the operator
    configured, arrived at silently. `capFromEnv` already refuses an unreadable
    override for the same reason; this is the relationship version.
    """
    proc = subprocess.run(
        ["bun", str(CHANNEL_TS)],
        cwd=str(CHANNEL_TS.parent),
        env={
            **os.environ,
            "SUPERTOOL_WATCH_SOCK": str(tmp_path / "w.sock"),
            "SUPERTOOL_CHANNEL_WINDOW_MAX": "5000",
            "SUPERTOOL_CHANNEL_WINDOW_HARD": "1000",
        },
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == EXIT_BAD_CAP, (
        f"started with an unreachable reduction stage (exit {proc.returncode})"
    )
    assert "SUPERTOOL_CHANNEL_WINDOW_HARD" in proc.stderr, (
        f"refused without naming the variable: {proc.stderr!r}"
    )
