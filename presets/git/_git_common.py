#!/usr/bin/env python3
"""Shared helpers for the git/* preset scripts.

Holds the bits that were drifting across commit.py / push.py:
  - _git            : thin subprocess wrapper
  - _first_error_line: pick the salient line out of git/hook output
  - query_open_mr   : open MR/PR for a branch, as structured fields
  - use_utf8_stdout : stop the ✓/✗ glyphs crashing a cp1252 console

Each script formats query_open_mr's output its own way — the lookup
(glab → gh fallback, all failures swallowed) lives here once.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Optional


def use_utf8_stdout() -> None:
    """Force UTF-8 on this process's stdout/stderr.

    Windows stdout defaults to cp1252, which cannot encode the ✓/✗/⚠ glyphs
    these scripts print — writing one kills the process with
    UnicodeEncodeError, so a commit that actually succeeded reports as a
    crash. supertool.py reconfigures its own streams, but each preset runs as
    a separate process and does not inherit that. diff.py carried this inline
    from issue #308; it lives here so the rest do not each need a copy.

    A stream without ``reconfigure`` (wrapped or replaced, as under pytest's
    capture) is left alone rather than treated as an error.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def repo_label() -> str:
    """Absolute path of the repo the calling op is acting on.

    `git-diff` has printed a `Repo:` line for a long time; `git-commit` and
    `git-push` did not — so the two ops that WRITE were the two that never said
    where they wrote. When a commit lands somewhere unexpected, that line is
    the difference between noticing within the minute and noticing next week
    (#692).

    The work tree when there is one, the git dir otherwise: a bare repo has no
    top level, and printing an empty string there would be worse than printing
    nothing. "unknown" rather than a guess when git answers neither — a wrong
    repo name is the one output worse than no repo name.
    """
    top = _git(["rev-parse", "--show-toplevel"])
    if top.returncode == 0 and top.stdout.strip():
        return top.stdout.strip()
    bare = _git(["rev-parse", "--absolute-git-dir"])
    if bare.returncode == 0 and bare.stdout.strip():
        return f"{bare.stdout.strip()} (bare)"
    return "unknown"


def _looks_like_success(line: str) -> bool:
    """True for lines that report success — must never be picked as an error.

    Pre-push / pre-receive hooks that auto-format print green '✅ … 0 errors.'
    lines and 'pushed successfully' notices; the substrings 'errors' /
    'fatal' would otherwise match the error scan and surface a success line
    as the "first error".
    """
    s = line.strip()
    if not s:
        return False
    low = s.lower()
    has_success = ("✅" in s or "✓" in s or any(m in low for m in (
        "0 errors", "no errors", "pushed successfully", "successfully pushed")))
    if not has_success:
        return False
    # A success marker doesn't win if the same line also carries a hard error
    # signal (e.g. 'lint ✓ — push blocked: error: …'). 'error:' (with colon)
    # avoids matching the '0 errors' / 'no errors' success phrases.
    has_error = any(k in low for k in (
        "error:", "fatal", "rejected", "aborted", "failed", "declined")) \
        or "! [" in s or "❌" in s
    return not has_error


def _first_error_line(text: str) -> str:
    """First line mentioning an error/rejection, else last non-empty line.

    Skips success lines (green ✅, '0 errors', 'pushed successfully') so a
    hook's success banner is never misreported as the failure cause.
    """
    lines = text.splitlines()
    for line in lines:
        s = line.strip()
        if not s or _looks_like_success(s):
            continue
        low = s.lower()
        if ("error" in low or "fatal" in low or "rejected" in low
                or "aborted" in low or "failed" in low
                or "! [" in s or "❌" in s):
            return s
    for line in reversed(lines):
        s = line.strip()
        if s and not _looks_like_success(s):
            return s
    return ""


def query_open_mr(branch: str) -> Optional[dict]:
    """Open MR/PR for `branch`, or None when none / no tool available.

    Returns {source, iid, target, pipeline, pipeline_id, pipeline_url,
    merge_status}. `pipeline` is the GitLab pipeline status when known, else
    None (gh list carries no cheap check state). `merge_status` is the
    server's view of mergeability ('can_be_merged' / 'cannot_be_merged' /
    None). The extra fields ride the same call — no added round-trip — and
    are best-effort: absent on a glab version that doesn't emit them. Tries
    glab (GitLab) first, falls back to gh (GitHub). All failures swallowed —
    this is advisory output, never blocking.
    """
    if not branch or branch == "HEAD":
        return None
    if shutil.which("glab"):
        try:
            res = subprocess.run(
                ["glab", "mr", "list", "--source-branch", branch, "--state",
                 "opened", "--output", "json"],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            if res.returncode == 0 and res.stdout.strip().startswith("["):
                mrs = json.loads(res.stdout)
                if mrs:
                    mr = mrs[0]
                    pipeline = mr.get("pipeline") or mr.get("head_pipeline") or {}
                    return {
                        "source": "gitlab",
                        "iid": mr.get("iid") or mr.get("number") or "?",
                        "target": mr.get("target_branch", "?"),
                        "pipeline": pipeline.get("status"),
                        "pipeline_id": pipeline.get("id"),
                        "pipeline_url": pipeline.get("web_url"),
                        "merge_status": mr.get("detailed_merge_status")
                        or mr.get("merge_status"),
                    }
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    if shutil.which("gh"):
        try:
            res = subprocess.run(
                ["gh", "pr", "list", "--head", branch, "--state", "open",
                 "--json", "number,baseRefName,mergeable", "--limit", "1"],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            if res.returncode == 0 and res.stdout.strip().startswith("["):
                prs = json.loads(res.stdout)
                if prs:
                    pr = prs[0]
                    # gh: mergeable is CONFLICTING / MERGEABLE / UNKNOWN
                    gh_merge = pr.get("mergeable")
                    merge_status = ("cannot_be_merged"
                                    if gh_merge == "CONFLICTING" else None)
                    return {
                        "source": "github",
                        "iid": pr.get("number", "?"),
                        "target": pr.get("baseRefName", "?"),
                        "pipeline": None,
                        "pipeline_id": None,
                        "pipeline_url": None,
                        "merge_status": merge_status,
                    }
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    return None
