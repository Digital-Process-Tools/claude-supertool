#!/usr/bin/env python3
"""YouTube read: youtube_read:VIDEO_ID_OR_URL

Aggregates: video title + channel + stats + top N comments (with comment IDs)
+ NEXT hint. Accepts a bare video ID or a watch/youtu.be/shorts/embed URL.

Comments come from commentThreads.list, a separate call from videos.list --
a video with comments disabled (403) degrades to a note rather than failing
the whole read, since the video metadata is still perfectly readable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _env (#654)
sys.path.insert(0, str(Path(__file__).parent))
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
from _auth import get_api_key  # noqa: E402
from _sanitize import detect, wrap as wrap_untrusted  # noqa: E402
from _yt import YouTubeAPIError, get  # noqa: E402


def parse_video_id(arg: str) -> str:
    if not arg:
        sys.stderr.write("ERROR: usage youtube_read:VIDEO_ID_OR_URL\n")
        sys.exit(2)
    if not arg.startswith("http"):
        return arg
    parsed = urlparse(arg)
    if parsed.path == "/watch":
        vid = parse_qs(parsed.query).get("v", [None])[0]
        if vid:
            return vid
    path = parsed.path.strip("/")
    for prefix in ("shorts/", "embed/", "v/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    if parsed.hostname and "youtu.be" in parsed.hostname and path:
        return path
    sys.stderr.write(f"ERROR: cannot parse video id from {arg!r}\n")
    sys.exit(2)


def render(video: dict, comments: list[dict], comments_note: str, inline_n: int) -> str:
    snip = video.get("snippet") or {}
    stats = video.get("statistics") or {}
    # channelTitle/title/authorDisplayName are all free text chosen by a
    # channel owner or commenter -- an unstripped newline reaches column 0
    # of a new output line and can forge a fake row/section boundary (#227
    # self-review).
    title = (snip.get("title") or "?").replace("\n", " ")
    channel = (snip.get("channelTitle") or "?").replace("\n", " ")
    date = (snip.get("publishedAt") or "").split("T")[0]
    description = snip.get("description") or ""
    head = (
        f"(video id={video.get('id','?')})\n"
        f"TITLE:    {title}\n"
        f"CHANNEL:  {channel}\n"
        f"DATE:     {date}\n"
        f"STATS:    {stats.get('viewCount','?')} views, {stats.get('likeCount','?')} likes, "
        f"{stats.get('commentCount','?')} comments"
    )
    if comments_note:
        comments_section = comments_note
    elif comments:
        cblock = [f"--- top {min(len(comments), inline_n)} comments ---"]
        for c in comments[:inline_n]:
            top = (c.get("snippet") or {}).get("topLevelComment") or {}
            csnip = top.get("snippet") or {}
            text = (csnip.get("textDisplay") or "").replace("\n", " ")[:200]
            author = (csnip.get("authorDisplayName") or "?").replace("\n", " ")
            cid = top.get("id") or "?"
            cblock.append(f"  [id={cid}] {author}: {text}")
        comments_section = "\n".join(cblock)
    else:
        comments_section = "--- 0 comments ---"
    nxt = (
        f"--- NEXT ---\n"
        f"  https://www.youtube.com/watch?v={video.get('id','VIDEO_ID')}   — open in browser"
    )
    all_text = description + " " + " ".join(
        ((c.get("snippet") or {}).get("topLevelComment") or {}).get(
            "snippet", {}).get("textDisplay", "")
        for c in comments
    )
    inj_hits = detect(all_text)
    warning = ""
    if inj_hits:
        warning = f"⚠ POSSIBLE INJECTION in this video's text — {', '.join(inj_hits[:3])}\n"
    desc_wrapped = wrap_untrusted(description, source="youtube-video")
    return f"{warning}{head}\n--- description ---\n{desc_wrapped}\n{comments_section}\n{nxt}"


def main(arg: str) -> None:
    video_id = parse_video_id(arg)
    api_key = get_api_key()
    try:
        data = get("videos", api_key, {
            "part": "snippet,statistics",
            "id": video_id,
        })
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    items = data.get("items") or []
    if not items:
        sys.stderr.write(f"ERROR: video not found: {arg}\n")
        sys.exit(1)
    inline_n = env_int("SUPERTOOL_INLINE_COMMENTS", 5, minimum=0)
    comments: list[dict] = []
    comments_note = ""
    if inline_n > 0:
        try:
            cdata = get("commentThreads", api_key, {
                "part": "snippet",
                "videoId": video_id,
                "order": "relevance",
                "maxResults": inline_n,
            })
            comments = cdata.get("items") or []
        except YouTubeAPIError as e:
            comments_note = f"--- comments unavailable: {e.message} ---"
    print(render(items[0], comments, comments_note, inline_n))


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
