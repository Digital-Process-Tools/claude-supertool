#!/usr/bin/env python3
"""Hashnode publish: TITLE|MD_FILE|CANONICAL[|TAGS|COVER]

Tags: comma-separated slugs. Cover: URL.
Defaults from manifest: SUPERTOOL_DEFAULT_TAGS, SUPERTOOL_DEFAULT_COVER (env).
Caller-supplied values win.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql

QUERY = """
mutation PublishPost($input: PublishPostInput!) {
  publishPost(input: $input) {
    post { id slug url title }
  }
}
"""


def parse_args(arg: str) -> dict[str, object]:
    parts = arg.split("|")
    if len(parts) < 3:
        sys.stderr.write("ERROR: usage hashnode_publish:TITLE|MD_FILE|CANONICAL[|TAGS|COVER]\n")
        sys.exit(2)
    title = parts[0].strip()
    md_path = Path(parts[1].strip())
    canonical = parts[2].strip()
    tags_csv = parts[3].strip() if len(parts) > 3 and parts[3].strip() else os.environ.get("SUPERTOOL_DEFAULT_TAGS", "")
    cover = parts[4].strip() if len(parts) > 4 and parts[4].strip() else os.environ.get("SUPERTOOL_DEFAULT_COVER", "")
    if not md_path.is_file():
        sys.stderr.write(f"ERROR: markdown file not found: {md_path}\n")
        sys.exit(2)
    tags = [{"slug": s.strip(), "name": s.strip().replace("-", " ").title()}
            for s in tags_csv.split(",") if s.strip()]
    return {
        "title": title,
        "markdown": md_path.read_text(),
        "canonical": canonical,
        "tags": tags,
        "cover": cover,
    }


def build_input(parsed: dict[str, object], publication_id: str) -> dict[str, object]:
    inp: dict[str, object] = {
        "publicationId": publication_id,
        "title": parsed["title"],
        "contentMarkdown": parsed["markdown"],
        "originalArticleURL": parsed["canonical"],
    }
    if parsed["tags"]:
        inp["tags"] = parsed["tags"]
    if parsed["cover"]:
        inp["coverImageOptions"] = {"coverImageURL": parsed["cover"]}
    return inp


def main(arg: str) -> None:
    parsed = parse_args(arg)
    token = get_token()
    pub_id = get_publication_id()
    data = gql(QUERY, {"input": build_input(parsed, pub_id)}, token)
    post = data["publishPost"]["post"]
    print(f"(published id={post['id']} slug={post['slug']})")
    print(f"URL:   {post['url']}")
    print(f"TITLE: {post['title']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    main(sys.argv[1])
