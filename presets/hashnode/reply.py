#!/usr/bin/env python3
"""Hashnode reply: hashnode_reply:COMMENT_ID|MESSAGE_OR_FILE"""
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

_FILE_PREFIX = "file://"


def _resolve_body(arg: str) -> tuple[str, bool]:
    """Resolve a body argument to its text content. Returns (text, from_file).

    - `file://path` MUST be an existing file. Errors otherwise.
    - bare path that `is_file()` reads the file (backward compat).
    - anything else returned as-is.
    """
    if arg.startswith(_FILE_PREFIX):
        path = arg[len(_FILE_PREFIX):]
        p = Path(path)
        if not p.is_file():
            sys.stderr.write(
                f"ERROR: file not found: {path}\n"
                "(file:// prefix requires the file to exist — typo or wrong path?)\n"
            )
            sys.exit(2)
        return p.read_text(), True
    try:
        p = Path(arg)
        if p.is_file():
            return p.read_text(), True
    except OSError:
        pass
    return arg, False


def parse_args(arg: str) -> tuple[str, str]:
    parts = arg.split("|", 1)
    if len(parts) < 2 or not parts[1].strip():
        sys.stderr.write("ERROR: usage hashnode_reply:COMMENT_ID|MESSAGE_OR_FILE\n")
        sys.exit(2)
    cid = parts[0].strip()
    text, from_file = _resolve_body(parts[1])
    message = text.strip() if from_file else text
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
