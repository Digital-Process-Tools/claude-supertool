#!/usr/bin/env python3
"""youtube_reply:COMMENT_ID|TEXT_OR_file://PATH[|force][|force-dup] (#2593).

Posts one reply via `comments.insert` (parentId=COMMENT_ID), then reads it
back through `comments.list` -- the same three-verdict shape `comment.py`
uses for `youtube_comment`, and for the same reason: a 200 from YouTube is
not visibility, and a failed read-back must not render like a confirmed one.

## Shares `comment.py`'s machinery, not its dedup key

`_sentinel.check`/`record` take a bare resource key and answer "have we
already written to this before" -- they were built for `youtube_comment`
keyed on a video, and #227's own note says these three ops reuse the module
"unchanged". This op honours that literally: it passes **the parent
`COMMENT_ID`**, not the video id, as the key. A reply is a write against one
specific comment thread, not against the whole video, and keying it on the
video would refuse a second reply to a *different* comment on a video this
account already replied on once -- which is not the duplicate the sentinel
exists to catch. The written `sent.jsonl` entry's `video_id` field therefore
holds a comment id for this op, not a video id; `op="youtube_reply"` in the
same entry is what tells the two apart when reading the log by hand.

The hourly rate cap is unaffected by this and stays account-wide across every
op that calls `_sentinel.record` -- YouTube's spam heuristics watch the
account's total write rate, not one endpoint's.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _publish_safety

import _sentinel  # noqa: E402
from _console import use_utf8_stdout  # noqa: E402
from _oauth import OAuthError, get_access_token  # noqa: E402
from _publish_safety import (  # noqa: E402
    apply_disclosure,
    require_confirm,
    safe_resolve_body_path,
)
from _sanitize import safe_short  # noqa: E402
from _yt import YouTubeAPIError, authorized  # noqa: E402

#: Same ceiling `comment.py` uses -- YouTube's own limit on a comment body
#: applies identically to a reply, which is also a comment resource.
MAX_LEN = 10000
_FILE_PREFIX = "file://"
USAGE = "youtube_reply:COMMENT_ID|TEXT_OR_file://PATH[|force][|force-dup]"


def _resolve_body(arg: str) -> str:
    """Inline text, or the contents of a `file://` path. See `comment.py`."""
    if not arg.startswith(_FILE_PREFIX):
        return arg
    path_str = arg[len(_FILE_PREFIX):]
    resolved = safe_resolve_body_path(path_str)
    if not resolved.is_file():
        sys.stderr.write(f"ERROR: file not found: {path_str}\n")
        sys.exit(2)
    return resolved.read_text(encoding="utf-8")


def parse_args(arg: str) -> tuple[str, str, bool, bool]:
    """(parent_comment_id, body, force, force_dup). Mirrors `comment.parse_args`."""
    parts = arg.split("|")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        sys.stderr.write(f"ERROR: usage {USAGE}\n")
        sys.exit(2)
    force = force_dup = False
    while len(parts) > 2:
        token = parts[-1].strip().lower()
        if token == "force":
            force = True
        elif token == "force-dup":
            force_dup = True
        else:
            break
        parts.pop()
    text_fields = parts[1:]
    body = _resolve_body("|".join(text_fields)).strip()
    if not body:
        sys.stderr.write("ERROR: reply body is empty\n")
        sys.exit(2)
    if len(body) > MAX_LEN:
        sys.stderr.write(
            f"ERROR: reply is {len(body)} chars (YouTube's max is {MAX_LEN}).\n")
        sys.exit(2)
    return parts[0].strip(), body, force, force_dup


def verify(comment_id: str, token: str, sent: str) -> tuple[str, str, str]:
    """Read the reply back. `(verdict, detail, video_id)` -- the same three
    verdicts `comment.verify` uses ("verified", "MISMATCH",
    "could-not-verify"), plus the video id the reply belongs to.

    Unlike `youtube_comment`, this op is only ever given a parent COMMENT_ID
    -- never a video id -- so `video_id` here is empty until this read-back
    tells us: the Comment resource's own `snippet.videoId` is where it comes
    from. Without it the receipt url below has no way to name the video at
    all and silently renders `?v=` empty on every single call, which is the
    op's own read-back link doing nothing for the "confirm in a logged-out
    browser" step this preset's whole design leans on.
    """
    try:
        data = authorized("comments", token,
                          {"part": "snippet", "id": comment_id})
    except YouTubeAPIError as e:
        return "could-not-verify", repr(safe_short(str(e), 300)), ""
    items = data.get("items") or []
    if not items:
        return "could-not-verify", (
            "the insert returned an id but comments.list came back with no "
            "such comment -- it may be held for review"), ""
    snippet = items[0].get("snippet") or {}
    got = (snippet.get("textOriginal") or "").strip()
    video_id = snippet.get("videoId") or ""
    if got == sent.strip():
        return ("verified",
                f"{len(sent)} characters, byte-identical to what was sent",
                video_id)
    return "MISMATCH", (
        f"read back {len(got)} characters, sent {len(sent)} -- "
        f"server text begins {safe_short(got, 120)!r}"), video_id


def main(arg: str) -> None:
    use_utf8_stdout()
    parent_id, body, force, force_dup = parse_args(arg)
    body, disclosure_state = apply_disclosure(body, max_len=MAX_LEN)
    require_confirm("youtube_reply", body, force=force)

    verdict, reason = _sentinel.check(parent_id)
    if verdict != "ok" and not force_dup:
        label = "ABORT" if verdict == "refuse" else "ABORT (cannot tell)"
        sys.stderr.write(
            f"{label} -- {reason}\n"
            "  Use |force-dup as a further field to reply anyway "
            "(|force alone confirms the publish, it does not override this).\n")
        sys.exit(1)

    try:
        token = get_access_token()
    except OAuthError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(2)

    try:
        data = authorized(
            "comments", token, {"part": "snippet"}, method="POST",
            body={"snippet": {"parentId": parent_id, "textOriginal": body}},
        )
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)

    comment_id = data.get("id") or "?"

    check_verdict, detail, video_id = verify(comment_id, token, body)
    # Built AFTER verify(), not before: this op is only ever given a parent
    # COMMENT_ID, never a video id, so the video id only exists once the
    # read-back's own snippet.videoId names it. Building the url from
    # parent_id/comment_id alone (as an earlier draft did) leaves ?v= empty
    # on every single call.
    url = (f"https://youtube.com/watch?v={video_id}&lc={parent_id}.{comment_id}"
           if video_id else
           f"https://youtube.com/watch?lc={parent_id}.{comment_id}"
           " (video id unknown -- the read-back did not return one)")

    print(f"youtube_reply OK parent={parent_id} comment_id={comment_id} url={url}")
    print(f"read-back: {check_verdict} -- {detail}")
    if disclosure_state != "appended":
        print(f"(disclosure: {disclosure_state})")
    print("NOTE: a 2xx and a read-back are not visibility. YouTube holds or "
          "shadow-bans programmatic comments silently, and the posting account "
          "can usually still see a held comment. Confirm in a logged-out "
          "browser before believing this landed.")

    try:
        _sentinel.record(op="youtube_reply", video_id=parent_id,
                         comment_id=comment_id, url=url,
                         verification=check_verdict)
    except OSError as e:
        sys.stderr.write(
            f"WARNING: the reply was published but could not be recorded in "
            f"{_sentinel.LOG_PATH} ({e}). The duplicate guard will not know "
            "about it -- add the line by hand or expect a re-run to post twice.\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write(f"ERROR: usage {USAGE}\n")
        sys.exit(2)
    main(":".join(sys.argv[1:]))
