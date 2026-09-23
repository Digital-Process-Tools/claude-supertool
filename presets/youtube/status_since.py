#!/usr/bin/env python3
"""youtube_status_since[:ISO] (#2593).

New top-level comments on the operator's own videos since a cutoff, across
`videos_read` config's default video count. Groups per-video, newest video
upload first.

## Why this needs OAuth2 despite being a read

Every other read op in this preset uses an API key: `search.list`,
`videos.list`, `playlistItems.list` and a public `commentThreads.list` all
work against public data with no user attached. "Own videos" is not public
data in that sense -- `channels.list?mine=true` only answers for whichever
credential made the call, so this op needs the same OAuth2 grant
`youtube_comment`/`youtube_reply`/`youtube_like` use, even though it never
writes anything (#227's own note: "grouped here for the credential, not the
blast radius"). No sentinel, no rate cap, no confirmation gate -- those exist
to slow down or refuse a *write*, and this op cannot write at all.

## Cutoff default

With no `ISO` argument this defaults to 24 hours before now. `commentThreads.
list` is called with `order=time` (newest first) per video, so once a thread
older than the cutoff is seen the scan for that video stops rather than
continuing to page through comments already known to be too old.

## Degrades per video, does not fail the whole run

`youtube_read` treats a 403 on a single video's comments (disabled by the
uploader) as a note rather than a failure of the whole read. This op does the
same per video in a multi-video sweep: one video with comments off must not
hide the new comments on every other video in the same run.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _env (#654)
sys.path.insert(0, str(Path(__file__).parent))
from _console import use_utf8_stdout  # noqa: E402
from _env import env_int  # noqa: E402
from _oauth import OAuthError, get_access_token  # noqa: E402
from _sanitize import detect, safe_short  # noqa: E402
from _untrusted import flat  # noqa: E402  (an injection-scan hit is text a stranger chose -- #2671)
from _yt import YouTubeAPIError, authorized  # noqa: E402

USAGE = "youtube_status_since[:ISO]"

#: Per-video cap on how many threads are paged looking for ones newer than
#: the cutoff. `order=time` means the ones that matter are always near the
#: front, and a video with an old comment storm should not make this op scan
#: hundreds of threads it will discard.
_THREADS_PER_VIDEO = 50


def default_cutoff(now: "datetime | None" = None) -> str:
    """24 hours before `now` (UTC), rendered the way YouTube renders
    `publishedAt` -- so a plain string comparison against it is valid."""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(arg: str) -> tuple[str, int]:
    """(cutoff_iso, n_videos)."""
    cutoff = arg.strip() if arg and arg.strip() else default_cutoff()
    n = env_int("SUPERTOOL_DEFAULT_LIMIT", 10, minimum=1)
    return cutoff, min(n, 50)


def new_threads(threads: list[dict], cutoff: str) -> tuple[list[dict], bool]:
    """`(matching, truncated)`. `matching` is every thread whose top-level
    comment's `publishedAt` sorts after `cutoff` (plain string compare, valid
    because both sides are the same zero-padded ISO-8601-with-Z shape).
    `truncated` is True when the page ended while still inside the cutoff
    window, meaning there may be more matching threads this call did not see
    -- said explicitly rather than silently reporting a partial count as
    the whole answer.
    """
    matching = []
    for thread in threads:
        top = (((thread.get("snippet") or {}).get("topLevelComment") or {})
               .get("snippet") or {})
        published = top.get("publishedAt") or ""
        if published > cutoff:
            matching.append(thread)
        else:
            return matching, False
    return matching, len(threads) >= _THREADS_PER_VIDEO


def render(video_title: str, video_id: str, matching: list[dict], truncated: bool) -> str:
    """The receipt for one video's new comments.

    `youtube_read` scans a video's description and its comments for known
    injection patterns and prefixes a `POSSIBLE INJECTION` warning when one
    hits -- untrusted comment text is exactly the same shape here, so this
    runs the same `detect()` scan rather than reusing only the newline-strip
    half of that convention.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    if not matching:
        return None
    all_text = " ".join(
        (((t.get("snippet") or {}).get("topLevelComment") or {})
         .get("snippet") or {}).get("textDisplay", "")
        for t in matching
    )
    inj_hits = detect(all_text)
    warning = ""
    if inj_hits:
        # `detect()` returns the matched substring itself, not a canned
        # pattern name -- one of its own patterns matches across an embedded
        # newline (a bare "system:" line) and that pattern's "\s*" can also
        # absorb a bare "\r" right after it, so an un-flattened hit can put
        # attacker-chosen text at column 0 of this receipt, indistinguishable
        # from a line this tool wrote. Flattened the same way every other
        # untrusted field in this render already is (_sanitize.wrap, read.py).
        flat_hits = [flat(h) for h in inj_hits[:3]]
        warning = (f"  ⚠ POSSIBLE INJECTION in this video's new comments -- "
                   f"{', '.join(flat_hits)}\n")
    lines = [f"{warning}({len(matching)} new comment(s)) {video_title} [{url}]"]
    for thread in matching:
        top = (((thread.get("snippet") or {}).get("topLevelComment") or {})
               .get("snippet") or {})
        author = flat(top.get("authorDisplayName") or "?")
        text = flat(top.get("textDisplay") or "")
        date = (top.get("publishedAt") or "").split("T")[0]
        cid = thread.get("id") or "?"
        lines.append(f"  - {date} {author} (comment_id={cid}): {text[:200]}")
    if truncated:
        lines.append(
            f"  ... more than {_THREADS_PER_VIDEO} threads scanned, there may "
            "be additional matches this call did not see")
    return "\n".join(lines)


def main(arg: str) -> None:
    use_utf8_stdout()
    cutoff, n = parse_args(arg)
    try:
        token = get_access_token()
    except OAuthError as e:
        # Escaped the same way comment.py/auth.py escape this: the OAuth
        # error body is Google's and can carry a newline or a known
        # injection pattern (trap.d/227.oauth-error-body-unescaped-in-receipt.md).
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(2)

    try:
        chan_data = authorized("channels", token,
                               {"part": "contentDetails", "mine": "true"})
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(1)
    channels = chan_data.get("items") or []
    if not channels:
        sys.stderr.write("ERROR: no channel found for this account\n")
        sys.exit(1)
    uploads = (
        (channels[0].get("contentDetails") or {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads:
        sys.stderr.write("ERROR: this account's channel has no uploads playlist\n")
        sys.exit(1)

    try:
        playlist_data = authorized("playlistItems", token, {
            "part": "snippet",
            "playlistId": uploads,
            "maxResults": n,
        })
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(1)
    videos = playlist_data.get("items") or []

    out = []
    degraded = []
    for item in videos:
        snip = item.get("snippet") or {}
        vid = (snip.get("resourceId") or {}).get("videoId")
        title = (snip.get("title") or "?").replace("\n", " ")
        if not vid:
            continue
        try:
            thread_data = authorized("commentThreads", token, {
                "part": "snippet",
                "videoId": vid,
                "order": "time",
                "maxResults": _THREADS_PER_VIDEO,
            })
        except YouTubeAPIError as e:
            degraded.append(f"{title} [{vid}]: {repr(safe_short(str(e), 300))}")
            continue
        matching, truncated = new_threads(thread_data.get("items") or [], cutoff)
        rendered = render(title, vid, matching, truncated)
        if rendered:
            out.append(rendered)

    print(f"youtube_status_since OK since={cutoff} videos_checked={len(videos)}")
    if out:
        print("\n\n".join(out))
    else:
        print("(no new comments since cutoff)")
    for note in degraded:
        print(f"--- comments unavailable: {note} ---")


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
