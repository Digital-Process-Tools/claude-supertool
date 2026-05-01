#!/usr/bin/env python3
"""Bluesky search: bluesky_search:QUERY[|N]

Searches recent posts. Useful for mentions ('max-ai-dev', 'claude-supertool', 'max.dp.tools').
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage bluesky_search:QUERY[|N]\n")
        sys.exit(2)
    parts = arg.split("|")
    query = parts[0]
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return query, min(n, 100)


def render(query: str, posts: list[dict]) -> str:
    if not posts:
        return f"(no results for {query!r})"
    out = [f"({len(posts)} results for {query!r})"]
    for p in posts:
        rec = p.get("record") or {}
        author = p.get("author") or {}
        date = (rec.get("createdAt") or "").split("T")[0]
        text = (rec.get("text") or "").replace("\n", " ")[:160]
        out.append(
            f"- {date} @{author.get('handle','?')} [{p.get('uri','?')}]: {text} "
            f"({p.get('likeCount',0)} likes, {p.get('replyCount',0)} replies)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    query, n = parse_args(arg)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    data = xrpc("app.bsky.feed.searchPosts", session,
                 params={"q": query, "limit": n})
    print(render(query, data.get("posts") or []))


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
