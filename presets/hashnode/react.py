#!/usr/bin/env python3
"""Hashnode react: hashnode_react:POST_ID_OR_URL — likes the post.

Accepts a post ID or a Hashnode URL on any publication. The URL→ID lookup
uses the cross-publication `publication(host:)` GraphQL field so foreign
posts (e.g. https://author.hashnode.dev/slug) resolve correctly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_token
from _graphql import gql
from _resolve import resolve_post_id

LIKE = """
mutation LikePost($input: LikePostInput!) {
  likePost(input: $input) { post { id reactionCount } }
}
"""


def main(arg: str) -> None:
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_react:POST_ID_OR_URL\n")
        sys.exit(2)
    token = get_token()
    post_id = resolve_post_id(token, arg)
    data = gql(LIKE, {"input": {"postId": post_id, "likesCount": 1}}, token)
    p = data["likePost"]["post"]
    print(f"(liked id={p['id']} total_reactions={p.get('reactionCount', 0)})")


if __name__ == "__main__":
    # Supertool {args} splits on ':' — URLs like https://... arrive as multiple
    # argv parts. Rejoin so the full identifier survives the tokenizer.
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
