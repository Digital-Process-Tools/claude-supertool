#!/usr/bin/env python3
"""GitHub pull request details via gh CLI."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _checks  # noqa: E402  (the one check tally, shared with gh-prs / git-status)

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500


def _relative_age(iso: str) -> str:
    """Format an ISO timestamp as 'Nd ago', 'Nh ago', or 'Nm ago'."""
    if not iso:
        return "?"
    try:
        from datetime import datetime, timezone
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, ImportError):
        return "?"


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the result."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-PR-source check.

    Empty string when not in a git repo, detached HEAD, or source is empty.
    """
    if not source:
        return ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return ""
        local = r.stdout.strip()
        if not local or local == "HEAD":
            return ""
        if local == source:
            return f"You are on: {local} ✓"
        return f"You are on: {local} ⚠ MISMATCH — switch with: ./supertool 'git-checkout:{source}'"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _fetch_review_threads(url: str, number: int | str) -> list[dict]:
    """Fetch reviewThreads via GraphQL — gh pr view --json doesn't expose them.

    Parses owner/repo from PR URL. Returns [] on any failure (silent fallback).
    """
    if not url:
        return []
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", url)
    if not m:
        return []
    owner, repo = m.group(1), m.group(2)
    query = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
        "{pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved}}}}}"
    )
    try:
        r = _gh([
            "api", "graphql",
            "-f", f"query={query}",
            "-F", f"o={owner}", "-F", f"r={repo}", "-F", f"n={number}",
        ])
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        repo_node = (data.get("data") or {}).get("repository") or {}
        pr_node = repo_node.get("pullRequest") or {}
        threads = pr_node.get("reviewThreads") or {}
        return threads.get("nodes") or []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError, TypeError):
        return []


def _head_commit_age_secs(url: str, number: int | str) -> int | None:
    """Seconds since the PR's head commit was made, or None if unestablished.

    Costs one GraphQL call, so it is only ever asked when a commit carries zero
    check runs — the tally needs no help when runs exist, and `gh-pr` is a hot
    path.

    GraphQL exposes `pushedDate`, which is the field this wants and which
    GitHub now returns as `null` for every commit (verified against this repo's
    own PRs), so the age comes from `committedDate`. That can predate the push
    — a commit can sit locally for a day — which only ever makes the age look
    *older* than the wait actually was. `_checks.absence()` treats old-and-empty
    on an open PR as UNKNOWN rather than as proof, so the skew cannot
    manufacture a "no runs will be created"; the worst it does is turn a
    freshly pushed old commit's "not yet" into "go look", which is safe.

    None on every failure — no repo in the URL, gh error, missing node. The
    caller must render that as a decline, not as either verdict.
    """
    if not url:
        return None
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    query = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
        "{pullRequest(number:$n){commits(last:1){nodes{commit"
        "{pushedDate committedDate}}}}}}"
    )
    try:
        r = _gh([
            "api", "graphql",
            "-f", f"query={query}",
            "-F", f"o={owner}", "-F", f"r={repo}", "-F", f"n={number}",
        ])
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        repo_node = (data.get("data") or {}).get("repository") or {}
        pr_node = repo_node.get("pullRequest") or {}
        nodes = (pr_node.get("commits") or {}).get("nodes") or []
        if not nodes:
            return None
        commit = nodes[-1].get("commit") or {}
        stamp = commit.get("pushedDate") or commit.get("committedDate") or ""
        if not stamp:
            return None
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError,
            AttributeError, TypeError, ImportError):
        return None


def _absence_lines(d: dict, number: int | str) -> tuple[str, str]:
    """`(checks_text, mergeable_note)` for a head commit with zero check runs."""
    return _checks.absence(
        d.get("state"),
        _head_commit_age_secs(d.get("url") or "", number),
        mergeable=d.get("mergeable"),
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


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: pr.py NUMBER_OR_BRANCH [status]")
        return 1

    arg = sys.argv[1]
    slim = len(sys.argv) > 2 and sys.argv[2] == "status"

    # If not all digits, treat as branch name
    if not arg.isdigit():
        try:
            branch_result = _gh([
                "pr", "list", "--head", arg, "--json", "number",
                "--limit", "1"
            ])
            if branch_result.returncode == 0:
                prs = json.loads(branch_result.stdout)
                if prs:
                    arg = str(prs[0].get("number", arg))
                else:
                    # Try closed PRs too
                    branch_result2 = _gh([
                        "pr", "list", "--head", arg, "--state", "all",
                        "--json", "number", "--limit", "1"
                    ])
                    if branch_result2.returncode == 0:
                        prs2 = json.loads(branch_result2.stdout)
                        if prs2:
                            arg = str(prs2[0].get("number", arg))
                        else:
                            print(f"ERROR: no PR found for branch {arg!r}")
                            return 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"ERROR: branch lookup failed: {e}")
            return 1

    # Fetch PR with all needed fields
    try:
        result = _gh([
            "pr", "view", arg, "--json",
            "number,title,state,author,headRefName,baseRefName,labels,"
            "milestone,reviewDecision,reviews,mergeCommit,mergeable,"
            "isDraft,url,body,comments,additions,deletions,changedFiles,"
            "statusCheckRollup,assignees,createdAt,updatedAt"
        ])
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "PR", arg))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from gh\n{result.stdout[:500]}")
        return 1

    if slim:
        iid = d.get("number", arg)
        state = d.get("state", "?")
        mergeable = d.get("mergeable", "?")
        review_decision = d.get("reviewDecision") or "none"
        check_states = _checks.github_states(d.get("statusCheckRollup"))
        merge_commit = (d.get("mergeCommit") or {}).get("oid", "")
        web_url = d.get("url", "")
        conflicts = "yes" if mergeable == "CONFLICTING" else "no"
        print(f"#{iid} | state: {state} | mergeable: {mergeable} | conflicts: {conflicts}")
        print(f"branch: {d.get('headRefName') or '?'} -> {d.get('baseRefName') or '?'}")
        if check_states:
            checks_text = _checks.summarize(check_states)
        else:
            checks_text, _ = _absence_lines(d, iid)
        print(f"checks: {checks_text}")
        print(f"review: {review_decision}")
        if merge_commit:
            print(f"merge_commit: {merge_commit[:12]}")
        if web_url:
            print(f"url: {web_url}")
        return 0

    title = d.get("title", "?")
    state = d.get("state", "?")
    iid = d.get("number", arg)
    source = d.get("headRefName", "?")
    target = d.get("baseRefName", "?")
    author = (d.get("author") or {}).get("login", "?")
    web_url = d.get("url", "")
    labels = ", ".join(l.get("name", "?") for l in d.get("labels", [])) or "none"
    milestone = (d.get("milestone") or {}).get("title", "none")
    draft = d.get("isDraft", False)
    mergeable = d.get("mergeable", "?")
    review_decision = d.get("reviewDecision") or "none"
    merge_commit = (d.get("mergeCommit") or {}).get("oid", "")
    additions = d.get("additions", "?")
    deletions = d.get("deletions", "?")
    changed_files = d.get("changedFiles", "?")

    # Header
    draft_marker = " [DRAFT]" if draft else ""
    print(f"# #{iid} {title}{draft_marker}")
    print(f"State: {state} | Author: {author}")
    print(f"Branch: {source} -> {target}")
    local_check = _local_branch_check(source)
    if local_check:
        print(local_check)
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")

    # Assignees (distinct from reviewers)
    assignees = d.get("assignees") or []
    assignee_names = [a.get("login", "?") for a in assignees]
    print(f"Assignees: {', '.join(assignee_names) if assignee_names else 'none'}")

    # Age — created/updated, for stale-PR signal
    created_at = d.get("createdAt") or ""
    updated_at = d.get("updatedAt") or ""
    if created_at:
        age_str = f"Created: {_relative_age(created_at)}"
        if updated_at and updated_at != created_at:
            age_str += f" | Updated: {_relative_age(updated_at)}"
        print(age_str)

    # Unresolved review threads — fetched via GraphQL (not exposed by gh pr view --json)
    review_threads = _fetch_review_threads(d.get("url", ""), iid)
    if review_threads:
        unresolved = sum(1 for t in review_threads if not t.get("isResolved"))
        print(f"Unresolved threads: {unresolved} / {len(review_threads)}")

    # Reviews — always print so absence is signal, not silence
    reviews = d.get("reviews", [])
    if reviews:
        reviewers = {}
        for r in reviews:
            login = (r.get("author") or {}).get("login", "?")
            r_state = r.get("state", "?")
            reviewers[login] = r_state  # latest review state per reviewer
        parts = [f"{login} ({state})" for login, state in reviewers.items()]
        print(f"Reviews: {', '.join(parts)}")
    else:
        print("Reviews: none")
    print(f"Review decision: {review_decision}")

    # Checks (CI status) — the tally accounts for every entry it was handed;
    # see presets/_checks.py for why the sum matters more than the labels.
    # An absent tally is two opposite readings, so the zero case is classified
    # rather than named — see _checks.absence() (#585). It buys the evidence for
    # that (one GraphQL call) only here, never when runs exist.
    check_states = _checks.github_states(d.get("statusCheckRollup"))
    if check_states:
        checks_text = _checks.summarize(check_states)
        merge_note = "" if _checks.all_green(check_states) else (
            f" — checks {_checks.NOT_GREEN}, see Checks above"
        )
    else:
        checks_text, merge_note = _absence_lines(d, iid)
    print(f"Checks: {checks_text}")

    # Changes
    print(f"Changes: {changed_files} files, +{additions} -{deletions}")

    # Mergeable — GitHub's *merge conflict* state, not a CI verdict. Printed
    # bare underneath a check tally it reads as one, which is half of what made
    # #454 dangerous, so it names what it measures and carries the CI caveat.
    # merge_note was computed with the Checks line above: "unknown because
    # nothing has run yet" and "unknown because nothing will run" are different
    # answers to a merge question, and this printed one sentence for both.
    if mergeable == "CONFLICTING":
        print(f"Conflicts: YES — cannot merge{merge_note}")
    elif mergeable == "MERGEABLE":
        print(f"Mergeable: yes (no merge conflicts){merge_note}")
    else:
        print(f"Mergeable: {mergeable}{merge_note}")

    # Merge commit
    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")

    # Linked issue — every issue a GitHub closing keyword actually binds to a
    # number, not the first `#N` in the body (#591). The pattern this replaces
    # made the keyword optional, and it lived here *and* in `git-status`
    # character-for-character; both now go through the one extractor.
    issue_refs = _checks.closing_issue_refs(d.get("body"))
    if not issue_refs:
        print(f"\n{_checks.linked_issue_line(issue_refs)}")
    for ref in issue_refs:
        # A cross-repo reference is printed as written and never fetched:
        # `gh issue view 5` resolves 5 against *this* repository, so fetching it
        # would print a different issue's title under this PR's closing
        # reference — #591's defect with more confidence attached.
        if not ref.startswith("#"):
            print(f"\nIssue: {ref} — in another repository, not fetched")
            continue
        issue_num = ref[1:]
        try:
            issue_result = _gh([
                "issue", "view", issue_num, "--json",
                "number,title,state,labels,assignees"
            ])
            if issue_result.returncode == 0:
                issue_data = json.loads(issue_result.stdout)
                i_title = issue_data.get("title", "?")
                i_state = issue_data.get("state", "?")
                i_labels = ", ".join(l.get("name", "") for l in issue_data.get("labels", []))
                i_assignees = ", ".join(a.get("login", "") for a in issue_data.get("assignees", []))
                print(f"\n## Issue #{issue_num} — {i_title}")
                info = f"State: {i_state}"
                if i_labels:
                    info += f" | Labels: {i_labels}"
                if i_assignees:
                    info += f" | Assignees: {i_assignees}"
                print(info)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            print(f"\nIssue: {ref}")

    # Description
    body = (d.get("body") or "")[:DESCRIPTION_MAX]
    if body:
        print(f"\n## Description\n{body}")
    else:
        print("\n## Description\n_(empty)_")

    # Comments
    comments = d.get("comments", [])
    if comments:
        print(f"\n## Comments ({len(comments)})")
        for c in comments[-10:]:
            c_author = (c.get("author") or {}).get("login", "?")
            c_body = (c.get("body") or "")[:COMMENT_MAX]
            c_created = (c.get("createdAt") or "")[:10]
            print(f"\n**{c_author}** ({c_created}):")
            print(c_body)
    else:
        print(f"\n## Comments (0)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
