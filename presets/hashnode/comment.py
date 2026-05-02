#!/usr/bin/env python3
"""Hashnode comment: hashnode_comment:POST_ID_OR_URL|MESSAGE

Accepts a post ID or a Hashnode URL on any publication. URL→ID resolution
uses the cross-publication `publication(host:)` GraphQL field so comments
can be posted on any publication, not just the caller's own.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_token
from _graphql import gql
from _outbound import append as track_append
from _resolve import resolve_post_id

ADD_COMMENT = """
mutation AddComment($input: AddCommentInput!) {
  addComment(input: $input) { comment { id dateAdded } }
}
"""


def parse_args(arg: str) -> tuple[str, str]:
    parts = arg.split("|", 1)
    if len(parts) < 2 or not parts[1].strip():
        sys.stderr.write("ERROR: usage hashnode_comment:POST_ID_OR_URL|MESSAGE\n")
        sys.exit(2)
    return parts[0].strip(), parts[1]


def main(arg: str) -> None:
    post_or_url, message = parse_args(arg)
    token = get_token()
    post_id = resolve_post_id(token, post_or_url)
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
