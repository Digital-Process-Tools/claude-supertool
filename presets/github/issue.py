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
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
import _untrusted  # noqa: E402  (the fence around tracker text — #694)

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


def _linked_prs_unknown(reason_lines: list) -> None:
    """Say the lookup could not be answered, rather than printing nothing.

    Silence here reads as "there are no linked PRs", and that reading has an
    action attached: an unclaimed issue is one to delegate. Work has been
    re-delegated onto an already-merged fix on this tracker because a list did
    not distinguish "none" from "could not ask" (#780).

    Three states, not two — `docs/validators.md`, "Declining instead of
    guessing".
    """
    reason = (reason_lines or [""])[0].strip() or "no detail from gh"
    print(f"{chr(10)}Linked PRs: unknown — could not query ({reason})")


# Aliased in a single-issue GraphQL call. Generous relative to `gh-issues`'
# `first: 5`: that op caps hard because it multiplies across a whole board's
# worth of issues per call, but this op is answering about the one issue a
# reader named, and probably wants the complete list. A cap that is hit is
# reported rather than silently truncated (#780).
CLOSING_PR_LIMIT = 20


def _owner_repo(web_url: str) -> tuple[str, str] | None:
    """(owner, name) for the GraphQL query. A repo target wins; else the issue's own URL.

    `gh pr list` resolved the repo implicitly from the cwd's git remote via
    `--repo`; `gh api graphql` has no such flag; the owner/name has to be in
    the query text itself. The issue JSON always carries its own `url`, so
    this costs no extra call and cannot disagree with the issue being shown.
    """
    target = _repo_target.owner_repo()
    if target is not None:
        return target
    parts = web_url.split("/")
    if len(parts) >= 5 and parts[2].endswith("github.com"):
        return parts[3], parts[4]
    return None


def _closing_prs_query(owner: str, name: str, number: object) -> str:
    """GraphQL for "is a PR going to close this" — matches `gh-issues` (#782).

    Not `gh pr list --search`, which matched the number anywhere in a PR's
    text: a body that only mentions the issue ("unlike #761, this one…")
    scored the same as a real closer (#780 item 2, measured live: #774
    reported as linked to #770 when it only mentions it while closing #760).

    `includeClosedPrs: true` is load-bearing: without it a *merged* closer
    disappears, which is #780 item 3 measured live — `gh pr list --search`
    defaults to open-only, so `gh-issue:778` reported "none" while #781
    (MERGED) is the PR that actually closed it.
    """
    return (
        f'query {{ repository(owner: "{owner}", name: "{name}") {{ '
        f'issue(number: {number}) {{ '
        f'closedByPullRequestsReferences(first: {CLOSING_PR_LIMIT}, includeClosedPrs: true) '
        '{ nodes { number title state headRefName } } } } }'
    )


def _closing_pr_nodes(payload: object) -> list[dict] | None:
    """Pull the closer PRs out of the GraphQL envelope.

    `None` at any level — not a dict, missing key, explicit null — means the
    lookup could not answer, distinct from an answered empty list. Mirrors
    `_closing_prs` in `presets/github/issues.py` (#782), same vocabulary.
    """
    if not isinstance(payload, dict):
        return None
    repo = (payload.get("data") or {}).get("repository")
    if not isinstance(repo, dict):
        return None
    issue_node = repo.get("issue")
    if not isinstance(issue_node, dict):
        return None
    refs = issue_node.get("closedByPullRequestsReferences")
    if not isinstance(refs, dict):
        return None
    nodes = refs.get("nodes")
    if nodes is None:
        return None
    return [n for n in nodes if isinstance(n, dict)]


def _print_linked_prs(iid: object, web_url: str = "") -> None:
    """Render the linked-PR section for an issue."""
    owner_name = _owner_repo(web_url)
    if owner_name is None:
        _linked_prs_unknown(["could not determine owner/repo for the linked-PR lookup"])
        return
    owner, name = owner_name
    query = _closing_prs_query(owner, name, iid)

    try:
        result = _gh(["api", "graphql", "-f", f"query={query}"], timeout=15)
        if result.returncode != 0:
            _linked_prs_unknown(
                result.stderr.strip().splitlines()[:1] or ["gh api graphql failed"]
            )
            return
        payload = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        _linked_prs_unknown(["gh api graphql timed out"])
        return
    except json.JSONDecodeError:
        _linked_prs_unknown(["gh api graphql returned output that is not JSON"])
        return

    nodes = _closing_pr_nodes(payload)
    if nodes is None:
        _linked_prs_unknown(["gh api graphql returned an unexpected shape"])
        return

    if not nodes:
        print(f"{chr(10)}Linked PRs: none")
        return

    print(f"{chr(10)}Linked PRs: {len(nodes)}")
    for pr in nodes:
        pr_num = pr.get("number", "?")
        pr_title = pr.get("title", "?")
        pr_state = pr.get("state", "?")
        pr_branch = pr.get("headRefName", "?")
        print(f"  #{pr_num} ({pr_state}) {pr_title}")
        print(f"    branch: {pr_branch}")
    if len(nodes) == CLOSING_PR_LIMIT:
        print(f"    (showing the first {CLOSING_PR_LIMIT} — there may be more)")


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

    _print_linked_prs(iid, web_url)

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
