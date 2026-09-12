#!/usr/bin/env python3
"""YouTube list: youtube_list:CHANNEL[|N]

CHANNEL accepts a channel ID (UC..., 24 chars), an @handle, a bare name
(tried as an @handle -- YouTube's legacy /c/ and /user/ custom URLs carry no
stable ID of their own), or a full https://www.youtube.com/channel/... or
https://www.youtube.com/@... URL.

Lists the channel's uploads via playlistItems.list against its uploads
playlist: one channels.list call resolves CHANNEL to that playlist id (1
unit), one playlistItems.list call reads it (1 unit) -- 2 units total,
cheaper than search.list's 100.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _env (#654)
sys.path.insert(0, str(Path(__file__).parent))
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
from _auth import get_api_key  # noqa: E402
from _yt import YouTubeAPIError, get  # noqa: E402


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage youtube_list:CHANNEL[|N]\n")
        sys.exit(2)
    parts = arg.split("|")
    channel = parts[0]
    default_n = env_int("SUPERTOOL_DEFAULT_LIMIT", 10, minimum=1)
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return channel, min(n, 50)


def parse_channel_ref(raw: str) -> tuple[str, str]:
    """Return (param_name, value) for channels.list -- 'id' or 'forHandle'."""
    raw = raw.strip()
    if raw.startswith("http"):
        path = urlparse(raw).path.strip("/")
        # A URL copied from a browser tab carries a trailing segment this
        # preset never asked for -- /channel/UCabc123/videos,
        # /@handle/about -- so only the first path segment is ever the
        # channel/handle identifier; everything after it is stripped (#227
        # self-review: the un-stripped form silently fails the
        # channels.list lookup for the whole channel, not just its /videos
        # tab).
        first, _, _rest = path.partition("/")
        if first == "channel":
            _, _, second = path.partition("/")
            channel_id, _, _rest2 = second.partition("/")
            return "id", channel_id
        if first.startswith("@"):
            return "forHandle", first
        if "/" in path:
            # /c/NAME or /user/NAME -- legacy custom URLs carry no stable ID;
            # best-effort as a handle lookup, first segment only.
            _, _, rest = path.partition("/")
            name, _, _rest2 = rest.partition("/")
            return "forHandle", "@" + name
        return ("forHandle", "@" + path) if path else ("forHandle", "")
    if raw.startswith("UC") and len(raw) == 24:
        return "id", raw
    if raw.startswith("@"):
        return "forHandle", raw
    return "forHandle", "@" + raw


def render(items: list[dict]) -> str:
    if not items:
        return "(no videos)"
    out = [f"({len(items)} videos)"]
    for item in items:
        snip = item.get("snippet") or {}
        vid = (snip.get("resourceId") or {}).get("videoId") or "?"
        title = (snip.get("title") or "?").replace("\n", " ")
        date = (snip.get("publishedAt") or "").split("T")[0]
        url = f"https://www.youtube.com/watch?v={vid}"
        out.append(f"- {date} {title} [{url}]")
    return "\n".join(out)


def main(arg: str) -> None:
    channel, n = parse_args(arg)
    api_key = get_api_key()
    param_name, value = parse_channel_ref(channel)
    if not value:
        sys.stderr.write(f"ERROR: cannot parse channel reference {channel!r}\n")
        sys.exit(2)
    try:
        chan_data = get("channels", api_key, {
            "part": "contentDetails",
            param_name: value,
        })
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    channels = chan_data.get("items") or []
    if not channels:
        sys.stderr.write(f"ERROR: no channel found for {channel!r}\n")
        sys.exit(1)
    uploads = (
        (channels[0].get("contentDetails") or {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads:
        sys.stderr.write(f"ERROR: channel {channel!r} has no uploads playlist\n")
        sys.exit(1)
    try:
        data = get("playlistItems", api_key, {
            "part": "snippet",
            "playlistId": uploads,
            "maxResults": n,
        })
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    print(render(data.get("items") or []))


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
