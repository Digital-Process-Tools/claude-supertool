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

import _body  # noqa: E402  (the one body cap + disclosure — #698)
import _untrusted  # noqa: E402  (the fence around tracker text — #694)

DESCRIPTION_MAX = 3000
COMMENT_MAX = 1000
IMAGE_DIR = "/tmp/supertool-images/gh"


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the result."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return f"ERROR: cwd is not a GitHub repo. cd into a GitHub-cloned repo, or run gh directly with --repo OWNER/REPO."
    if "could not resolve" in s or "404" in s or "not found" in s:
        return f"ERROR: {resource} #{identifier} not found in this repo. Check the number or verify you're in the right repo (gh repo view)."
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

    # One-line fields are flattened rather than fenced (#694): two marker lines
    # around a six-word title is the noise that gets a convention abandoned,
    # and the only thing a single line could otherwise do is become two.
    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    labels = _untrusted.flat(", ".join(l.get("name", "?") for l in d.get("labels", [])) or "none")
    milestone = _untrusted.flat((d.get("milestone") or {}).get("title", "none"))
    assignees = _untrusted.flat(", ".join(a.get("login", "?") for a in d.get("assignees", [])) or "none")
    author = _untrusted.flat((d.get("author") or {}).get("login", "?"))
    iid = d.get("number", number)
    web_url = d.get("url", "")
    body = d.get("body") or ""
    body_total = len(body)
    # The cut and its wording live in presets/_body.py — four ops render a
    # capped body and four hand-maintained copies of a disclosure is how a
    # fifth forgets to have one (#698).
    body, body_withheld = _body.cut(body, desc_max)

    # Header. The fence convention is declared before the first thing inside a
    # fence — the reader this protects is the one who acts on the first line.
    print(_untrusted.banner())
    print(f"# #{iid} {title}")
    print(f"State: {state} | Author: {author}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    print(f"Assignees: {assignees}")
    if web_url:
        print(f"URL: {web_url}")
    if body_withheld:
        # Disclosed here, not just at the point of the cut — a reader who
        # stops at the top must still see it (#681).
        print(_body.header_notice(body, body_total, body_withheld))

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
        print(f"\n## Description\n{_untrusted.fence(body)}")
        if body_withheld:
            print(f"\n{_body.cut_notice(body_withheld)}")

    # Comments — gh gives them directly in the issue JSON
    comments = d.get("comments", [])
    shown = comments if full else comments[-_body.COMMENT_TAIL:]
    print(f"\n{_body.comments_heading(len(shown), len(comments))}")
    for comment in shown:
        c_author = _untrusted.flat((comment.get("author") or {}).get("login", "?"))
        c_body = comment.get("body") or ""
        # The truncation notice is supertool's, so it is printed outside the
        # fence. Inside it, a reader who is applying the fence correctly
        # would have to discount it — and it is the one line here they need
        # to be able to believe. The comment-count heading above is the tool's
        # words for the same reason, and is printed before any fence opens.
        c_trunc = ""
        if comment_max is not None and len(c_body) > comment_max:
            c_body = c_body[:comment_max]
            c_trunc = _body.comment_cut_notice(comment_max)
        c_created = (comment.get("createdAt") or "")[:10]
        print(f"\n**{c_author}** ({c_created}):")
        print(_untrusted.fence(c_body))
        if c_trunc:
            print(c_trunc)
        all_image_urls.extend(_extract_image_urls(comment.get("body") or ""))

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
