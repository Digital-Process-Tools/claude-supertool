#!/usr/bin/env python3
"""Bluesky read: bluesky_read:AT_URI_OR_WEB_URL

Aggregates: post body + author + stats + top N replies (with URIs) + NEXT chain hints.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle


def parse_arg(arg: str, session: dict) -> str:
    """Return AT URI given either an at:// URI or a https://bsky.app/profile/HANDLE/post/RKEY URL."""
    if not arg:
        sys.stderr.write("ERROR: usage bluesky_read:AT_URI_OR_WEB_URL\n")
        sys.exit(2)
    if arg.startswith("at://"):
        return arg
    if arg.startswith("http"):
        path = urlparse(arg).path.strip("/").split("/")
        # /profile/HANDLE/post/RKEY
        if len(path) >= 4 and path[0] == "profile" and path[2] == "post":
            handle, rkey = path[1], path[3]
            profile = xrpc("app.bsky.actor.getProfile", session, params={"actor": handle})
            did = profile.get("did")
            if not did:
                sys.stderr.write(f"ERROR: cannot resolve handle {handle}\n")
                sys.exit(1)
            return f"at://{did}/app.bsky.feed.post/{rkey}"
    sys.stderr.write(f"ERROR: cannot parse {arg!r}\n")
    sys.exit(2)


def render(thread: dict, inline_n: int) -> str:
    post = thread.get("post") or {}
    rec = post.get("record") or {}
    author = post.get("author") or {}
    date = (rec.get("createdAt") or "").split("T")[0]
    body = rec.get("text") or ""
    head = (
        f"(post uri={post.get('uri','?')})\n"
        f"AUTHOR:   {author.get('displayName','?')} (@{author.get('handle','?')})\n"
        f"DATE:     {date}\n"
        f"STATS:    {post.get('likeCount',0)} likes, {post.get('replyCount',0)} replies, {post.get('repostCount',0)} reposts"
    )
    replies = thread.get("replies") or []
    if replies:
        rblock = [f"--- top {min(len(replies), inline_n)} replies ---"]
        for r in replies[:inline_n]:
            rp = r.get("post") or {}
            rrec = rp.get("record") or {}
            rauth = rp.get("author") or {}
            rtext = (rrec.get("text") or "").replace("\n", " ")[:200]
            rblock.append(f"  [uri={rp.get('uri','?')}] @{rauth.get('handle','?')}: {rtext}")
        replies_section = "\n".join(rblock)
    else:
        replies_section = "--- 0 replies ---"
    nxt = (
        f"--- NEXT ---\n"
        f"  bluesky_like:{post.get('uri','URI')}                 — like this post\n"
        f"  bluesky_publish:\"MSG\"|{post.get('uri','URI')}        — reply to this post\n"
        f"  bluesky_follow:{author.get('handle','HANDLE')}        — follow author"
    )
    return f"{head}\n--- body ---\n{body}\n{replies_section}\n{nxt}"


def main(arg: str) -> None:
    handle = get_handle()
    session = get_session(handle, get_app_password())
    uri = parse_arg(arg, session)
    inline_n = int(os.environ.get("SUPERTOOL_INLINE_COMMENTS", "5"))
    data = xrpc("app.bsky.feed.getPostThread", session,
                 params={"uri": uri, "depth": 1})
    thread = data.get("thread") or {}
    if not thread.get("post"):
        sys.stderr.write(f"ERROR: post not found: {arg}\n")
        sys.exit(1)
    print(render(thread, inline_n))


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
