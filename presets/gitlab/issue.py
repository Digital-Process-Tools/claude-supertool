#!/usr/bin/env python3
"""GitLab issue details via glab CLI.

Fetches issue metadata, human comments, related MRs, and downloads
any images found in description/comments to a local temp directory.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _body  # noqa: E402  (the one body cap + disclosure — #698)

DESCRIPTION_MAX = 3000
# Related MRs are listed, not summarised, so the list is capped. A *count* cut
# it — the total was always printed correctly above a short list, which reads as
# the numbers being wrong rather than as a ceiling being hit (#635).
RELATED_MRS_MAX = 10
COMMENT_MAX = 1000
IMAGE_DIR = "/tmp/supertool-images"


def _glab(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab command and return the result."""
    return subprocess.run(
        ["glab"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return f"ERROR: {resource} #{identifier} not found in this repo. Check the number or verify you're in the right repo."
    if "401" in s or "unauthorized" in s or "glpat_" in s or "authenticate" in s or "bad token" in s or "token expired" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    return f"ERROR: glab failed for {resource} #{identifier}: {stderr.strip()}"


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab api call."""
    return subprocess.run(
        ["glab", "api", endpoint],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
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


def _is_inside(candidate: str, directory: str) -> bool:
    """True when `candidate` really resolves inside `directory`.

    Compared after realpath on both sides, and with a trailing separator on the
    directory so a sibling like `/tmp/images-other` cannot pass as `/tmp/images`.
    """
    root = os.path.realpath(directory)
    target = os.path.realpath(candidate)
    return target == root or target.startswith(root + os.sep)


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
        # Decode BEFORE taking the basename. The remote name is percent-encoded,
        # so `basename` on the encoded form sees one segment and leaves any
        # encoded separators intact — they only become separators afterwards.
        # Decoding first means basename operates on what the name really says.
        local_name = os.path.basename(urllib.parse.unquote(upload_path))
        local_path = os.path.join(out_dir, local_name)
        # And confirm it: a name is only usable if it actually resolves inside
        # the directory we chose. Anything else is skipped rather than guessed at.
        if not _is_inside(local_path, out_dir):
            print(f"note: skipped an attachment whose name resolves outside {out_dir}")
            continue

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
        print("ERROR: usage: issue.py NUMBER [full]")
        return 1

    number = sys.argv[1]
    full = len(sys.argv) > 2 and sys.argv[2] == "full"
    desc_max = None if full else DESCRIPTION_MAX
    comment_max = None if full else COMMENT_MAX

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
        print(_format_error(result.stderr, "Issue", number))
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
    # GitLab markdown attributes are stripped *before* the cap, not after, so
    # the disclosed counts describe the text actually printed (#698).
    description = re.sub(
        r'\{width=\d+\s+height=\d+\}', '', d.get("description") or ""
    )
    description_total = len(description)
    description, description_withheld = _body.cut(description, desc_max)
    project_id = d.get("project_id", "")

    # Header
    print(f"# #{iid} {title}")
    print(f"State: {state} | Author: {author}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    print(f"Assignees: {assignees}")
    if web_url:
        print(f"URL: {web_url}")
    if description_withheld:
        # Before the description, not only at the cut — the reader this
        # protects is the one who stops at the top (#681, #698).
        print(_body.header_notice(
            description, description_total, description_withheld))

    # 2. Fetch related MRs via API
    try:
        mr_result = _glab_api(
            f"projects/:id/issues/{iid}/related_merge_requests"
        )
        if mr_result.returncode == 0:
            mrs = json.loads(mr_result.stdout)
            if isinstance(mrs, list) and mrs:
                shown_mrs = mrs if full else mrs[:RELATED_MRS_MAX]
                hidden_mrs = len(mrs) - len(shown_mrs)
                if hidden_mrs:
                    print(
                        f"\nRelated MRs: {len(shown_mrs)} of {len(mrs)} shown "
                        f"({hidden_mrs} not listed — count limit of "
                        f"{RELATED_MRS_MAX}; use :full for all)"
                    )
                else:
                    print(f"\nRelated MRs: {len(mrs)}")
                for mr in shown_mrs:
                    mr_iid = mr.get("iid", "?")
                    mr_title = mr.get("title", "?")
                    mr_state = mr.get("state", "?")
                    mr_branch = mr.get("source_branch", "?")
                    print(f"  !{mr_iid} ({mr_state}) {mr_title}")
                    print(f"    branch: {mr_branch}")
                if hidden_mrs:
                    print(
                        f"  ... ({hidden_mrs} more related MR(s) not shown — "
                        f"use :full)"
                    )
            else:
                print("\nRelated MRs: none")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # 3. Description (markdown attributes already stripped, above the cap)
    if description:
        print(f"\n## Description\n{description}")
        if description_withheld:
            print(f"\n{_body.cut_notice(description_withheld)}")

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
                    if full:
                        shown_notes = human_notes
                        print(f"\n## Comments ({len(human_notes)} human, {system_count} system skipped)")
                    else:
                        shown_notes = human_notes[-10:]
                        truncated = len(human_notes) - len(shown_notes)
                        suffix = f", {truncated} earlier truncated — use :full to fetch all" if truncated else ""
                        print(f"\n## Comments ({len(shown_notes)} of {len(human_notes)} human shown{suffix}, {system_count} system skipped)")
                    for note in shown_notes:
                        note_author = (note.get("author") or {}).get("username", "?")
                        body = note.get("body") or ""
                        if comment_max is not None and len(body) > comment_max:
                            body = body[:comment_max] + f"\n…[truncated at {comment_max} chars — use :full]"
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
