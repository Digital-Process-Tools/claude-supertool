#!/usr/bin/env python3
"""Hashnode react: hashnode_react:POST_ID_OR_URL — likes the post."""
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql

RESOLVE_QUERY = """
query Resolve($publicationId: ObjectId!, $slug: String!) {
  publication(id: $publicationId) { post(slug: $slug) { id } }
}
"""

LIKE = """
mutation LikePost($input: LikePostInput!) {
  likePost(input: $input) { post { id reactionCount } }
}
"""


def resolve_post_id(token: str, arg: str) -> str:
    if arg.startswith("http"):
        slug = urlparse(arg).path.strip("/").split("/")[-1]
        pub_id = get_publication_id()
        data = gql(RESOLVE_QUERY, {"publicationId": pub_id, "slug": slug}, token)
        post = (data.get("publication") or {}).get("post")
        if not post:
            sys.stderr.write(f"ERROR: post not found for {arg}\n")
            sys.exit(1)
        return post["id"]
    return arg


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
    main(sys.argv[1] if len(sys.argv) > 1 else "")
