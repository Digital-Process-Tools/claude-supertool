#!/usr/bin/env python3
"""YouTube search: youtube_search:QUERY[|N]

Searches for videos matching QUERY via search.list (type=video, so results are
always a watchable video rather than a channel or playlist). API-key auth
only -- read op, no OAuth2. Quota cost: 100 units per call, the most
expensive endpoint in this API and the only full-text search surface it has.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _env (#654)
sys.path.insert(0, str(Path(__file__).parent))
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
from _auth import get_api_key  # noqa: E402
from _yt import YouTubeAPIError, get  # noqa: E402


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage youtube_search:QUERY[|N]\n")
        sys.exit(2)
    parts = arg.split("|")
    query = parts[0]
    default_n = env_int("SUPERTOOL_DEFAULT_LIMIT", 10, minimum=1)
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return query, min(n, 50)


def render(query: str, items: list[dict]) -> str:
    if not items:
        return f"(no results for {query!r})"
    out = [f"({len(items)} results for {query!r})"]
    for it in items:
        vid = (it.get("id") or {}).get("videoId") or "?"
        snip = it.get("snippet") or {}
        title = (snip.get("title") or "?").replace("\n", " ")
        # channelTitle is free text a channel owner chooses, same as title
        # above -- an unstripped newline reaches column 0 of a new output
        # line and can forge a fake row boundary (#227 self-review).
        channel = (snip.get("channelTitle") or "?").replace("\n", " ")
        date = (snip.get("publishedAt") or "").split("T")[0]
        url = f"https://www.youtube.com/watch?v={vid}"
        out.append(f"- {date} {title} — {channel} [{url}]")
    return "\n".join(out)


def main(arg: str) -> None:
    query, n = parse_args(arg)
    api_key = get_api_key()
    try:
        data = get("search", api_key, {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": n,
        })
    except YouTubeAPIError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    print(render(query, data.get("items") or []))


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
