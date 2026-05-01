#!/usr/bin/env python3
"""Bluesky follow: bluesky_follow:HANDLE_OR_DID"""
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle


def main(arg: str) -> None:
    target = arg.strip().lstrip("@")
    if not target:
        sys.stderr.write("ERROR: usage bluesky_follow:HANDLE_OR_DID\n")
        sys.exit(2)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    did = target if target.startswith("did:") else None
    if not did:
        profile = xrpc("app.bsky.actor.getProfile", session, params={"actor": target})
        did = profile.get("did")
        if not did:
            sys.stderr.write(f"ERROR: cannot resolve handle {target}\n")
            sys.exit(1)
    data = xrpc(
        "com.atproto.repo.createRecord", session, method="POST",
        body={
            "repo": session["did"],
            "collection": "app.bsky.graph.follow",
            "record": {
                "$type": "app.bsky.graph.follow",
                "subject": did,
                "createdAt": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
        },
    )
    print(f"(followed @{target} did={did} follow_uri={data.get('uri','?')})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
