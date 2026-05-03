#!/usr/bin/env python3
"""Hashnode reply: hashnode_reply:COMMENT_ID|MESSAGE"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_token
from _graphql import gql
from _outbound import append as track_append

ADD_REPLY = """
mutation AddReply($input: AddReplyInput!) {
  addReply(input: $input) { reply { id dateAdded } }
}
"""

PARENT_POST_QUERY = """
query ParentPost($cid: ID!) {
  comment(id: $cid) { post { id } }
}
"""


def parse_args(arg: str) -> tuple[str, str]:
    parts = arg.split("|", 1)
    if len(parts) < 2 or not parts[1].strip():
        sys.stderr.write("ERROR: usage hashnode_reply:COMMENT_ID|MESSAGE_OR_FILE\n")
        sys.exit(2)
    cid = parts[0].strip()
    message = parts[1]
    # Body file support — symmetric with bluesky_publish: if MESSAGE is a path
    # to an existing file, read its contents. File bodies get stripped so an
    # editor's trailing newline doesn't post as visible whitespace.
    try:
        p = Path(message)
        if p.is_file():
            message = p.read_text().strip()
    except OSError:
        pass
    return cid, message


def main(arg: str) -> None:
    cid, message = parse_args(arg)
    token = get_token()
    # Resolve post_id from parent comment for outbound tracking
    parent_data = gql(PARENT_POST_QUERY, {"cid": cid}, token)
    post_id = ((parent_data.get("comment") or {}).get("post") or {}).get("id")
    data = gql(ADD_REPLY, {"input": {"commentId": cid, "contentMarkdown": message}}, token)
    r = data["addReply"]["reply"]
    track_append({
        "comment_id": r["id"],
        "post_id": post_id,
        "parent_id": cid,
        "posted_at": r["dateAdded"],
    })
    print(f"(reply posted id={r['id']} at={r['dateAdded']})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    # Supertool {args} passes parts space-separated; rejoin with ':' so a body
    # containing ':' survives the supertool tokenizer.
    arg = ":".join(sys.argv[1:])
    main(arg)
