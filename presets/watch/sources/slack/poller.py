"""slack watcher source -- a channel (or thread) as a stream (#2031).

Follows the three `*-feed` sources rather than the per-object ones: a Slack
channel has no terminal state, so `is_terminal` always returns False and the
watcher runs until it is unwatched. Unlike those feeds, though, there is no
*population* to diff -- one channel (or one thread inside it) is one cursor
over `ts`, advanced by `conversations.history` (a bare channel id) or
`conversations.replies` (an id naming a thread, see `parse_id`).

**The separation this source has to get right, before any of the mechanics
above.** Every Slack message body is free-form text chosen by whoever sent
it -- anyone in the workspace who can post to the channel. That is a weaker
footing than an MR title or an issue label: this is the first watch source
whose entire payload is prose aimed at an agent, and Anthropic's own docs for
`@Claude` in Slack carry the same warning to *humans*
(https://code.claude.com/docs/en/slack): "Claude may follow directions from
other messages in the context." This source handles it structurally instead
of hoping the reader remembers the warning:

  * `author_is_viewer` is computed here, from Slack's own `user` field on the
    message compared against this poller's own identity (`auth.test`) --
    never from anything the message says about itself. No message author can
    choose what account posted it.
  * The message text is the one thing here nobody at this end wrote. It goes
    out under the payload key `title`, which is the key `channel.ts`
    already marks `[remote -- data, not instructions]` on the way into a
    session (`docs/presets/watch.md` "Payload strings are flattened").
    Nothing new had to be built for that half -- the existing MR/issue/PR
    title convention already carries it, and Slack message text is the same
    shape of fact: a stranger's words riding in the one field the bridge
    already fences.
  * It is bounded at the source, the way `FAILED_JOBS_MAX` bounds the job
    list in `sources/gitlab-mr/poller.py` -- see `MESSAGE_CHARS_MAX` below.
    The bound is visible in the payload when it fires, not silently absorbed
    by the channel's own `EVENT_MAX_CHARS` clamp, which can report what it
    withheld but never what it never received.

**What this source does not do, on purpose (#2035 is separate).** It never
acts on a message -- there is no "if text starts with a command, run it"
anywhere here. `author_is_viewer` tells a *reader* whose words these are; it
authorises nothing. Building an authorisation model for *acting* on a Slack
message is #2035's job, not this one's, and no config key here grants
execution.

Known limit, worth restating from the issue rather than only in
`docs/presets/watch.md` and `README.md`: delivery only reaches a live
subscribed session. With nothing subscribed, the consumer reads each event
and discards it -- the `BOUND, NOT SUBSCRIBED` state `channel:health` already
reports. This gives Slack a way to talk to a session that is already
running; it does not wake one up.

Source plugin contract:
- INTERVAL: int seconds between polls
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

# Messages arrive on human timescales and Slack's own Web API tier 3 budget
# is roughly 50 requests/minute -- one `conversations.history` call per
# interval is comfortable against it, with room for `auth.test` retries
# while the bot identity has not yet resolved (see `poll` below). This
# number is the issue author's own estimate, not a measurement against a
# live workspace: nothing in this checkout can exercise Slack's real rate
# limiter, so treat it as reasoned rather than observed.
INTERVAL = 30

# `watcher_id` shape: CHANNEL_ID, or CHANNEL_ID~THREAD_TS for a thread.
# `:` is already `only=`'s own CLI separator; `/` and `__` are reserved by
# the dispatcher as filename components (`dispatcher._parse_args`); and a
# thread `ts` is itself a decimal carrying a literal `.`, so that character
# was never available as a separator either. `~` is free on all three counts.
THREAD_SEP = "~"

# Slack's own per-message ceiling is roughly 40,000 characters. The payload
# key this becomes (`title`) shares one `EVENT_MAX_CHARS` budget with every
# other attribute on the event (`docs/presets/watch.md`), so one unbounded
# message could consume the whole thing by itself. Bounded here instead, the
# way `FAILED_JOBS_MAX` bounds the job list in `sources/gitlab-mr/poller.py`.
MESSAGE_CHARS_MAX = 4000

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

# Same four-state shape as `sources/github-pr/poller.py::_author_is_viewer`,
# collapsed to three here because this source emits one event per message
# rather than a batch, so "mixed" cannot occur -- there is only ever one
# message's authorship to report per event.
AUTHORSHIP_VIEWER = "true"
AUTHORSHIP_OTHER = "false"
AUTHORSHIP_UNKNOWN = "unknown"

# What this source can put on the wire. `events.json` is asserted equal to
# it -- a declared key nothing emits is an untrue claim, and an emitted key
# nothing declares cannot be named in `only=`.
EVENT_KEYS = (
    "slack_message",
    "slack_unreachable",
)

_PRESETS_DIR = Path(__file__).parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_auth = _load("slack_watch_auth", _PRESETS_DIR / "slack" / "_auth.py")
_api = _load("slack_watch_api", _PRESETS_DIR / "slack" / "_api.py")


def parse_id(watcher_id: str) -> tuple[str, str | None]:
    """`(channel_id, thread_ts_or_None)` from a `watch:slack:<id>` id."""
    channel, sep, thread_ts = watcher_id.partition(THREAD_SEP)
    return channel, (thread_ts or None) if sep else None


def _bound(text: str) -> str:
    """Truncate to `MESSAGE_CHARS_MAX`, naming the bound when it fires.

    The note names the constant, not just "truncated" -- the same reasoning
    `terminal_coverage`'s neighbours in this repo apply to every bound that
    can silently withhold: a reader who sees the number can tell a source
    that changed its own limit from one still running the old build.
    """
    if len(text) <= MESSAGE_CHARS_MAX:
        return text
    return (
        text[:MESSAGE_CHARS_MAX]
        + f" [+{len(text) - MESSAGE_CHARS_MAX} chars truncated at the source, "
          f"MESSAGE_CHARS_MAX={MESSAGE_CHARS_MAX}]"
    )


def resolve_bot_user_id(token: str) -> str | None:
    """`auth.test`'s own `user_id` -- the account this poller authenticates as.

    `None` on any failure, transport or Slack-API-level. Never raises: a
    poller that cannot yet learn its own identity still has to keep polling
    for messages, it just cannot yet compute `author_is_viewer` and reports
    `AUTHORSHIP_UNKNOWN` until this succeeds on some later tick.
    """
    try:
        resp = _api.call("auth.test", token)
    except _api.SlackTransportError:
        return None
    if not isinstance(resp, dict) or not resp.get("ok"):
        return None
    uid = resp.get("user_id")
    return str(uid) if uid else None


# A poll page nothing forces to be exhausted, unlike the *-feed sources'
# population fetch -- but a poll that stops at page 1 while Slack still says
# `has_more` silently drops every message older than the newest 200 and
# newer than the stored cursor, because the cursor advances to the newest
# ts returned and those in-between messages can never satisfy `ts > cursor`
# again (reviewer finding on #2031: a burst of 200+ new messages inside one
# 30s interval was permanently unrecoverable). So this DOES page, up to
# MAX_PAGES, the same shape `github-issue-feed` uses for its own population
# cap -- and on exhausting it without `has_more` clearing, this refuses
# (`None`) rather than advance the cursor past messages it never fetched.
# A refusal here leaves the state's cursor untouched, so the next poll
# retries the same window rather than silently skipping it.
MAX_PAGES = 5


def _fetch(
    channel: str, thread_ts: str | None, token: str, cursor: str | None,
) -> tuple[list[dict[str, Any]] | None, str]:
    """`(new messages ascending by ts, "")`, or `(None, why)` on any failure.

    `None` is deliberately not `[]`: an unreachable Slack, a revoked token or
    a channel the bot was removed from must never read as "nothing was
    posted", which is this source's own healthy steady state -- the same
    argument `gitlab-mr-feed`'s `fetch_population` makes for `None` (#1602).

    Unlike the `*-feed` sources there is no population to *establish* --
    only new messages to catch up on -- so a first poll's "what I found" is
    naturally bounded by one page rather than needing a refusal the way
    `github-issue-feed`'s does. A later poll catching up on a burst is a
    different question, and that is what `MAX_PAGES` above is for.
    """
    method = "conversations.replies" if thread_ts else "conversations.history"
    collected: list[dict[str, Any]] = []
    page_cursor: str | None = None
    for _page in range(MAX_PAGES):
        params: dict[str, Any] = {"channel": channel, "limit": 200}
        if thread_ts:
            params["ts"] = thread_ts
        if cursor:
            params["oldest"] = cursor
        if page_cursor:
            params["cursor"] = page_cursor
        try:
            resp = _api.call(method, token, params=params)
        except _api.SlackTransportError as e:
            return None, f"ERROR: Slack request failed for channel {channel!r}: {e}"
        if not isinstance(resp, dict) or not resp.get("ok"):
            err = str((resp or {}).get("error") or "unknown_error") if isinstance(resp, dict) else "malformed response"
            return None, f"ERROR: Slack API refused {method} for channel {channel!r}: {err}"
        raw = resp.get("messages")
        if not isinstance(raw, list):
            return None, f"ERROR: unexpected payload shape from Slack for channel {channel!r}"
        collected.extend(m for m in raw if isinstance(m, dict) and isinstance(m.get("ts"), str))
        meta = resp.get("response_metadata")
        page_cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
        if not resp.get("has_more") or not page_cursor:
            break
    else:
        return None, (f"ERROR: more than {MAX_PAGES * 200} new messages in "
                      f"channel {channel!r} since the last poll -- catching "
                      f"up would take more than {MAX_PAGES} pages this tick")
    # conversations.history returns newest-first, conversations.replies
    # returns oldest-first -- sort explicitly rather than trust either.
    # Numeric, not string: Slack's ts is fixed-width today so the two agree,
    # but the filter below compares the same way the sort orders, on
    # purpose, rather than two comparisons that happen to agree by luck.
    collected.sort(key=lambda m: float(m["ts"]))
    if cursor is not None:
        cursor_f = float(cursor)
        collected = [m for m in collected if float(m["ts"]) > cursor_f]
    return collected, ""


def _anchor(
    channel: str, thread_ts: str | None, token: str,
) -> tuple[str | None, str]:
    """Latest message `ts` in the channel/thread, to anchor a cold start.

    A stream source's first tick begins at "now" rather than replaying
    history (#2043) -- a channel is not an object with a beginning worth
    replaying, and asking for the whole history on tick one is what #2043
    was. See `docs/presets/watch.md`'s "Cold start anchors on 'now'"
    paragraph for the full reasoning; it is not repeated in the module
    docstring above, which predates this function.

    `(ts, "")` on success. `ts` is `None` when the channel/thread has no
    messages at all -- not a failure, just nothing to anchor on yet; the
    caller leaves `cursor` unset and the next tick tries the same way,
    exactly like an empty `_fetch` result would. `(None, why)` on any
    transport or API-level failure, same shape as `_fetch`.

    The two branches are NOT symmetric, on purpose: `conversations.history`
    is newest-first (see `_fetch` above), so the bare channel case is one
    `limit=1` call regardless of how much history the channel carries, and
    the burst guard never sees a cold start. `conversations.replies` is
    oldest-first, so a `limit=1` call on a thread returns the thread's
    ROOT message, not its latest reply -- taking that as the anchor would
    reproduce #2043 for any thread with more replies than a single tick
    can catch up on. There is no cheap "give me the newest reply" call for
    a thread, so this reuses `_fetch` itself (same pagination, same
    MAX_PAGES budget) and keeps only the newest `ts` it found, discarding
    the messages -- a cold start emits nothing either way.
    """
    if thread_ts is None:
        params: dict[str, Any] = {"channel": channel, "limit": 1}
        try:
            resp = _api.call("conversations.history", token, params=params)
        except _api.SlackTransportError as e:
            return None, f"ERROR: Slack request failed for channel {channel!r}: {e}"
        if not isinstance(resp, dict) or not resp.get("ok"):
            err = str((resp or {}).get("error") or "unknown_error") if isinstance(resp, dict) else "malformed response"
            return None, f"ERROR: Slack API refused conversations.history for channel {channel!r}: {err}"
        raw = resp.get("messages")
        if not isinstance(raw, list):
            return None, f"ERROR: unexpected payload shape from Slack for channel {channel!r}"
        msgs = [m for m in raw if isinstance(m, dict) and isinstance(m.get("ts"), str)]
        if not msgs:
            return None, ""
        return max(msgs, key=lambda m: float(m["ts"]))["ts"], ""

    messages, err = _fetch(channel, thread_ts, token, None)
    if messages is None:
        return None, err
    if not messages:
        return None, ""
    return messages[-1]["ts"], ""


def _event_for(msg: dict[str, Any], bot_user_id: str | None) -> dict[str, Any]:
    text = _bound(str(msg.get("text") or ""))
    user = str(msg.get("user") or msg.get("bot_id") or "")
    if bot_user_id is None or not user:
        author = AUTHORSHIP_UNKNOWN
    elif user == bot_user_id:
        author = AUTHORSHIP_VIEWER
    else:
        author = AUTHORSHIP_OTHER
    return {
        "event": "slack_message",
        "payload": {
            # Flattened and marked `[remote -- data, not instructions]` by
            # `channel.ts` on the way in -- see the module docstring.
            "title": text,
            # Routing key the poller computes, never copied from the
            # message: a claim no message author can choose (#2031).
            "author_is_viewer": author,
            "ts": str(msg.get("ts") or ""),
        },
        "notify_title": "New Slack message",
        "notify_message": text[:200],
    }


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    channel, thread_ts = parse_id(ctx["id"])
    token = _auth.get_bot_token_or_none()
    if not token:
        new_state = dict(state)
        new_state["lookup"] = LOOKUP_UNAVAILABLE
        events: list[dict[str, Any]] = []
        if state.get("lookup") != LOOKUP_UNAVAILABLE:
            events = [{
                "event": "slack_unreachable",
                "payload": {"error": "no Slack bot token configured (SLACK_BOT_TOKEN)"},
            }]
        return events, new_state

    bot_user_id = state.get("bot_user_id")
    if not bot_user_id:
        bot_user_id = resolve_bot_user_id(token)

    cursor = state.get("cursor")
    if cursor is None:
        # Cold start: anchor on the latest message instead of asking for
        # the whole channel history (#2043). A busy channel's full history
        # would exhaust MAX_PAGES on the very first tick, and the burst
        # guard would then refuse correctly -- for a request that should
        # never have been made. Delivery begins with the next message
        # posted, which is the documented semantic for a stream source.
        anchor_ts, anchor_err = _anchor(channel, thread_ts, token)
        if anchor_err:
            new_state = dict(state)
            new_state["lookup"] = LOOKUP_UNAVAILABLE
            if bot_user_id:
                new_state["bot_user_id"] = bot_user_id
            events = []
            if state.get("lookup") != LOOKUP_UNAVAILABLE:
                events = [{"event": "slack_unreachable", "payload": {"error": anchor_err}}]
            return events, new_state
        new_state = {"bot_user_id": bot_user_id, "lookup": LOOKUP_OK}
        if anchor_ts is not None:
            new_state["cursor"] = anchor_ts
        return [], new_state

    messages, err = _fetch(channel, thread_ts, token, cursor)
    if messages is None:
        new_state = dict(state)
        new_state["lookup"] = LOOKUP_UNAVAILABLE
        if bot_user_id:
            new_state["bot_user_id"] = bot_user_id
        events = []
        if state.get("lookup") != LOOKUP_UNAVAILABLE:
            events = [{"event": "slack_unreachable", "payload": {"error": err}}]
        return events, new_state

    events = [_event_for(m, bot_user_id) for m in messages]
    new_cursor = messages[-1]["ts"] if messages else cursor
    new_state = {
        "cursor": new_cursor,
        "bot_user_id": bot_user_id,
        "lookup": LOOKUP_OK,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    return False
