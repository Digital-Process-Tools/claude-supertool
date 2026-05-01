#!/usr/bin/env python3
"""Bluesky list: bluesky_list[:N] (own feed) | bluesky_list:HANDLE[|N]

N defaults to SUPERTOOL_DEFAULT_LIMIT or 10.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle


def parse_args(arg: str) -> tuple[str | None, int]:
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    if not arg or arg.isdigit():
        return None, int(arg) if arg else default_n
    parts = arg.split("|")
    actor = parts[0]
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return actor, n


def render(posts: list[dict]) -> str:
    if not posts:
        return "(no posts)"
    out = [f"({len(posts)} posts)"]
    for item in posts:
        post = item.get("post") or {}
        rec = post.get("record") or {}
        author = post.get("author") or {}
        date = (rec.get("createdAt") or "").split("T")[0]
        text = (rec.get("text") or "").replace("\n", " ")[:160]
        out.append(
            f"- {date} @{author.get('handle','?')}: {text} "
            f"({post.get('replyCount', 0)} replies, {post.get('likeCount', 0)} likes)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    actor, n = parse_args(arg)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    target = actor if actor else session["did"]
    data = xrpc("app.bsky.feed.getAuthorFeed", session,
                 params={"actor": target, "limit": min(n, 100)})
    print(render(data.get("feed") or []))


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
