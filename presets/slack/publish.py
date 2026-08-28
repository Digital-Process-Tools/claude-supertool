#!/usr/bin/env python3
"""Slack publish: slack_publish:CHANNEL_ID|TEXT_OR_file://PATH[|THREAD_TS[|force]]

A deliberate, call-it-on-purpose post to a Slack channel (or a reply inside
a thread), via `chat.postMessage`. This is NOT the same job as the Slack
webhook notifier documented in `docs/notifiers.md` ("Slack ping when files
in `src/auth/` change") -- that one is fire-and-forget, fires on a glob
match against a file event, has no return path, and carries none of the
outbound safety gating below. Use the notifier for "ping Slack whenever X
happens on the op stream"; use this op for "post this sentence now, and
hand me back what I need to reply in the same thread later" (#2032).

CHANNEL_ID, not a channel name (open question the issue left to this
implementation): a name is resolved server-side and can be re-pointed by
someone else out from under a saved call, while an id cannot. Look the id up
once in the Slack UI (channel details -> "Channel ID") and use that.

Third field, THREAD_TS: the `ts` of the parent message to reply inside its
thread. Cheap to carry from the start -- the issue names this explicitly,
because retrofitting it once callers already assume a flat post would be a
breaking change to every existing call.

Returns the posted message's own `ts`, on stdout -- what a later thread
reply or edit needs, and the one thing about this op's own output that is
not optional (per the issue). A permalink is also fetched (`chat.
getPermalink`) and printed when that lookup succeeds; if it does not, the
publish itself is not undone or retried over it -- the post already
happened and a `ts` you cannot yet link to is not a `ts` you do not have.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _publish_safety
from _auth import get_bot_token  # noqa: E402
from _api import SlackTransportError, call  # noqa: E402
from _publish_safety import safe_resolve_body_path, require_confirm  # noqa: E402

_FILE_PREFIX = "file://"


def _resolve_body(arg: str) -> str:
    """Resolve a text argument to its content.

    Unlike `bluesky/publish.py::_resolve_body` and the `devto`/`hashnode`
    comment presets, there is no bare-path auto-detect here (#2039).
    Those four presets' TEXT is maintainer-typed, so a path landing in that
    slot is a path a human wrote. `slack_publish`'s TEXT is the first of
    these argument slots to also carry stranger-authored prose -- the
    `slack` watch source hands back message text written by anyone in the
    workspace, and this op's own module docstring anticipates that text
    being echoed back into a reply. A bare path that happens to `is_file()`
    and sits inside the allowlist (`.max/` among them -- the maintainer's
    own private drafts, per `.gitignore:43`) would let untrusted text choose
    a file to read and post its contents, with no marker in the call that a
    file was ever involved. Only the explicit `file://` prefix may trigger a
    read: it is seven characters no forwarded message would carry by
    accident, so a path is only ever resolved when a human meant one."""
    if arg.startswith(_FILE_PREFIX):
        path_str = arg[len(_FILE_PREFIX):]
        resolved = safe_resolve_body_path(path_str)
        if not resolved.is_file():
            sys.stderr.write(
                f"ERROR: file not found: {path_str}\n"
                "(file:// prefix requires the file to exist -- typo or wrong path?)\n"
            )
            sys.exit(2)
        return resolved.read_text(encoding="utf-8")
    return arg


_SLACK_TS_RE = re.compile(r"\d{9,}\.\d{3,6}")


def parse_args(arg: str) -> tuple[str, str, str | None, bool]:
    """Return (channel, text, thread_ts, force).

    Parsed from the RIGHT, not with a bounded `split("|", 3)` (#2032 review).
    TEXT is the one field here that can be Slack message content -- someone
    else's words, possibly echoed back from a `slack_message` event by an
    agent replying in a thread -- and unlike CHANNEL_ID/THREAD_TS/force it is
    not itself pipe-free by construction. A left-bounded split consumes `|`
    characters INSIDE the message first and only then reads what is left as
    THREAD_TS/force, so a message body containing two or more `|` could
    silently truncate the text and reinterpret its own tail as THREAD_TS or
    -- worse -- as the literal token `force`, skipping `require_confirm`'s
    human-confirmation gate on an "acts"-class op with nothing printed to
    say so.

    So `force` and `thread_ts` are recognised only when the LAST field(s),
    after splitting on every `|`, EXACTLY match their own narrow shape --
    the literal token `force`, matched case-insensitively and after
    stripping surrounding whitespace (so `FORCE`, ` force `, and `\tforce`
    all count), and a Slack `ts` (digits, a literal dot, digits) -- and only
    then are they peeled off before the text is reassembled from whatever
    pipe-joined fields remain. Everything else stays part of TEXT, including
    a literal `|` in the message. This is a mitigation, not a proof: a
    message whose own last field happens to match either narrow shape --
    `force` (however cased or padded) or a `ts`-shaped number -- is still
    misread as the flag or the thread to reply into, and there is no
    escaping delimiter that solves that in general for a scheme that packs
    an untrusted string and structured fields into one pipe-joined blob. The
    `ts` case is left open deliberately (#2040): when `force` is NOT also
    present, `require_confirm`'s preview shows the resolved `(thread ...)`
    before anything posts, so a human sees the misrouting before it happens
    -- but the two strips compose, so a message ending in a `ts`-shaped
    field followed by a literal `force` hijacks the thread AND skips the
    preview in one string; this is the same "any two pipes chosen right"
    class the whole docstring is about, not a new one. It narrows the window from
    "any two pipes" to "the exact trailing token", which is the fix worth
    making without redesigning the argument grammar the issue asked for.
    """
    parts = arg.split("|")
    if len(parts) < 2 or not parts[0].strip():
        sys.stderr.write(
            "ERROR: usage slack_publish:CHANNEL_ID|TEXT_OR_file://PATH[|THREAD_TS[|force]]\n"
        )
        sys.exit(2)
    channel = parts[0].strip()
    rest = parts[1:]
    force = False
    if len(rest) > 1 and rest[-1].strip().lower() == "force":
        force = True
        rest = rest[:-1]
    thread_ts: str | None = None
    if len(rest) > 1 and _SLACK_TS_RE.fullmatch(rest[-1].strip()):
        thread_ts = rest[-1].strip()
        rest = rest[:-1]
    text_raw = "|".join(rest)
    if not text_raw.strip():
        sys.stderr.write(
            "ERROR: usage slack_publish:CHANNEL_ID|TEXT_OR_file://PATH[|THREAD_TS[|force]]\n"
        )
        sys.exit(2)
    text = _resolve_body(text_raw).strip()
    if not text:
        sys.stderr.write("ERROR: message body is empty after resolving it\n")
        sys.exit(2)
    return channel, text, thread_ts, force


def fetch_permalink(channel: str, ts: str, token: str) -> str:
    """Best-effort -- `""` on any failure. The publish already happened by
    the time this runs; a permalink lookup failing must not read as the post
    itself having failed."""
    try:
        resp = call("chat.getPermalink", token,
                    params={"channel": channel, "message_ts": ts})
    except SlackTransportError:
        return ""
    if not isinstance(resp, dict) or not resp.get("ok"):
        return ""
    return str(resp.get("permalink") or "")


def main(arg: str) -> None:
    channel, text, thread_ts, force = parse_args(arg)
    preview = f"{channel}: {text}" + (f" (thread {thread_ts})" if thread_ts else "")
    require_confirm("slack_publish", preview, force=force)
    token = get_bot_token()
    body: dict[str, object] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    try:
        resp = call("chat.postMessage", token, body=body)
    except SlackTransportError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    if not resp.get("ok"):
        err = str(resp.get("error") or "unknown_error")
        sys.stderr.write(f"ERROR: Slack refused chat.postMessage: {err}\n")
        sys.exit(1)
    ts = str(resp.get("ts") or "")
    print(f"(published channel={channel} ts={ts})")
    permalink = fetch_permalink(channel, ts, token) if ts else ""
    if permalink:
        print(f"URL: {permalink}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    main(":".join(sys.argv[1:]))
