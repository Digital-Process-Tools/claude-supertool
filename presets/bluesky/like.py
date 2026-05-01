#!/usr/bin/env python3
"""Bluesky like: bluesky_like:AT_URI_OR_WEB_URL"""
import datetime as _dt
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle


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


def main(arg: str) -> None:
    if not arg:
        sys.stderr.write("ERROR: usage bluesky_like:AT_URI_OR_WEB_URL\n")
        sys.exit(2)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    uri = to_at_uri(arg, session)
    # Need post cid for the like record subject
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
            "collection": "app.bsky.feed.like",
            "record": {
                "$type": "app.bsky.feed.like",
                "subject": {"uri": uri, "cid": cid},
                "createdAt": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
        },
    )
    print(f"(liked uri={uri} like_uri={data.get('uri','?')})")


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
