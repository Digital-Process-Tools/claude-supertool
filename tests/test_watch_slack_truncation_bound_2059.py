"""#2059: the poller's own truncation bound (`MESSAGE_CHARS_MAX`) must sit
strictly below the channel's per-attribute cap (`ATTR_MAX_CHARS` in
`notifiers/claude-channel/channel.ts`), including the truncation note it
appends.

Before this fix `MESSAGE_CHARS_MAX` was `4000`, above the transport's `2048`
-- so a message under 2048 chars never hit the source bound, and a message
over 2048 chars had its whole `title` attribute deleted by the channel
(over-cap attributes are dropped whole, not truncated -- see `clampMeta` in
`channel.ts`), including the truncation note that was supposed to say so.
No message over 2048 chars ever delivered any text at all.

This does not spin up the real Node channel process -- that would make the
test an integration test of a second language runtime for a one-line size
comparison. It reproduces the channel's own over-cap rule (`len > ATTR_MAX`
=> dropped whole) directly, which is the one fact this bug depends on and is
stated in `channel.ts`'s own docstring/comments read above.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

SOURCE_DIR = WATCH_DIR / "sources" / "slack"
POLLER = SOURCE_DIR / "poller.py"

CHANNEL = "C0123456"
BOT_UID = "U_BOT"
CTX = {"source": "slack", "id": CHANNEL, "only": []}

# The channel's own default, mirrored here only for the test's own
# assertions -- not read by the poller, which reads the environment
# variable itself (see `_attr_max_chars` in poller.py).
CHANNEL_ATTR_MAX_CHARS_DEFAULT = 2048


@pytest.fixture(autouse=True)
def _fake_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake-not-a-real-token-0006")
    monkeypatch.delenv("SUPERTOOL_CHANNEL_ATTR_MAX", raising=False)


def _load_poller():
    spec = importlib.util.spec_from_file_location("slack_watch_poller_2059", POLLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _channel_would_deliver(attr_value: str, attr_max: int) -> bool:
    """The one rule `channel.ts::clampMeta` applies to a non-routing
    attribute over the cap: dropped whole, never truncated."""
    return len(attr_value) <= attr_max


def test_message_chars_max_is_strictly_below_the_channel_attr_cap() -> None:
    poller = _load_poller()
    assert poller.MESSAGE_CHARS_MAX < CHANNEL_ATTR_MAX_CHARS_DEFAULT


def test_a_message_just_over_the_channel_cap_still_delivers_truncated_text() -> None:
    """The actual failure mode: before the fix, a 2049-char message's
    `title` attribute -- 2049 chars after the source bound never fired --
    was dropped whole by the channel, delivering no text at all."""
    poller = _load_poller()
    long_text = "x" * (CHANNEL_ATTR_MAX_CHARS_DEFAULT + 1)
    with mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID), \
         mock.patch.object(poller, "_fetch", return_value=(
             [{"ts": "1.0", "text": long_text, "user": BOT_UID}], "")):
        events, _ = poller.poll({"cursor": "0.5", "bot_user_id": BOT_UID}, CTX)
    title = events[0]["payload"]["title"]
    assert title != ""
    assert _channel_would_deliver(title, CHANNEL_ATTR_MAX_CHARS_DEFAULT), (
        f"title is {len(title)} chars, over the channel's own cap of "
        f"{CHANNEL_ATTR_MAX_CHARS_DEFAULT} -- the channel would drop it whole"
    )
    assert "MESSAGE_CHARS_MAX=" in title  # the note itself survived


def test_a_message_at_slacks_own_ceiling_still_delivers_truncated_text() -> None:
    """Slack's real per-message ceiling (~40,000 chars) is the worst case
    for the truncation note's own length (`+NNNNN chars truncated`) -- this
    is the case that could blow the margin if it were too tight."""
    poller = _load_poller()
    long_text = "x" * 40000
    with mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID), \
         mock.patch.object(poller, "_fetch", return_value=(
             [{"ts": "1.0", "text": long_text, "user": BOT_UID}], "")):
        events, _ = poller.poll({"cursor": "0.5", "bot_user_id": BOT_UID}, CTX)
    title = events[0]["payload"]["title"]
    assert title != ""
    assert _channel_would_deliver(title, CHANNEL_ATTR_MAX_CHARS_DEFAULT)


def test_a_short_message_under_the_bound_is_never_touched() -> None:
    """Must-not-fire pair: a message safely under the new, lower bound must
    still pass through byte-for-byte, not get truncated preemptively."""
    poller = _load_poller()
    short_text = "hello team"
    with mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID), \
         mock.patch.object(poller, "_fetch", return_value=(
             [{"ts": "1.0", "text": short_text, "user": BOT_UID}], "")):
        events, _ = poller.poll({"cursor": "0.5", "bot_user_id": BOT_UID}, CTX)
    assert events[0]["payload"]["title"] == short_text


def test_attr_max_chars_tracks_the_channels_own_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_attr_max_chars` reads `SUPERTOOL_CHANNEL_ATTR_MAX` the way the
    transport does, rather than hand-copying the bare literal `2048` -- an
    operator who raises or lowers the transport's cap gets a poller bound
    that moves with it."""
    monkeypatch.setenv("SUPERTOOL_CHANNEL_ATTR_MAX", "5000")
    poller = _load_poller()
    assert poller._attr_max_chars() == 5000
    assert poller.MESSAGE_CHARS_MAX < 5000
    assert poller.MESSAGE_CHARS_MAX > CHANNEL_ATTR_MAX_CHARS_DEFAULT


def test_attr_max_chars_falls_back_on_an_unparseable_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike the transport (which exits the process, #`capFromEnv`'s
    `EXIT_BAD_CAP`), a poller falls back rather than dying -- a stalled
    watcher is worse than one bounding on a stale-but-safe default for a
    tick."""
    monkeypatch.setenv("SUPERTOOL_CHANNEL_ATTR_MAX", "not-a-number")
    poller = _load_poller()
    assert poller._attr_max_chars() == CHANNEL_ATTR_MAX_CHARS_DEFAULT
