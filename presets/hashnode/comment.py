#!/usr/bin/env python3
"""Hashnode comment: hashnode_comment:POST_ID_OR_URL|MESSAGE[|force]

Accepts a post ID or a Hashnode URL on any publication. URL→ID resolution
uses the cross-publication `publication(host:)` GraphQL field so comments
can be posted on any publication, not just the caller's own.

PRE-FLIGHT DUPLICATE CHECK: Before posting, the op fetches existing comments
on the post and aborts if the authenticated user has already commented (matched
by username from HASHNODE_USERNAME env / me query). Pass |force as the 3rd
pipe-separated field to bypass: hashnode_comment:POST_ID|MSG|force
If the pre-flight check fails, a warning is printed and the comment proceeds
(graceful degrade — don't block on platform issues).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_token
from _graphql import gql, gql_safe
from _outbound import append as track_append
from _resolve import resolve_post_id

ADD_COMMENT = """
mutation AddComment($input: AddCommentInput!) {
  addComment(input: $input) { comment { id dateAdded } }
}
"""

PREFLIGHT = """
query PostComments($id: ID!) {
  post(id: $id) {
    comments(first: 50) {
      edges { node { id author { username } dateAdded } }
    }
  }
}
"""


def parse_args(arg: str) -> tuple[str, str, bool]:
    """Return (post_or_url, message, force)."""
    parts = arg.split("|", 2)
    if len(parts) < 2 or not parts[1].strip():
        sys.stderr.write("ERROR: usage hashnode_comment:POST_ID_OR_URL|MESSAGE[|force]\n")
        sys.exit(2)
    post_or_url = parts[0].strip()
    message = parts[1]
    force = len(parts) > 2 and parts[2].strip().lower() == "force"
    return post_or_url, message, force


def preflight_comment(post_id: str, me: str, token: str) -> tuple[bool | None, list[str], str]:
    """Check if `me` has already commented on post_id.

    Returns (already_commented, existing_ids, last_date) or (None, [], '')
    when the lookup itself failed (caller should fail-closed).
    """
    if not me:
        return None, [], ""
    data = gql_safe(PREFLIGHT, {"id": post_id}, token)
    if data is None:
        return None, [], ""
    post = data.get("post") or {}
    edges = (post.get("comments") or {}).get("edges") or []
    mine = [e["node"] for e in edges
            if (e.get("node") or {}).get("author", {}).get("username") == me]
    if not mine:
        return False, [], ""
    ids = [c["id"] for c in mine]
    last = max(c.get("dateAdded") or "" for c in mine).split("T")[0]
    return True, ids, last


def main(arg: str) -> None:
    post_or_url, message, force = parse_args(arg)
    token = get_token()
    post_id = resolve_post_id(token, post_or_url)
    if not force:
        try:
            from _me import get_username
            me = get_username(token)
        except Exception as exc:
            sys.stderr.write(
                f"ABORT — pre-flight failed (cannot identify user: {exc}). "
                "Use |force as 3rd field to bypass and post anyway.\n"
            )
            sys.exit(1)
        already, ids, last = preflight_comment(post_id, me, token)
        if already is None:
            sys.stderr.write(
                f"ABORT — pre-flight lookup failed for post {post_id} "
                "(cannot verify whether already commented). "
                "Use |force as 3rd field to bypass and post anyway.\n"
            )
            sys.exit(1)
        if already:
            sys.stderr.write(
                f"ABORT — already commented {len(ids)}× on post {post_id} "
                f"(ids: {', '.join(ids)}, last {last}). "
                "Use |force as 3rd field to override.\n"
            )
            sys.exit(1)
    data = gql(ADD_COMMENT, {"input": {"postId": post_id, "contentMarkdown": message}}, token)
    c = data["addComment"]["comment"]
    track_append({
        "comment_id": c["id"],
        "post_id": post_id,
        "parent_id": None,
        "posted_at": c["dateAdded"],
    })
    print(f"(comment posted id={c['id']} at={c['dateAdded']})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    # Supertool {args} passes parts space-separated; rejoin with ':' so a body
    # containing ':' survives the supertool tokenizer.
    arg = ":".join(sys.argv[1:])
    main(arg)
