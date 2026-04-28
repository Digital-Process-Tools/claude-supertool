#!/usr/bin/env python3
"""GitLab issue details via glab CLI.

Fetches issue metadata, human comments, related MRs, and downloads
any images found in description/comments to a local temp directory.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse

DESCRIPTION_MAX = 3000
COMMENT_MAX = 1000
IMAGE_DIR = "/tmp/supertool-images"


def _glab(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab command and return the result."""
    return subprocess.run(
        ["glab"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab api call."""
    return subprocess.run(
        ["glab", "api", endpoint],
        capture_output=True, text=True, timeout=timeout,
    )


def _extract_image_urls(text: str) -> list[str]:
    """Extract GitLab upload image URLs from markdown text.

    Matches:
    - ![alt](/uploads/SECRET/filename.png)
    - ![alt](https://gitlab.example.com/user/repo/uploads/SECRET/filename.png)
    """
    patterns = [
        r'!\[[^\]]*\]\((/uploads/[^\)]+)\)',
        r'!\[[^\]]*\]\((https?://[^\)]*?/uploads/[^\)]+)\)',
    ]
    urls: list[str] = []
    for pattern in patterns:
        urls.extend(re.findall(pattern, text))
    return urls


def _download_images(image_urls: list[str], issue_number: str) -> list[str]:
    """Download GitLab upload images to local temp directory.

    Returns list of local file paths for successfully downloaded images.
    """
    if not image_urls:
        return []

    out_dir = os.path.join(IMAGE_DIR, issue_number)
    os.makedirs(out_dir, exist_ok=True)

    downloaded: list[str] = []
    for url in image_urls:
        # Extract the /uploads/SECRET/FILENAME part
        match = re.search(r'(/uploads/[^\s\)]+)', url)
        if not match:
            continue

        upload_path = match.group(1)
        filename = os.path.basename(upload_path)
        # URL-decode filename for local storage
        local_name = urllib.parse.unquote(filename)
        local_path = os.path.join(out_dir, local_name)

        # Use glab api to download (handles auth automatically)
        # The endpoint is projects/:id/uploads — but glab api with GET
        # on the raw upload path also works
        api_path = f"projects/:id{upload_path}"
        try:
            result = subprocess.run(
                ["glab", "api", "--method", "GET", api_path],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                with open(local_path, "wb") as f:
                    f.write(result.stdout)
                downloaded.append(local_path)
        except (subprocess.TimeoutExpired, OSError):
            continue

    return downloaded


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: issue.py NUMBER")
        return 1

    number = sys.argv[1]

    # 1. Fetch issue metadata
    try:
        result = _glab(["issue", "view", number, "--output", "json"])
    except FileNotFoundError:
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(f"ERROR: glab failed: {result.stderr.strip()}")
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from glab\n{result.stdout[:500]}")
        return 1

    title = d.get("title", "?")
    state = d.get("state", "?")
    labels = ", ".join(d.get("labels", [])) or "none"
    milestone = (d.get("milestone") or {}).get("title", "none")
    assignees = ", ".join(a.get("username", "?") for a in d.get("assignees", [])) or "none"
    author = (d.get("author") or {}).get("username", "?")
    iid = d.get("iid", number)
    web_url = d.get("web_url", "")
    description = (d.get("description") or "")[:DESCRIPTION_MAX]
    project_id = d.get("project_id", "")

    # Header
    print(f"# #{iid} {title}")
    print(f"State: {state} | Author: {author}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    print(f"Assignees: {assignees}")
    if web_url:
        print(f"URL: {web_url}")

    # 2. Fetch related MRs via API
    try:
        mr_result = _glab_api(
            f"projects/:id/issues/{iid}/related_merge_requests"
        )
        if mr_result.returncode == 0:
            mrs = json.loads(mr_result.stdout)
            if isinstance(mrs, list) and mrs:
                print(f"\nRelated MRs: {len(mrs)}")
                for mr in mrs[:10]:
                    mr_iid = mr.get("iid", "?")
                    mr_title = mr.get("title", "?")
                    mr_state = mr.get("state", "?")
                    mr_branch = mr.get("source_branch", "?")
                    print(f"  !{mr_iid} ({mr_state}) {mr_title}")
                    print(f"    branch: {mr_branch}")
            else:
                print("\nRelated MRs: none")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # 3. Description (strip GitLab markdown attributes like {width=... height=...})
    description = re.sub(r'\{width=\d+\s+height=\d+\}', '', description)
    if description:
        print(f"\n## Description\n{description}")

    # 4. Fetch comments (notes) — human only
    all_image_urls = _extract_image_urls(description)
    try:
        notes_result = _glab_api(
            f"projects/:id/issues/{iid}/notes?per_page=50&sort=asc"
        )
        if notes_result.returncode == 0:
            notes = json.loads(notes_result.stdout)
            if isinstance(notes, list):
                human_notes = [n for n in notes if not n.get("system", False)]
                system_count = len(notes) - len(human_notes)
                if human_notes:
                    print(f"\n## Comments ({len(human_notes)} human, {system_count} system skipped)")
                    for note in human_notes[-10:]:  # last 10 human comments
                        note_author = (note.get("author") or {}).get("username", "?")
                        body = (note.get("body") or "")[:COMMENT_MAX]
                        created = (note.get("created_at") or "")[:10]
                        print(f"\n**{note_author}** ({created}):")
                        print(body)
                        # Extract images from comments too
                        all_image_urls.extend(_extract_image_urls(note.get("body") or ""))
                else:
                    print(f"\n## Comments (0 human, {system_count} system skipped)")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # 5. Download images
    if all_image_urls:
        # Deduplicate
        all_image_urls = list(dict.fromkeys(all_image_urls))
        downloaded = _download_images(all_image_urls, str(iid))
        print(f"\n## Images ({len(all_image_urls)} found, {len(downloaded)} downloaded)")
        for path in downloaded:
            print(f"  {path}")
        failed = len(all_image_urls) - len(downloaded)
        if failed > 0:
            print(f"  ({failed} failed to download)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
