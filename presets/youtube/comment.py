#!/usr/bin/env python3
"""youtube_comment:VIDEO_ID_OR_URL|TEXT_OR_file://PATH[|force][|force-dup] (#227).

Posts one top-level comment via `commentThreads.insert`, then reads it back.

## The thing this op is mostly about

**A 200 from YouTube is not visibility.** Programmatic comments get held or
shadow-banned silently: the insert succeeds, the resource comes back with an
id, and nobody else ever sees it. #227 asked for a hard warning in the output
for exactly this. So the op does not report success on the write -- it reads
the comment back through `commentThreads.list` afterwards and renders one of
three verdicts:

  * `verified`      -- read back and the text matches what was sent.
  * `MISMATCH`      -- read back and the text does NOT match.
  * `could-not-verify` -- the read-back itself failed.

`could-not-verify` is not a pass and does not render like one. And even
`verified` is only a statement about what *this account* can see, which is
said in the output rather than left to be assumed: the account that posted a
held comment can generally still read it.

## Guardrails, all three refusing rather than warning

`require_confirm` (no `|force`, no publish), the sentinel (one comment per
video, 5 writes/hour), and `apply_disclosure` (the `[AI-generated]` marker,
opt-out via `no_publish_disclosure`). The sentinel's `cannot-tell` is treated
as a refusal: this preset has no delete op, so an unknown is the one state
where doing nothing is clearly right.

**The first two take different tokens.** `|force` confirms the publish,
`|force-dup` overrides the sentinel, and passing one does not grant the
other. They shared `|force` in the first draft of #2543, which left the
duplicate guard off on every invocation that actually published, since
`|force` is what an operator has to pass to publish at all.

**A body whose own text ends in the literal word "force"/"force-dup" is
ambiguous under `|` splitting** (#2600): there is no telling "that is body
text" from "that is the flag" once both spell the same word. Use `:::`
instead of `|` as the field separator when that matters --
`VIDEO:::body ending in the word force:::force` keeps the whole body intact
and reads the flag from its own explicit trailing field rather than
guessing.
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
from read import parse_video_id  # noqa: E402

#: YouTube's own ceiling on a comment. Over it the API 400s, so this is a
#: refusal before the network rather than a surprise after it.
MAX_LEN = 10000
_FILE_PREFIX = "file://"
USAGE = ("youtube_comment:VIDEO_ID_OR_URL|TEXT_OR_file://PATH[|force][|force-dup]"
          " (or ::: in place of | when the body might end in the word force/force-dup)")


def _resolve_body(arg: str) -> str:
    """Inline text, or the contents of a `file://` path.

    Only the explicit prefix reads a file. `bluesky_publish` also treats a bare
    existing path as a file for backward compatibility; this op is new, so it
    does not inherit that -- a comment whose text happens to match a filename
    should post that text, not the file.
    """
    if not arg.startswith(_FILE_PREFIX):
        return arg
    path_str = arg[len(_FILE_PREFIX):]
    resolved = safe_resolve_body_path(path_str)
    if not resolved.is_file():
        sys.stderr.write(f"ERROR: file not found: {path_str}\n")
        sys.exit(2)
    return resolved.read_text(encoding="utf-8")


def parse_args(arg: str) -> tuple[str, str, bool, bool]:
    """(video_id, body, force, force_dup).

    Two tokens, not one. `force` is the shared publish confirmation every
    op in this repo takes; `force-dup` is the separate permission to post
    over the sentinel. Folding them together would have disarmed the
    duplicate guard on every normal invocation, because `force` is the token
    the usage string tells an operator to pass in order to publish at all --
    and this preset has no delete op, so that is the guard that matters.

    Both are read from the trailing fields, in either order.

    Split on "|" by default. A body whose own last "|"-delimited segment
    happens to spell "force"/"force-dup" is genuinely ambiguous under that
    scheme -- there is no way to tell "the operator's last field is body
    text" from "the operator meant the flag" once both spell the same word,
    and #2600 is that ambiguity silently resolving in the flag's favour
    every time, dropping the trailing word from the body AND forging the
    publish confirmation. "|" is common in ordinary prose; ":::" is not, so
    when the caller opts into it (present anywhere in `arg`) it becomes the
    field separator instead, and a body typed with ordinary pipes in it
    (like the example above) survives untouched unless the operator also
    types out ":::force" as its own explicit field. This does not change
    default "|" parsing -- an already-shipped "|force" call must keep
    meaning what it always has.
    """
    sep = ":::" if ":::" in arg else "|"
    parts = arg.split(sep)
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
    # Rejoin on the same separator that was used to split, so a body
    # containing that character survives, which a plain `split(sep, 2)`
    # would silently truncate at the second one.
    body = _resolve_body(sep.join(text_fields)).strip()
    if not body:
        sys.stderr.write("ERROR: comment body is empty\n")
        sys.exit(2)
    if len(body) > MAX_LEN:
        sys.stderr.write(
            f"ERROR: comment is {len(body)} chars (YouTube's max is {MAX_LEN}).\n")
        sys.exit(2)
    return parse_video_id(parts[0].strip()), body, force, force_dup


def verify(comment_id: str, token: str, sent: str) -> tuple[str, str]:
    """Read the comment back. `(verdict, detail)`.

    Verdicts: "verified", "MISMATCH", "could-not-verify". The third exists
    because a failed read-back must not render like a successful one -- that is
    this repository's recurring defect, and here it would be reporting a
    published comment as confirmed on the strength of a call that never
    completed.
    """
    try:
        data = authorized("commentThreads", token,
                          {"part": "snippet", "id": comment_id})
    except YouTubeAPIError as e:
        # Escaped the same way the MISMATCH arm below does: the HTTP error
        # body is Google's and can carry a newline, which would otherwise
        # put the remainder at column 0 of a receipt an agent parses
        # (trap.d/227.oauth-error-body-unescaped-in-receipt.md).
        return "could-not-verify", repr(safe_short(str(e), 300))
    items = data.get("items") or []
    if not items:
        return "could-not-verify", (
            "the insert returned an id but commentThreads.list came back with "
            "no such thread -- it may be held for review")
    top = (((items[0].get("snippet") or {}).get("topLevelComment") or {})
           .get("snippet") or {})
    got = (top.get("textOriginal") or "").strip()
    if got == sent.strip():
        return "verified", f"{len(sent)} characters, byte-identical to what was sent"
    return "MISMATCH", (
        f"read back {len(got)} characters, sent {len(sent)} -- "
        f"server text begins {safe_short(got, 120)!r}")


def main(arg: str) -> None:
    # The read-back detail can carry server text on a MISMATCH, and that text
    # is whatever YouTube stored -- arbitrary unicode from a stranger's
    # keyboard. Printed to a cp1252 console it raises UnicodeEncodeError, and
    # it does so AFTER the comment is published, so the op dies reporting
    # nothing about a write that already happened (#2062's failure, on the one
    # path where it cannot be retried).
    use_utf8_stdout()
    video_id, body, force, force_dup = parse_args(arg)
    body, disclosure_state = apply_disclosure(body, max_len=MAX_LEN)
    require_confirm("youtube_comment", body, force=force)

    # `force` is spent on the confirmation above and buys nothing here. The
    # sentinel takes its own token, so confirming a publish is not also a
    # licence to double-post on a video this account already commented on.
    verdict, reason = _sentinel.check(video_id)
    if verdict != "ok" and not force_dup:
        label = "ABORT" if verdict == "refuse" else "ABORT (cannot tell)"
        sys.stderr.write(
            f"{label} -- {reason}\n"
            "  Use |force-dup as a further field to post anyway "
            "(|force alone confirms the publish, it does not override this).\n")
        sys.exit(1)

    try:
        token = get_access_token()
    except OAuthError as e:
        # Escaped the same way verify()'s could-not-verify arm is
        # (trap.d/227.oauth-error-body-unescaped-in-receipt.md): the OAuth
        # error body is Google's and can carry a newline.
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(2)

    try:
        data = authorized(
            "commentThreads", token, {"part": "snippet"}, method="POST",
            body={"snippet": {
                "videoId": video_id,
                "topLevelComment": {"snippet": {"textOriginal": body}},
            }},
        )
    except YouTubeAPIError as e:
        # Same escaping as the OAuthError arm above and verify()'s
        # could-not-verify arm: this is _yt._format_oauth_http_error's raw
        # body[:300], Google's bytes, not a stranger's.
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(1)

    thread_id = data.get("id") or "?"
    comment_id = ((((data.get("snippet") or {}).get("topLevelComment") or {})
                   .get("id")) or thread_id)
    url = f"https://youtube.com/watch?v={video_id}&lc={comment_id}"

    check_verdict, detail = verify(thread_id, token, body)

    print(f"youtube_comment OK video={video_id} comment_id={comment_id} url={url}")
    print(f"read-back: {check_verdict} -- {detail}")
    if disclosure_state != "appended":
        print(f"(disclosure: {disclosure_state})")
    print("NOTE: a 2xx and a read-back are not visibility. YouTube holds or "
          "shadow-bans programmatic comments silently, and the posting account "
          "can usually still see a held comment. Confirm in a logged-out "
          "browser before believing this landed.")

    try:
        _sentinel.record(op="youtube_comment", video_id=video_id,
                         comment_id=comment_id, url=url,
                         verification=check_verdict)
    except OSError as e:
        # The comment IS published. A logging failure must not read as a
        # failed write, and must not be silent either: the next run's
        # duplicate check is now blind to this one.
        sys.stderr.write(
            f"WARNING: the comment was published but could not be recorded in "
            f"{_sentinel.LOG_PATH} ({e}). The duplicate guard will not know "
            "about it -- add the line by hand or expect a re-run to post twice.\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write(f"ERROR: usage {USAGE}\n")
        sys.exit(2)
    main(":".join(sys.argv[1:]))
