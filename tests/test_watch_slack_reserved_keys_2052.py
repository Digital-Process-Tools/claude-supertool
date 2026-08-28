"""#2052: no payload key this poller emits may collide with the channel's
own reserved routing/disclosure names.

The reserved set is a constant on the transport side (`RESERVED_KEYS` in
`notifiers/claude-channel/channel.ts`, spread from `ROUTING_KEYS`), not a
list this test would otherwise have to hand-copy and let drift out of sync
-- which is exactly how the original defect went unnoticed: the poller's own
`"ts"` payload key collided with `ROUTING_KEYS`' `"ts"` and the merge simply
dropped it. This parses the two `Set([...])` literals out of the real
TypeScript source with a narrow regex rather than mirroring their contents
by hand, the same approach the v0.52.0 gate 3 release audit used on this
same finding (see the issue's own comment).

Same loading style as `test_watch_slack_poller_2031.py`: the real poller
module, loaded fresh per test, with `_fetch`/`resolve_bot_user_id` mocked at
the poller's own seams so nothing here touches a live Slack workspace.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).parent.parent
WATCH_DIR = REPO_ROOT / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

SOURCE_DIR = WATCH_DIR / "sources" / "slack"
POLLER = SOURCE_DIR / "poller.py"
CHANNEL_TS = REPO_ROOT / "notifiers" / "claude-channel" / "channel.ts"

CHANNEL = "C0123456"
BOT_UID = "U_BOT"
CTX = {"source": "slack", "id": CHANNEL, "only": []}
THREAD_CTX = {"source": "slack", "id": f"{CHANNEL}~1000000000.000000", "only": []}


@pytest.fixture(autouse=True)
def _fake_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake-not-a-real-token-0006")


def _load_poller():
    spec = importlib.util.spec_from_file_location("slack_watch_poller_2052", POLLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_set_literal(src: str, name: str, _seen: set[str] | None = None) -> set[str]:
    """The string literals inside `const NAME = new Set([...])`.

    Not a JS parser -- a narrow regex over one array literal. `...OTHER`
    spread entries are resolved recursively so `RESERVED_KEYS` (which spreads
    `ROUTING_KEYS`) comes back complete. `_seen` guards against a spread
    cycle turning a bug in the source into infinite recursion here.
    """
    seen = _seen or set()
    assert name not in seen, f"cycle detected parsing Set literals: {seen} -> {name}"
    seen = seen | {name}
    m = re.search(
        r"const\s+" + re.escape(name) + r"\s*=\s*new\s+Set\(\[(.*?)\]\)",
        src, re.DOTALL,
    )
    assert m is not None, f"could not find `const {name} = new Set([...])` in {CHANNEL_TS}"
    body = m.group(1)
    keys: set[str] = set()
    for spread in re.findall(r"\.\.\.(\w+)", body):
        keys |= _parse_set_literal(src, spread, seen)
    keys |= set(re.findall(r'"([^"]+)"', body))
    return keys


def _reserved_keys() -> set[str]:
    src = CHANNEL_TS.read_text(encoding="utf-8")
    return _parse_set_literal(src, "RESERVED_KEYS")


def _one_event(poller, thread: bool = False) -> dict:
    ctx = THREAD_CTX if thread else CTX
    msg = {"ts": "1000000001.000000", "text": "hi", "user": BOT_UID}
    with mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID), \
         mock.patch.object(poller, "_fetch", return_value=([msg], "")):
        events, _ = poller.poll({"cursor": "0.5", "bot_user_id": BOT_UID}, ctx)
    return events[0]


def test_reserved_keys_constant_is_reachable_and_non_trivial() -> None:
    """Sanity check on the parser, not on the poller: if this regressed to
    returning an empty or tiny set, the tests below would pass vacuously."""
    reserved = _reserved_keys()
    assert {"watcher_source", "id", "event", "ts", "first_tick"} <= reserved
    assert len(reserved) >= 10


def test_no_emitted_payload_key_is_reserved() -> None:
    """The actual defect: `_event_for`'s payload used to carry a key
    literally named `ts`, one of `RESERVED_KEYS`, so the envelope silently
    dropped it and every `slack_message` event's `ts` attribute was the
    consumer's own emit time, never the message's (#2052)."""
    poller = _load_poller()
    payload_keys = set(_one_event(poller)["payload"])
    reserved = _reserved_keys()
    collisions = payload_keys & reserved
    assert collisions == set(), f"payload keys collide with the reserved set: {collisions}"


def test_a_deliberately_reserved_key_would_be_caught() -> None:
    """Positive control for the test above: without this, a broken parse of
    `RESERVED_KEYS` (e.g. an empty set from a regex that stopped matching)
    would make `test_no_emitted_payload_key_is_reserved` pass for the wrong
    reason. Simulate the old regression -- a payload carrying a reserved
    name -- and confirm the same check catches it."""
    poller = _load_poller()
    payload = dict(_one_event(poller)["payload"])
    payload["id"] = "simulates the pre-#2052 regression"
    reserved = _reserved_keys()
    collisions = set(payload) & reserved
    assert collisions == {"id"}


def test_message_ts_carries_the_messages_own_timestamp() -> None:
    """The value the rename exists to preserve, not just the key name."""
    poller = _load_poller()
    payload = _one_event(poller)["payload"]
    assert payload["message_ts"] == "1000000001.000000"


def test_thread_ts_reaches_the_consumer_on_a_thread_watch() -> None:
    """`parse_id` already splits `CHANNEL~THREAD_TS`, but a consumer reading
    the event only ever saw the composite `id` -- the thread parent's own ts
    was unreachable (#2052's "hidden judgment call"). A thread-arm watch
    must surface it."""
    poller = _load_poller()
    payload = _one_event(poller, thread=True)["payload"]
    assert payload["thread_ts"] == "1000000000.000000"


def test_thread_ts_is_empty_on_a_bare_channel_watch() -> None:
    """Must-not-fire pair for the test above: a bare-channel watch has no
    thread parent, so `thread_ts` must not carry a stale or wrong value."""
    poller = _load_poller()
    payload = _one_event(poller, thread=False)["payload"]
    assert payload["thread_ts"] == ""
