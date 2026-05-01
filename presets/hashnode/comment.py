#!/usr/bin/env python3
"""Hashnode comment: hashnode_comment:POST_ID_OR_URL|MESSAGE"""
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


def resolve_post_id(token: str, post_or_url: str) -> str:
    if post_or_url.startswith("http"):
        slug = urlparse(post_or_url).path.strip("/").split("/")[-1]
        pub_id = get_publication_id()
        data = gql(RESOLVE_QUERY, {"publicationId": pub_id, "slug": slug}, token)
        post = (data.get("publication") or {}).get("post")
        if not post:
            sys.stderr.write(f"ERROR: post not found for {post_or_url}\n")
            sys.exit(1)
        return post["id"]
    return post_or_url


def main(arg: str) -> None:
    post_or_url, message = parse_args(arg)
    token = get_token()
    post_id = resolve_post_id(token, post_or_url)
    data = gql(ADD_COMMENT, {"input": {"postId": post_id, "contentMarkdown": message}}, token)
    c = data["addComment"]["comment"]
    print(f"(comment posted id={c['id']} at={c['dateAdded']})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    main(sys.argv[1])
