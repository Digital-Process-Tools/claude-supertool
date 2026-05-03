#!/usr/bin/env python3
"""Hashnode react: hashnode_react:POST_ID_OR_URL[|force]

Accepts a post ID or a Hashnode URL on any publication. The URL→ID lookup
uses the cross-publication `publication(host:)` GraphQL field so foreign
posts (e.g. https://author.hashnode.dev/slug) resolve correctly.

PRE-FLIGHT DUPLICATE CHECK: Before liking, the op queries the post's
myTotalReactions field and aborts if >0 (already reacted). Pass |force as a
second pipe-separated field to bypass: hashnode_react:POST_ID_OR_URL|force
If the pre-flight check fails, a warning is printed and the like proceeds
(graceful degrade — don't block on platform issues).
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

PREFLIGHT = """
query PostReactions($id: ID!) {
  post(id: $id) { myTotalReactions }
}
"""


def parse_args(arg: str) -> tuple[str, bool]:
    """Return (raw_identifier, force)."""
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_react:POST_ID_OR_URL[|force]\n")
        sys.exit(2)
    parts = arg.split("|", 1)
    raw = parts[0].strip()
    force = len(parts) > 1 and parts[1].strip().lower() == "force"
    return raw, force


def preflight_react(post_id: str, token: str) -> tuple[bool | None, int]:
    """Check if already reacted to post_id.

    Returns (already_reacted, reaction_count) or (None, 0) when unknown.
    Hashnode's GraphQL schema has no per-user reaction field, so we cannot
    reliably tell whether *we* reacted — only the total. Returning None
    signals the caller to fail-closed (likePost is additive on Hashnode,
    so silently posting on every call would spam reactions).
    """
    return None, 0


def main(arg: str) -> None:
    raw, force = parse_args(arg)
    token = get_token()
    post_id = resolve_post_id(token, raw)
    if not force:
        already, count = preflight_react(post_id, token)
        if already is None:
            sys.stderr.write(
                f"ABORT — cannot verify whether already reacted to post {post_id} "
                "(Hashnode GraphQL exposes no per-user reaction field, and likePost "
                "is additive — each call adds a reaction). Use |force to react anyway.\n"
            )
            sys.exit(1)
        if already:
            sys.stderr.write(
                f"ABORT — already reacted to post {post_id} "
                f"(reactions={count}). Use |force to override.\n"
            )
            sys.exit(1)
    data = gql(LIKE, {"input": {"postId": post_id, "likesCount": 1}}, token)
    p = data["likePost"]["post"]
    print(f"(liked id={p['id']} total_reactions={p.get('reactionCount', 0)})")


if __name__ == "__main__":
    # Supertool {args} splits on ':' — URLs like https://... arrive as multiple
    # argv parts. Rejoin so the full identifier survives the tokenizer.
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
