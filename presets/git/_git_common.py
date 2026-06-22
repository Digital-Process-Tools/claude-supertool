#!/usr/bin/env python3
"""Shared helpers for the git/* preset scripts.

Holds the bits that were drifting across commit.py / push.py:
  - _git            : thin subprocess wrapper
  - _first_error_line: pick the salient line out of git/hook output
  - query_open_mr   : open MR/PR for a branch, as structured fields

Each script formats query_open_mr's output its own way — the lookup
(glab → gh fallback, all failures swallowed) lives here once.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Optional


def _git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _first_error_line(text: str) -> str:
    """First line mentioning an error/rejection, else last non-empty line."""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if ("error" in low or "fatal" in low or "rejected" in low
                or "aborted" in low or "failed" in low
                or "! [" in s or "❌" in s):
            return s
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def query_open_mr(branch: str) -> Optional[dict]:
    """Open MR/PR for `branch`, or None when none / no tool available.

    Returns {source, iid, target, pipeline}. `pipeline` is the GitLab
    pipeline status when known, else None (gh list carries no cheap check
    state). Tries glab (GitLab) first, falls back to gh (GitHub). All
    failures swallowed — this is advisory output, never blocking.
    """
    if not branch or branch == "HEAD":
        return None
    if shutil.which("glab"):
        try:
            res = subprocess.run(
                ["glab", "mr", "list", "--source-branch", branch, "--state",
                 "opened", "--output", "json"],
                capture_output=True, text=True, timeout=5,
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
                    }
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    if shutil.which("gh"):
        try:
            res = subprocess.run(
                ["gh", "pr", "list", "--head", branch, "--state", "open",
                 "--json", "number,baseRefName", "--limit", "1"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip().startswith("["):
                prs = json.loads(res.stdout)
                if prs:
                    pr = prs[0]
                    return {
                        "source": "github",
                        "iid": pr.get("number", "?"),
                        "target": pr.get("baseRefName", "?"),
                        "pipeline": None,
                    }
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    return None
