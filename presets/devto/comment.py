#!/usr/bin/env python3
"""Dev.to comment: devto_comment:ARTICLE_ID|MESSAGE[|PARENT_COMMENT_ID]

SESSION-COOKIE ONLY. The Dev.to public API does not expose a comment
write endpoint, so this op requires DEVTO_SESSION_COOKIE (env or
~/.config/devto/session_cookie). See _session.py for the ToS notice.

PARENT_COMMENT_ID makes it a reply (numeric id_code from
devto_comments output, NOT the alphanumeric short slug).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _outbound import append as track_append
from _session import fetch_csrf_token, get_session_cookie, web_post_json


def parse_args(arg: str) -> tuple[str, str, str | None]:
    parts = arg.split("|")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        sys.stderr.write("ERROR: usage devto_comment:ARTICLE_ID|MESSAGE[|PARENT_COMMENT_ID]\n")
        sys.exit(2)
    aid = parts[0].strip()
    message = parts[1]
    parent = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    return aid, message, parent


def main(arg: str) -> None:
    aid, message, parent = parse_args(arg)
    cookie = get_session_cookie()
    if not cookie:
        sys.stderr.write(
            "ERROR: devto_comment requires DEVTO_SESSION_COOKIE (env or "
            "~/.config/devto/session_cookie). The Dev.to API has no public "
            "comment-write endpoint — see _session.py for opt-in instructions.\n"
        )
        sys.exit(2)
    csrf = fetch_csrf_token(cookie)
    body: dict[str, object] = {
        "comment": {
            "body_markdown": message,
            "commentable_id": int(aid) if aid.isdigit() else aid,
            "commentable_type": "Article",
        }
    }
    if parent:
        body["comment"]["parent_id"] = int(parent) if parent.isdigit() else parent  # type: ignore[index]
    text, status = web_post_json("/comments", cookie, csrf, body)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict) and data.get("error"):
        sys.stderr.write(f"ERROR: Dev.to: {data['error']}\n")
        sys.exit(1)
    cid = data.get("id_code") or data.get("id") or "?"
    url = data.get("path") or data.get("url") or ""
    import datetime as _dt
    track_append({
        "comment_id": cid,
        "article_id": int(aid) if aid.isdigit() else aid,
        "parent_id": parent,
        "posted_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    print(f"(comment posted id={cid} url={url} mode=session)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    main(sys.argv[1])
