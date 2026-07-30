"""End-to-end tests for `claude-channel` routing-key collisions (issue #609).

#605 made an event's *size* honest. This is the event whose size is fine and
whose **identity** is not: `buildMeta` sets the routing fields and then merges
the poller's `payload` over them, guarding only `source`. Measured on `235b377`
against a real server, a well-formed event announced as

    gitlab-mr 33173: pipeline_failed

was delivered as

    not-gitlab 11111: pipeline_succeeded

— every routing field replaced by a payload key of the same name, the body and
the attributes in perfect agreement, nothing on stderr. A red pipeline read as a
green one, which is `docs/validators.md`'s test met literally: someone acting
reasonably on this output concludes the opposite of the truth.

The contract these tests pin has two halves, and a fix that satisfied only the
first would be the trade this repo keeps refusing:

    no payload key may change what an event *is*, AND every payload key that
    lost to a reserved name says so — in the event's own attributes and in its
    own body, naming the key.

Four shapes, because they fail differently:

  * payload `id`/`event`/`ts`/`watcher_source`  — the reported misrouting
  * payload `clamped`                            — forges #605's disclosure on
                                                   an event that lost nothing
  * payload `id` of 400,000 chars                — killed the *whole* event with
                                                   a stderr reason that was
                                                   false ("routing key over 2048
                                                   chars"; the routing id was
                                                   "66666")
  * payload `__proto__`                          — assigning it to a plain
                                                   object is a silent no-op, so
                                                   the key vanished with no word

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

# Mirrors channel.ts. Duplicated rather than imported because these tests assert
# on what crosses the wire, not on what the module believes.
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


def test_a_payload_key_cannot_replace_the_events_routing(channel: Channel) -> None:
    """The reported defect, in its most consequential form.

    Every routing field is attacked at once, including the two `first_tick` and
    `watcher_source` that #608 added — the event `pipeline_failed` must not be
    deliverable as `pipeline_succeeded` by a poller that happens to name a
    payload field `event`.

    Both surfaces are checked. The attributes are what a session routes on; the
    body is what it reads. They agreed with each other before the fix, which is
    exactly why nothing caught this.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "33173",
        "event": "pipeline_failed",
        "payload": {
            "id": "11111",
            "event": "pipeline_succeeded",
            "ts": "1999-01-01T00:00:00Z",
            "watcher_source": "not-gitlab",
            "first_tick": "true",
            "title": "real title",
        },
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert meta["watcher_source"] == "gitlab-mr", f"payload re-aimed the source: {meta}"
    assert meta["id"] == "33173", f"payload re-aimed the id: {meta}"
    assert meta["event"] == "pipeline_failed", f"a red pipeline was delivered as {meta['event']}"
    assert meta["ts"] == "2026-07-30T00:00:00Z", f"payload rewrote the timestamp: {meta}"
    assert "first_tick" not in meta, (
        "the poller did not send first_tick; a payload key claimed it did")
    body = msg["params"]["content"]
    assert body.startswith("gitlab-mr 33173: pipeline_failed"), f"body was re-aimed: {body}"
    # The payload key that did *not* collide is untouched.
    assert meta["title"] == "real title"


def test_a_payload_key_that_lost_says_so(channel: Channel) -> None:
    """Disclosure, asserted separately from the guard.

    A fix that protects the routing value and drops the producer's field in
    silence trades one invisible loss for another. It would satisfy every
    assertion in the test above and fail here, which is the point of splitting
    them: the producer has to be able to find out that their `id` never arrived.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "33173",
        "event": "pipeline_failed",
        "payload": {"id": "11111", "event": "pipeline_succeeded", "title": "real title"},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert "collided" in meta, f"two payload keys were discarded in silence: {sorted(meta)}"
    disclosure = meta["collided"]
    assert "id" in disclosure and "event" in disclosure, (
        f"the disclosure does not name the keys that lost: {disclosure}")
    assert len(disclosure) <= ATTR_MAX, "the disclosure must itself respect the cap"
    body = msg["params"]["content"]
    assert "id" in body and "reserved" in body, (
        f"the body does not disclose the collision: {body}")


def test_the_disclosure_attributes_cannot_be_forged(channel: Channel) -> None:
    """`clamped` is the one attribute that has to stay trustworthy.

    #605's whole contribution is that a reduced event is distinguishable from a
    complete one *from the event itself*. Before this fix a payload key named
    `clamped` set that attribute on an event that lost nothing — the surface
    built to report withholding, writable by the party it reports on.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "44444",
        "event": "pipeline_failed",
        "payload": {
            "clamped": "9 attributes withheld — secret (9 chars); limits: made up",
            "collided": "nothing collided, honest",
            "title": "small",
        },
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert "made up" not in meta.get("clamped", ""), (
        f"a complete event forged a withholding notice: {meta.get('clamped')}")
    assert "honest" not in meta.get("collided", ""), (
        f"the collision notice was itself forged: {meta.get('collided')}")
    assert "clamped" in meta["collided"], (
        f"the forgery attempt was swallowed instead of reported: {meta['collided']}")


def test_an_oversized_payload_routing_key_does_not_destroy_the_event(
    channel: Channel,
) -> None:
    """The collision's worst outcome was total loss, for a stated false reason.

    Measured on `235b377`: a payload key `id` of 400,000 chars overwrote the
    routing id, `clampMeta` then found a routing key over the per-attribute cap
    and refused the event outright — so an ordinary `pipeline_failed` for MR
    66666 never arrived, and stderr blamed "routing key over 2048 chars" when
    the routing id was five digits. A loud failure carrying a false reason is
    not better than a quiet one.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "66666",
        "event": "pipeline_failed",
        "payload": {"id": "Z" * 400_000, "title": "small"},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert meta["id"] == "66666", f"the event was re-aimed or lost: {meta}"
    assert meta["title"] == "small", "an unrelated attribute was collateral damage"
    assert "collided" in meta, "the 400 KB payload key vanished without a word"
    assert "400000" in meta["collided"], (
        f"the disclosure does not state the real size: {meta['collided']}")


def test_a_payload_key_that_cannot_become_an_attribute_is_disclosed(
    channel: Channel,
) -> None:
    """`__proto__` was a silent loss hiding inside the same six lines.

    `meta["__proto__"] = "x"` on an object literal runs the inherited setter and
    creates no own property, so the key never reached the wire and nothing said
    so — the same defect class as the briefed one, one layer down.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "55555",
        "event": "pipeline_failed",
        "payload": {"__proto__": "poison", "title": "small"},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert meta["id"] == "55555"
    assert "collided" in meta, f"__proto__ was discarded in silence: {sorted(meta)}"
    assert "__proto__" in meta["collided"], (
        f"the disclosure does not name the key: {meta['collided']}")


def test_first_tick_body_and_attribute_agree(channel: Channel) -> None:
    """The body and the attributes must not disagree about the same fact.

    `buildContent` read `first_tick` from the raw event while the attribute came
    from `meta`, so a payload key could set the attribute to "true" while the
    body carried no "(state at watcher start)" note — two surfaces, one event,
    two answers. #605 already states the rule this restores: every value in the
    body comes from `meta`.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "77777",
        "event": "pipeline_failed", "first_tick": True,
        "payload": {"first_tick": "false", "title": "small"},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert meta["first_tick"] == "true", f"the payload overrode the watcher's flag: {meta}"
    assert "state at watcher start" in msg["params"]["content"], (
        f"the attribute says first_tick, the body does not: {msg['params']['content']}")


def test_an_ordinary_event_is_untouched_and_says_nothing(channel: Channel) -> None:
    """The over-reach guard: this passes before *and* after, deliberately.

    A `collided` attribute on an event where nothing collided would be noise on
    a surface that has to stay trustworthy, and a patch that reserved too much —
    or annotated every event — fails here rather than reading as progress.
    """
    channel.emit({
        "ts": "2026-07-30T00:00:00Z", "source": "gitlab-mr", "id": "33173",
        "event": "pipeline_failed",
        "payload": {
            "title": "feat: something ordinary",
            "url": "https://gitlab.example.com/x/-/merge_requests/33173",
            "pipeline_id": "154177",
            "observed_failed_jobs": "test_unit_1,test_unit_2",
        },
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert "collided" not in meta, f"a clean event was reported as collided: {meta.get('collided')}"
    assert "clamped" not in meta, f"a small event was reported as clamped: {meta.get('clamped')}"
    assert meta["title"] == "feat: something ordinary"
    assert meta["url"] == "https://gitlab.example.com/x/-/merge_requests/33173"
    assert meta["pipeline_id"] == "154177"
    assert meta["observed_failed_jobs"] == "test_unit_1,test_unit_2"
