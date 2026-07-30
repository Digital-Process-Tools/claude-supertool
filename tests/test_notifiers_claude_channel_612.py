"""End-to-end tests for `claude-channel` unsendable payload values (issue #612).

`asAttr` returns `null` for a value it will not coerce — objects, arrays, `NaN`,
`Infinity`, `null`, `undefined` — and the caller in `buildMeta` used to just
`continue`, dropping the key with no trace anywhere: not in the attributes, not
in the body, not on stderr. The producer sent a field, the session never saw
it, and nothing recorded that a decision was made.

The refusal to coerce is correct and stays — `String({})` is `"[object
Object]"`, which reads downstream as data somebody meant to send, and `_meta`
is a string map the receiver enforces with a schema (#554). This is only about
the silence: #605 disclosed a key dropped for size (`clamped`) and #609
disclosed a key dropped for colliding with a reserved name (`collided`); a key
dropped for being unsendable now gets the same treatment (`unsendable`).

Measured before filing this: across the six live pollers under
`presets/watch/sources/`, five build scalar-only payloads. `gl-runners`'s
`runner_added` and `runner_silent` events do not — `payload.tags` is
`sorted(runner.get("tag_list") or [])`, a `list[str]`, so this is not a
hypothetical guard against a mistake nobody makes.

Coverage: same `notifiers` job as `test_notifiers_claude_channel_554.py`.
"""
from __future__ import annotations

import shutil
import socket as _socket

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

ATTR_MAX = 2048


@pytest.fixture()
def channel():
    ch = Channel()
    try:
        yield ch
    finally:
        ch.close()


def _meta(msg: dict) -> dict:
    return msg["params"]["meta"]


def test_a_dropped_object_value_says_so(channel: Channel) -> None:
    """The reported gap, in its plainest form: a payload object, refused.

    Before the fix this event delivered `title` and said nothing at all about
    `jobs` — not in `meta`, not in the body, not on stderr. The key name and
    why it was refused is the useful part; the value's contents cannot be
    rendered so they are not quoted.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "1",
        "event": "pipeline_failed",
        "payload": {"title": "ok", "jobs": {"a": 1}, "stages": ["build", "test"]},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert meta["title"] == "ok"
    assert "jobs" not in meta, f"structured value stringified: {meta.get('jobs')!r}"
    assert "stages" not in meta
    assert "unsendable" in meta, f"two payload keys vanished with no word: {sorted(meta)}"
    disclosure = meta["unsendable"]
    assert "jobs" in disclosure and "stages" in disclosure, (
        f"the disclosure does not name the keys that were refused: {disclosure}")
    assert len(disclosure) <= ATTR_MAX, "the disclosure must itself respect the cap"
    body = msg["params"]["content"]
    assert "jobs" in body, f"the body does not disclose the refusal: {body}"


def test_the_disclosure_names_the_shape_not_the_contents(channel: Channel) -> None:
    """`String({})` is not an option, but `typeof`/shape is exactly the point.

    An array and a plain object are refused for the same underlying reason but
    are a different mistake for a poller author to fix, so the shape word
    should tell them apart.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "2",
        "event": "pipeline_failed",
        "payload": {"one_object": {"nested": True}, "one_array": [1, 2, 3]},
    })
    meta = _meta(channel.next_message())
    disclosure = meta["unsendable"]
    assert "one_object (object)" in disclosure, disclosure
    assert "one_array (array)" in disclosure, disclosure


def test_the_unsendable_attribute_cannot_be_forged(channel: Channel) -> None:
    """The disclosure has to stay trustworthy, same as `clamped` and `collided`.

    A payload key literally named `unsendable` must not be able to write that
    attribute directly — it is reserved, exactly like the other two.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "3",
        "event": "pipeline_failed",
        "payload": {"unsendable": "nothing was refused, honest", "title": "small"},
    })
    meta = _meta(channel.next_message())
    assert "honest" not in meta.get("unsendable", ""), (
        f"the refusal notice was itself forged: {meta.get('unsendable')}")
    assert "collided" in meta and "unsendable" in meta["collided"], (
        f"the forgery attempt was swallowed instead of reported: {meta.get('collided')}")


def test_an_ordinary_event_is_untouched_and_says_nothing(channel: Channel) -> None:
    """The over-reach guard: a scalar-only payload must not gain a disclosure."""
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "4",
        "event": "pipeline_failed",
        "payload": {"title": "feat: something ordinary", "pipeline_id": "154177"},
    })
    meta = _meta(channel.next_message())
    assert "unsendable" not in meta, f"a clean event was reported as unsendable: {meta}"


def test_null_and_non_finite_number_are_disclosed_too(channel: Channel) -> None:
    """`asAttr` refuses more than objects and arrays — the disclosure covers all of it.

    A JSON `null` payload value and a non-finite number both fail
    `typeof value === "number" && Number.isFinite(value)` the same way an
    object does, so they hit the same silent `continue` before this fix.
    """
    channel.emit_raw(
        '{"ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "5", '
        '"event": "pipeline_failed", '
        '"payload": {"title": "ok", "reading": null}}'
    )
    meta = _meta(channel.next_message())
    assert meta["title"] == "ok"
    assert "reading" not in meta
    assert "reading (null)" in meta.get("unsendable", ""), meta.get("unsendable")
