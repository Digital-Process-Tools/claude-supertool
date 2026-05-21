#!/usr/bin/env python3
"""git-status validator adapter. Emits SCHEMA.md JSON.

Always ok=true — reports working-tree delta as metrics, never triggers rollback.

Usage: git-status.py <file>

Env vars:
  GIT_BIN  git binary (default: git)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import pathlib
import time


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def _parse_numstat(output: str) -> tuple[int, int]:
    """Parse `git diff --numstat` single-file output → (added, removed)."""
    line = output.strip()
    if not line:
        return 0, 0
    parts = line.split("\t")
    if len(parts) < 2:
        return 0, 0
    try:
        added = int(parts[0]) if parts[0] != "-" else 0
        removed = int(parts[1]) if parts[1] != "-" else 0
        return added, removed
    except ValueError:
        return 0, 0


def _parse_state(porcelain: str) -> str:
    """Parse `git status --porcelain` single-file output → state string."""
    line = porcelain.rstrip("\n")
    if not line:
        return "clean"
    if len(line) < 2:
        return "unknown"
    xy = line[:2]
    x = xy[0]  # index (staged)
    y = xy[1]  # worktree (unstaged)
    if xy == "??":
        return "untracked"
    if x != " " and x != "?" and y == " ":
        return "staged"
    if y != " " and y != "?":
        return "modified"
    if x != " " and x != "?":
        return "staged"
    return "clean"


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "git-status", "file": "", "ok": True, "count": 0,
            "errors": [], "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "lines_staged_added": 0, "lines_staged_removed": 0,
                        "state": "clean"},
        })
        return

    file = sys.argv[1]
    git_bin = os.environ.get("GIT_BIN", "git")

    if not shutil.which(git_bin):
        emit({
            "tool": "git-status", "file": file, "ok": True, "count": 0,
            "errors": [], "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "lines_staged_added": 0, "lines_staged_removed": 0,
                        "state": "clean"},
        })
        return

    start = time.time()
    file_dir = str(pathlib.Path(file).resolve().parent)

    def run(*args: str) -> str:
        try:
            r = subprocess.run(
                [git_bin, *args],
                capture_output=True, text=True, timeout=5,
                cwd=file_dir,
            )
            return r.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""

    # Check we're inside a git repo (fast — runs rev-parse)
    rev = run("rev-parse", "--is-inside-work-tree")
    if rev.strip() != "true":
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "git-status", "file": file, "ok": True, "count": 0,
            "errors": [], "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "lines_staged_added": 0, "lines_staged_removed": 0,
                        "state": "clean"},
        })
        return

    worktree_out = run("diff", "--numstat", "--", file)
    staged_out = run("diff", "--cached", "--numstat", "--", file)
    porcelain_out = run("status", "--porcelain", "--", file)

    dur = int((time.time() - start) * 1000)

    added, removed = _parse_numstat(worktree_out)
    staged_added, staged_removed = _parse_numstat(staged_out)
    state = _parse_state(porcelain_out)

    emit({
        "tool": "git-status",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {
            "lines_added": added,
            "lines_removed": removed,
            "lines_staged_added": staged_added,
            "lines_staged_removed": staged_removed,
            "state": state,
        },
    })


if __name__ == "__main__":
    main()
