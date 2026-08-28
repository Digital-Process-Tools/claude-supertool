#!/usr/bin/env python3
"""Slack publish: slack_publish:CHANNEL_ID|TEXT_OR_FILE_OR_file://PATH[|THREAD_TS[|force]]

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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _publish_safety
from _auth import get_bot_token  # noqa: E402
from _api import SlackTransportError, call  # noqa: E402
from _publish_safety import safe_resolve_body_path, require_confirm  # noqa: E402

_FILE_PREFIX = "file://"


def _resolve_body(arg: str) -> str:
    """Resolve a text argument to its content -- mirrors `bluesky/publish.py::
    _resolve_body`. `file://path` MUST resolve to an existing file; a bare
    path that happens to exist is read the same way for backward compat with
    the other publish presets' calling convention; anything else is inline
    text."""
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
    try:
        p = Path(arg)
        if p.is_file():
            resolved = safe_resolve_body_path(arg)
            return resolved.read_text(encoding="utf-8")
    except OSError:
        pass
    return arg


def parse_args(arg: str) -> tuple[str, str, str | None, bool]:
    """Return (channel, text, thread_ts, force)."""
    parts = arg.split("|", 3)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        sys.stderr.write(
            "ERROR: usage slack_publish:CHANNEL_ID|TEXT_OR_FILE_OR_file://PATH[|THREAD_TS[|force]]\n"
        )
        sys.exit(2)
    channel = parts[0].strip()
    text = _resolve_body(parts[1]).strip()
    if not text:
        sys.stderr.write("ERROR: message body is empty after resolving it\n")
        sys.exit(2)
    thread_ts = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    force = len(parts) > 3 and parts[3].strip().lower() == "force"
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
