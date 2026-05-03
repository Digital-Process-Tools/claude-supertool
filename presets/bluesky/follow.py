#!/usr/bin/env python3
"""Bluesky follow: bluesky_follow:HANDLE_OR_DID[|force]

PRE-FLIGHT DUPLICATE CHECK: Before following, the op scans own follows via
app.bsky.graph.getFollows and aborts if target DID is already followed.
Pass |force as 2nd pipe-separated field to bypass: bluesky_follow:HANDLE|force
If the pre-flight check fails, a warning is printed and the follow proceeds
(graceful degrade — don't block on platform issues).
"""
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle


def parse_args(arg: str) -> tuple[str, bool]:
    """Return (handle_or_did, force)."""
    parts = arg.split("|", 1)
    target = parts[0].strip().lstrip("@")
    if not target:
        sys.stderr.write("ERROR: usage bluesky_follow:HANDLE_OR_DID[|force]\n")
        sys.exit(2)
    force = len(parts) > 1 and parts[1].strip().lower() == "force"
    return target, force


def preflight_follow(target_did: str, session: dict) -> bool:
    """Return True if own DID already follows target_did.

    Scans up to 100 own follows. Returns False on any API error (graceful degrade).
    """
    try:
        resp = xrpc("app.bsky.graph.getFollows", session,
                    params={"actor": session["did"], "limit": 100})
        for follow in resp.get("follows") or []:
            if follow.get("did") == target_did:
                return True
        return False
    except Exception:
        return False


def main(arg: str) -> None:
    target, force = parse_args(arg)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    did = target if target.startswith("did:") else None
    if not did:
        profile = xrpc("app.bsky.actor.getProfile", session, params={"actor": target})
        did = profile.get("did")
        if not did:
            sys.stderr.write(f"ERROR: cannot resolve handle {target}\n")
            sys.exit(1)
    if not force:
        try:
            if preflight_follow(did, session):
                sys.stderr.write(
                    f"ABORT — already following {target} (did={did}). Use |force to override.\n"
                )
                sys.exit(1)
        except Exception as exc:
            sys.stderr.write(f"WARNING: pre-flight check failed ({exc}) — proceeding anyway.\n")
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
