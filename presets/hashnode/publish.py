#!/usr/bin/env python3
"""Hashnode publish: TITLE|MD_FILE|CANONICAL[|TAGS|COVER[|force]]

Tags: comma-separated slugs. Cover: URL.
Defaults from manifest: SUPERTOOL_DEFAULT_TAGS, SUPERTOOL_DEFAULT_COVER (env).
Caller-supplied values win.

PRE-FLIGHT DUPLICATE CHECK: Before publishing, the op queries
me { posts(first:50) } and aborts if a post with the same canonical URL
already exists. Pass |force as the 6th pipe-separated field to bypass:
TITLE|MD|CANONICAL||||force
If the pre-flight check fails, a warning is printed and publish proceeds.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql, gql_safe

QUERY = """
mutation PublishPost($input: PublishPostInput!) {
  publishPost(input: $input) {
    post { id slug url title }
  }
}
"""

PREFLIGHT = """
query MyPublicationPosts {
  me {
    publications(first: 1) {
      edges { node { posts(first: 50) { edges { node { id slug url canonicalUrl } } } } }
    }
  }
}
"""


def parse_args(arg: str) -> dict[str, object]:
    parts = arg.split("|")
    if len(parts) < 3:
        sys.stderr.write("ERROR: usage hashnode_publish:TITLE|MD_FILE|CANONICAL[|TAGS|COVER[|force]]\n")
        sys.exit(2)
    title = parts[0].strip()
    md_path = Path(parts[1].strip())
    canonical = parts[2].strip()
    tags_csv = parts[3].strip() if len(parts) > 3 and parts[3].strip() else os.environ.get("SUPERTOOL_DEFAULT_TAGS", "")
    cover = parts[4].strip() if len(parts) > 4 and parts[4].strip() else os.environ.get("SUPERTOOL_DEFAULT_COVER", "")
    force = len(parts) > 5 and parts[5].strip().lower() == "force"
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
        "force": force,
    }


def preflight_publish(canonical: str, token: str) -> tuple[bool | None, str, str]:
    """Check if a post with this canonical URL already exists.

    Returns (already_exists, existing_url, existing_slug) or (None, '', '')
    when the lookup itself failed (caller should fail-closed).
    """
    if not canonical:
        return False, "", ""
    data = gql_safe(PREFLIGHT, {}, token)
    if data is None:
        return None, "", ""
    me = data.get("me") or {}
    pubs = (me.get("publications") or {}).get("edges") or []
    edges = []
    for pub in pubs:
        pub_node = pub.get("node") or {}
        edges.extend((pub_node.get("posts") or {}).get("edges") or [])
    for e in edges:
        node = e.get("node") or {}
        node_canonical = (node.get("canonicalUrl") or "").rstrip("/")
        if node_canonical == canonical.rstrip("/"):
            return True, node.get("url", ""), node.get("slug", "")
    return False, "", ""


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
    if not parsed["force"]:
        already, url, slug = preflight_publish(str(parsed["canonical"]), token)
        if already is None:
            sys.stderr.write(
                f"ABORT — pre-flight lookup failed for canonical_url={parsed['canonical']!r} "
                "(cannot verify whether already published). "
                "Use |force as 6th field to bypass and publish anyway.\n"
            )
            sys.exit(1)
        if already:
            sys.stderr.write(
                f"ABORT — already published with canonical_url={parsed['canonical']!r} "
                f"(slug={slug}, url={url}). "
                "Use |force as 6th field to override.\n"
            )
            sys.exit(1)
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
