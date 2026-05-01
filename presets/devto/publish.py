#!/usr/bin/env python3
"""Dev.to publish: TITLE|MD_FILE|CANONICAL[|TAGS|COVER|PUBLISHED]

Tags: comma-separated, max 4. Cover: URL. Published: 'true'/'false', default true.
Defaults from manifest: SUPERTOOL_DEFAULT_TAGS, SUPERTOOL_DEFAULT_COVER (env).
Caller-supplied values win.
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
        sys.stderr.write("ERROR: usage devto_publish:TITLE|MD_FILE|CANONICAL[|TAGS|COVER|PUBLISHED]\n")
        sys.exit(2)
    title = parts[0].strip()
    md_path = Path(parts[1].strip())
    canonical = parts[2].strip()
    tags_csv = parts[3].strip() if len(parts) > 3 and parts[3].strip() else os.environ.get("SUPERTOOL_DEFAULT_TAGS", "")
    cover = parts[4].strip() if len(parts) > 4 and parts[4].strip() else os.environ.get("SUPERTOOL_DEFAULT_COVER", "")
    published_raw = parts[5].strip().lower() if len(parts) > 5 and parts[5].strip() else "true"
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
    }


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
    data = request("POST", "/articles", api_key, body=build_body(parsed))
    print(f"(published id={data.get('id')} slug={data.get('slug')})")
    print(f"URL:   {data.get('url')}")
    print(f"TITLE: {data.get('title')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    main(sys.argv[1])
