#!/usr/bin/env python3
"""youtube_like:VIDEO_ID_OR_URL[|force][|force-dup] (#2593).

Sets this account's rating on a video to "like" via `videos.rate`, then reads
it back with `videos.getRating` -- the same "a 2xx is not the truth, read it
back" shape `comment.py`/`reply.py` use, adapted to an endpoint with no text
body of its own.

## Why there is no disclosure marker here

`apply_disclosure` exists to mark text this tool authored on a stranger's
behalf. A like carries no text -- there is nothing for `[AI-generated]` to be
appended to, and forcing the machinery on regardless would print a disclosure
banner attached to nothing. `require_confirm` still applies: a like is still
an account action taken on the operator's behalf, previewed as the video id
being liked rather than a body of prose.

## Two tokens, not one, for the same reason `comment.py` has two

`|force` confirms the action; `|force-dup` overrides the sentinel. They
shared a single `|force` in comment.py's first draft (#2543) and it left the
duplicate guard off on every invocation that actually acted, because `|force`
is the token an operator has to pass to act at all. Repeating that mistake
here for a "simpler" op would reintroduce the exact bug that draft was
rejected for.

## Sentinel: video-keyed, shared with `youtube_comment`

Unlike `reply.py`, this keeps the exact key `youtube_comment` uses --
`VIDEO_ID`. A comment and a like on the same video will therefore refuse each
other without `|force-dup`, which is a conservative default (`_sentinel`'s own
docstring: an unknown or a shared footprint refused is the safe direction),
not a claim that liking and commenting are the same action.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _publish_safety

import _sentinel  # noqa: E402
from _console import use_utf8_stdout  # noqa: E402
from _oauth import OAuthError, get_access_token  # noqa: E402
from _publish_safety import require_confirm  # noqa: E402
from _sanitize import safe_short  # noqa: E402
from _yt import YouTubeAPIError, authorized  # noqa: E402
from read import parse_video_id  # noqa: E402

USAGE = "youtube_like:VIDEO_ID_OR_URL[|force][|force-dup]"


def parse_args(arg: str) -> tuple[str, bool, bool]:
    """(video_id, force, force_dup)."""
    parts = arg.split("|")
    if not parts or not parts[0].strip():
        sys.stderr.write(f"ERROR: usage {USAGE}\n")
        sys.exit(2)
    tokens = [p.strip().lower() for p in parts[1:]]
    force = "force" in tokens
    force_dup = "force-dup" in tokens
    return parse_video_id(parts[0].strip()), force, force_dup


def verify(video_id: str, token: str) -> tuple[str, str]:
    """Read the rating back via `videos.getRating`. `(verdict, detail)` --
    the same three states `comment.verify` uses: "verified", "MISMATCH",
    "could-not-verify"."""
    try:
        data = authorized("videos/getRating", token, {"id": video_id})
    except YouTubeAPIError as e:
        # `safe_short`, not a bare slice: the same reasoning `comment.py`'s
        # verify() gives for this exact call shape -- an HTTP error body is
        # Google's and can carry a newline (column-0 forgery in a receipt an
        # agent parses) or a known injection pattern, and a bare `str(e)[:300]`
        # gives neither protection `reply.py`'s identical call gets.
        return "could-not-verify", repr(safe_short(str(e), 300))
    items = data.get("items") or []
    if not items:
        return "could-not-verify", (
            "videos.getRating came back with no rating for this video")
    rating = items[0].get("rating") or "?"
    if rating == "like":
        return "verified", "rating read back as 'like'"
    return "MISMATCH", f"rating read back as {rating!r}, expected 'like'"


def main(arg: str) -> None:
    use_utf8_stdout()
    video_id, force, force_dup = parse_args(arg)
    require_confirm("youtube_like", f"like video {video_id}", force=force)

    verdict, reason = _sentinel.check(video_id)
    if verdict != "ok" and not force_dup:
        label = "ABORT" if verdict == "refuse" else "ABORT (cannot tell)"
        sys.stderr.write(
            f"{label} -- {reason}\n"
            "  Use |force-dup as a further field to like anyway "
            "(|force alone confirms the action, it does not override this).\n")
        sys.exit(1)

    try:
        token = get_access_token()
    except OAuthError as e:
        # Escaped the same way this op's own verify() escapes an HTTP error
        # body (trap.d/227.oauth-error-body-unescaped-in-receipt.md): it is
        # Google's, and can carry a newline.
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(2)

    try:
        authorized("videos/rate", token, {"id": video_id, "rating": "like"},
                  method="POST")
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(1)

    check_verdict, detail = verify(video_id, token)

    print(f"youtube_like OK video={video_id}")
    print(f"read-back: {check_verdict} -- {detail}")

    try:
        _sentinel.record(op="youtube_like", video_id=video_id,
                         comment_id="", url=f"https://youtube.com/watch?v={video_id}",
                         verification=check_verdict)
    except OSError as e:
        sys.stderr.write(
            f"WARNING: the video was liked but could not be recorded in "
            f"{_sentinel.LOG_PATH} ({e}). The duplicate guard will not know "
            "about it.\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write(f"ERROR: usage {USAGE}\n")
        sys.exit(2)
    main(":".join(sys.argv[1:]))
