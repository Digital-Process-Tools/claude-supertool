#!/usr/bin/env python3
"""GitHub issue details via gh CLI."""
import json
import os
import re
import subprocess
import sys
import urllib.request

DESCRIPTION_MAX = 3000
COMMENT_MAX = 1000
IMAGE_DIR = "/tmp/supertool-images/gh"


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the result."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "could not resolve" in s or "404" in s or "not found" in s:
        return f"ERROR: {resource} #{identifier} not found in this repo. Check the number or verify you're in the right repo (gh repo view)."
    if "auth" in s or "login" in s or "token" in s:
        return f"ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    return f"ERROR: gh failed for {resource} #{identifier}: {stderr.strip()}"


def _extract_image_urls(text: str) -> list[str]:
    """Extract image URLs from markdown text.

    Matches:
    - ![alt](https://...png)
    - ![alt](https://user-images.githubusercontent.com/...)
    """
    return re.findall(r'!\[[^\]]*\]\((https?://[^\)]+)\)', text)


def _download_images(image_urls: list[str], issue_number: str) -> list[str]:
    """Download images to local temp directory."""
    if not image_urls:
        return []

    out_dir = os.path.join(IMAGE_DIR, issue_number)
    os.makedirs(out_dir, exist_ok=True)

    downloaded: list[str] = []
    for i, url in enumerate(image_urls):
        # Extract filename from URL or use index
        filename = os.path.basename(url.split("?")[0])
        if not filename or len(filename) > 100:
            ext = ".png" if "png" in url else ".jpg" if "jpg" in url else ".png"
            filename = f"image_{i}{ext}"
        local_path = os.path.join(out_dir, filename)

        try:
            urllib.request.urlretrieve(url, local_path)
            downloaded.append(local_path)
        except (urllib.error.URLError, OSError):
            continue

    return downloaded


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: issue.py NUMBER")
        return 1

    number = sys.argv[1]

    # Fetch issue with all needed fields
    try:
        result = _gh([
            "issue", "view", number, "--json",
            "number,title,state,labels,milestone,assignees,author,url,body,comments"
        ])
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Issue", number))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from gh\n{result.stdout[:500]}")
        return 1

    title = d.get("title", "?")
    state = d.get("state", "?")
    labels = ", ".join(l.get("name", "?") for l in d.get("labels", [])) or "none"
    milestone = (d.get("milestone") or {}).get("title", "none")
    assignees = ", ".join(a.get("login", "?") for a in d.get("assignees", [])) or "none"
    author = (d.get("author") or {}).get("login", "?")
    iid = d.get("number", number)
    web_url = d.get("url", "")
    body = (d.get("body") or "")[:DESCRIPTION_MAX]

    # Header
    print(f"# #{iid} {title}")
    print(f"State: {state} | Author: {author}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    print(f"Assignees: {assignees}")
    if web_url:
        print(f"URL: {web_url}")

    # Linked PRs — search by issue number in PR body/title
    try:
        pr_result = _gh([
            "pr", "list", "--search", str(iid), "--json",
            "number,title,state,headRefName", "--limit", "5"
        ])
        if pr_result.returncode == 0:
            prs = json.loads(pr_result.stdout)
            if prs:
                print(f"\nLinked PRs: {len(prs)}")
                for pr in prs:
                    pr_num = pr.get("number", "?")
                    pr_title = pr.get("title", "?")
                    pr_state = pr.get("state", "?")
                    pr_branch = pr.get("headRefName", "?")
                    print(f"  #{pr_num} ({pr_state}) {pr_title}")
                    print(f"    branch: {pr_branch}")
            else:
                print("\nLinked PRs: none")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # Description
    all_image_urls = _extract_image_urls(body)
    if body:
        print(f"\n## Description\n{body}")

    # Comments — gh gives them directly in the issue JSON
    comments = d.get("comments", [])
    if comments:
        print(f"\n## Comments ({len(comments)})")
        for comment in comments[-10:]:
            c_author = (comment.get("author") or {}).get("login", "?")
            c_body = (comment.get("body") or "")[:COMMENT_MAX]
            c_created = (comment.get("createdAt") or "")[:10]
            print(f"\n**{c_author}** ({c_created}):")
            print(c_body)
            all_image_urls.extend(_extract_image_urls(comment.get("body") or ""))
    else:
        print(f"\n## Comments (0)")

    # Download images
    if all_image_urls:
        all_image_urls = list(dict.fromkeys(all_image_urls))
        downloaded = _download_images(all_image_urls, str(iid))
        print(f"\n## Images ({len(all_image_urls)} found, {len(downloaded)} downloaded)")
        for path in downloaded:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
