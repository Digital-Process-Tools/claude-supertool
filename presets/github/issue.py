#!/usr/bin/env python3
"""GitHub issue details via gh CLI."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)

DESCRIPTION_MAX = 3000
COMMENT_MAX = 1000
IMAGE_DIR = "/tmp/supertool-images/gh"


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the result.

    A repo target (#673) becomes `--repo OWNER/NAME`; `gh api` never takes one.
    """
    if args and args[0] != "api":
        args = args + _repo_target.gh_args()
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-issue:673")
    if "could not resolve" in s or "404" in s or "not found" in s:
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.not_found_hint()}")
    if "401" in s or "unauthorized" in s or "not logged in" in s or "token" in s:
        return f"ERROR: gh CLI not authenticated. Run: gh auth login (verify with: gh auth status)"
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
        print("ERROR: usage: issue.py NUMBER [full]")
        return 1

    number = sys.argv[1]
    full = len(sys.argv) > 2 and sys.argv[2] == "full"
    desc_max = None if full else DESCRIPTION_MAX
    comment_max = None if full else COMMENT_MAX

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
    body = d.get("body") or ""
    if desc_max is not None:
        body = body[:desc_max]

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
        if full:
            shown = comments
            print(f"\n## Comments ({len(comments)})")
        else:
            shown = comments[-10:]
            truncated = len(comments) - len(shown)
            suffix = f", {truncated} earlier truncated — use :full to fetch all" if truncated else ""
            print(f"\n## Comments ({len(shown)} of {len(comments)} shown{suffix})")
        for comment in shown:
            c_author = (comment.get("author") or {}).get("login", "?")
            c_body = comment.get("body") or ""
            if comment_max is not None and len(c_body) > comment_max:
                c_body = c_body[:comment_max] + f"\n…[truncated at {comment_max} chars — use :full]"
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
