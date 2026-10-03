#!/usr/bin/env python3
"""Bluesky repost: bluesky_repost:AT_URI_OR_WEB_URL[|force] — boost (retweet) a post."""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _publish_safety
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle
from _publish_safety import require_confirm  # noqa: E402


def to_at_uri(arg: str, session: dict) -> str:
    if arg.startswith("at://"):
        return arg
    if arg.startswith("http"):
        path = urlparse(arg).path.strip("/").split("/")
        if len(path) >= 4 and path[0] == "profile" and path[2] == "post":
            handle, rkey = path[1], path[3]
            profile = xrpc("app.bsky.actor.getProfile", session, params={"actor": handle})
            did = profile.get("did")
            return f"at://{did}/app.bsky.feed.post/{rkey}"
    sys.stderr.write(f"ERROR: cannot parse {arg!r}\n")
    sys.exit(2)


def parse_args(arg: str) -> tuple[str, bool]:
    """Return (uri_or_url, force)."""
    parts = arg.split("|", 1)
    target = parts[0].strip()
    if not target:
        sys.stderr.write("ERROR: usage bluesky_repost:AT_URI_OR_WEB_URL[|force]\n")
        sys.exit(2)
    force = len(parts) > 1 and parts[1].strip().lower() == "force"
    return target, force


def main(arg: str) -> None:
    target, force = parse_args(arg)
    require_confirm("bluesky_repost", f"repost {target}", force=force)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    uri = to_at_uri(target, session)
    thread = xrpc("app.bsky.feed.getPostThread", session, params={"uri": uri, "depth": 0})
    post = (thread.get("thread") or {}).get("post") or {}
    cid = post.get("cid")
    if not cid:
        sys.stderr.write(f"ERROR: cannot resolve post cid for {uri}\n")
        sys.exit(1)
    data = xrpc(
        "com.atproto.repo.createRecord", session, method="POST",
        body={
            "repo": session["did"],
            "collection": "app.bsky.feed.repost",
            "record": {
                "$type": "app.bsky.feed.repost",
                "subject": {"uri": uri, "cid": cid},
                "createdAt": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
        },
    )
    print(f"(reposted uri={uri} repost_uri={data.get('uri','?')})")


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
