#!/usr/bin/env python3
"""Bluesky like: bluesky_like:AT_URI_OR_WEB_URL[|force]

PRE-FLIGHT DUPLICATE CHECK: Before liking, the op scans own actor likes via
app.bsky.feed.getActorLikes and aborts if the target URI is already liked.
Pass |force as 2nd pipe-separated field to bypass: bluesky_like:URI|force
If the pre-flight check fails, a warning is printed and the like proceeds
(graceful degrade — don't block on platform issues).
"""
import datetime as _dt
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle


def parse_args(arg: str) -> tuple[str, bool]:
    """Return (raw_uri_or_url, force)."""
    if not arg:
        sys.stderr.write("ERROR: usage bluesky_like:AT_URI_OR_WEB_URL[|force]\n")
        sys.exit(2)
    parts = arg.split("|", 1)
    raw = parts[0].strip()
    force = len(parts) > 1 and parts[1].strip().lower() == "force"
    return raw, force


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


def preflight_like(target_uri: str, session: dict) -> bool:
    """Return True if own DID has already liked target_uri.

    Scans up to 100 own likes. Returns False on any API error (graceful degrade).
    """
    try:
        resp = xrpc("app.bsky.feed.getActorLikes", session,
                    params={"actor": session["did"], "limit": 100})
        for item in resp.get("feed") or []:
            post = (item.get("post") or {})
            if post.get("uri") == target_uri:
                return True
        return False
    except Exception:
        return False


def main(arg: str) -> None:
    raw, force = parse_args(arg)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    uri = to_at_uri(raw, session)
    if not force:
        try:
            if preflight_like(uri, session):
                sys.stderr.write(
                    f"ABORT — already liked {uri}. Use |force to override.\n"
                )
                sys.exit(1)
        except Exception as exc:
            sys.stderr.write(f"WARNING: pre-flight check failed ({exc}) — proceeding anyway.\n")
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
