#!/usr/bin/env python3
"""Dev.to publish: TITLE|MD_FILE|CANONICAL[|TAGS|COVER|PUBLISHED[|force]]

Tags: comma-separated, max 4. Cover: URL. Published: 'true'/'false', default true.
Defaults from manifest: SUPERTOOL_DEFAULT_TAGS, SUPERTOOL_DEFAULT_COVER (env).
Caller-supplied values win.

PRE-FLIGHT DUPLICATE CHECK: Before publishing, the op fetches /articles/me and
aborts if an article with the same canonical_url already exists. Pass `force` as
the 7th pipe-separated field to bypass: TITLE|MD|CANONICAL|||||force
If the pre-flight API call fails, a warning is printed and the publish proceeds.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request


def parse_args(arg: str) -> dict[str, object]:
    parts = arg.split("|")
    if len(parts) < 3:
        sys.stderr.write("ERROR: usage devto_publish:TITLE|MD_FILE|CANONICAL[|TAGS|COVER|PUBLISHED[|force]]\n")
        sys.exit(2)
    title = parts[0].strip()
    raw_path = parts[1].strip()
    used_file_prefix = raw_path.startswith("file://")
    if used_file_prefix:
        raw_path = raw_path[len("file://"):]
    md_path = Path(raw_path)
    if used_file_prefix and not md_path.is_file():
        sys.stderr.write(
            f"ERROR: file not found: {raw_path}\n"
            "(file:// prefix requires the file to exist — typo or wrong path?)\n"
        )
        sys.exit(2)
    canonical = parts[2].strip()
    tags_csv = parts[3].strip() if len(parts) > 3 and parts[3].strip() else os.environ.get("SUPERTOOL_DEFAULT_TAGS", "")
    cover = parts[4].strip() if len(parts) > 4 and parts[4].strip() else os.environ.get("SUPERTOOL_DEFAULT_COVER", "")
    published_raw = parts[5].strip().lower() if len(parts) > 5 and parts[5].strip() else "true"
    force = len(parts) > 6 and parts[6].strip().lower() == "force"
    if not md_path.is_file():
        sys.stderr.write(f"ERROR: markdown file not found: {md_path}\n")
        sys.exit(2)
    tags = [s.strip() for s in tags_csv.split(",") if s.strip()][:4]
    return {
        "title": title,
        "markdown": md_path.read_text(),
        "canonical": canonical,
        "tags": tags,
        "cover": cover,
        "published": published_raw == "true",
        "force": force,
    }


def preflight_publish(canonical: str, api_key: str) -> tuple[bool, str, str]:
    """Check if an article with this canonical_url already exists on the account.

    Returns (already_exists, existing_url, existing_slug).
    On API error returns (False, '', '') so the caller can degrade gracefully.
    """
    if not canonical:
        return False, "", ""
    try:
        articles = request("GET", "/articles/me", api_key, query={"per_page": 1000})
    except Exception:
        return False, "", ""
    if not isinstance(articles, list):
        return False, "", ""
    for a in articles:
        if (a.get("canonical_url") or "").rstrip("/") == canonical.rstrip("/"):
            return True, a.get("url", ""), a.get("slug", "")
    return False, "", ""


def build_body(parsed: dict[str, object]) -> dict[str, object]:
    article: dict[str, object] = {
        "title": parsed["title"],
        "published": parsed["published"],
        "body_markdown": parsed["markdown"],
        "canonical_url": parsed["canonical"],
    }
    if parsed["tags"]:
        article["tags"] = parsed["tags"]
    if parsed["cover"]:
        article["main_image"] = parsed["cover"]
    return {"article": article}


def main(arg: str) -> None:
    parsed = parse_args(arg)
    api_key = get_api_key()
    if not parsed["force"]:
        try:
            already, url, slug = preflight_publish(str(parsed["canonical"]), api_key)
            if already:
                sys.stderr.write(
                    f"ABORT — already published with canonical_url={parsed['canonical']!r} "
                    f"(slug={slug}, url={url}). "
                    "Use |force as 7th field to override.\n"
                )
                sys.exit(1)
        except Exception as exc:
            sys.stderr.write(f"WARNING: pre-flight check failed ({exc}) — proceeding anyway.\n")
    data = request("POST", "/articles", api_key, body=build_body(parsed))
    print(f"(published id={data.get('id')} slug={data.get('slug')})")
    print(f"URL:   {data.get('url')}")
    print(f"TITLE: {data.get('title')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    main(sys.argv[1])
