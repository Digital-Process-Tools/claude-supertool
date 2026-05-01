#!/usr/bin/env python3
"""Bluesky publish: bluesky_publish:TEXT_FILE_OR_TEXT[|REPLY_TO_AT_URI]

If the first arg is a path to a file that exists, its contents become
the post body. Otherwise the arg is treated as inline text. Bluesky
posts are limited to 300 characters by the AT Protocol — we error if
the body exceeds that, since the API will silently truncate otherwise.

Optional second arg: AT URI of a post to reply to
(at://DID/app.bsky.feed.post/POST_ID). Builds the reply ref correctly
(parent + root via getPostThread).
"""
import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle

MAX_LEN = 300


def parse_args(arg: str) -> tuple[str, str | None]:
    parts = arg.split("|", 1)
    if not parts[0].strip():
        sys.stderr.write("ERROR: usage bluesky_publish:TEXT_OR_FILE[|REPLY_TO_AT_URI]\n")
        sys.exit(2)
    body = parts[0]
    try:
        p = Path(body)
        is_file = p.is_file()
    except OSError:
        # Path too long for filesystem — treat as inline text
        is_file = False
    if is_file:
        body = Path(body).read_text()
    body = body.strip()
    if len(body) > MAX_LEN:
        sys.stderr.write(f"ERROR: post is {len(body)} chars (max {MAX_LEN}). Trim or split.\n")
        sys.exit(2)
    reply_uri = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return body, reply_uri


def resolve_reply_ref(session: dict, reply_to_uri: str) -> dict:
    """Build the reply ref: needs parent + root cid+uri."""
    thread = xrpc("app.bsky.feed.getPostThread", session,
                   params={"uri": reply_to_uri, "depth": 0})
    post = (thread.get("thread") or {}).get("post") or {}
    parent_ref = {"uri": post.get("uri"), "cid": post.get("cid")}
    record = post.get("record") or {}
    parent_reply = record.get("reply") or {}
    root_ref = parent_reply.get("root") or parent_ref  # if parent is itself the root
    return {"parent": parent_ref, "root": root_ref}


def main(arg: str) -> None:
    body, reply_uri = parse_args(arg)
    handle = get_handle()
    session = get_session(handle, get_app_password())
    record = {
        "$type": "app.bsky.feed.post",
        "text": body,
        "createdAt": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    if reply_uri:
        record["reply"] = resolve_reply_ref(session, reply_uri)
    data = xrpc(
        "com.atproto.repo.createRecord", session, method="POST",
        body={
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
    )
    uri = data.get("uri", "?")
    cid = data.get("cid", "?")
    # Convert AT URI → web URL: at://DID/app.bsky.feed.post/RKEY
    rkey = uri.split("/")[-1] if uri.startswith("at://") else "?"
    web = f"https://bsky.app/profile/{handle}/post/{rkey}"
    print(f"(published uri={uri} cid={cid})")
    print(f"URL: {web}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    main(":".join(sys.argv[1:]))
