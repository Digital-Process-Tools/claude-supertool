#!/usr/bin/env python3
































from __future__ import annotations

import os
import subprocess
import sys
from refusal import guard_main

TOOL = "ci-lint-resolve"
















RESOLVE_ERROR_PREFIX = "RESOLVE-ERROR: "


def _repo_root(start: str) -> tuple[str | None, str | None]:









    start_dir = os.path.dirname(os.path.abspath(start)) or "."
    try:
        r = subprocess.run(
            ["git", "-C", start_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "git binary not found"
    except subprocess.TimeoutExpired:
        return None, "git rev-parse timed out"
    except OSError as exc:
        return None, "git could not be run: {0}".format(exc)
    if r.returncode != 0:
        return None, "not inside a git repository"
    top = r.stdout.strip()
    return (top, None) if top else (None, "git reported no toplevel")


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        return
    root, reason = _repo_root(sys.argv[1])
    if root is None:
        if reason:
            print(RESOLVE_ERROR_PREFIX + reason)
        return
    candidate = os.path.join(root, ".gitlab-ci.yml")
    if os.path.isfile(candidate):
        print(candidate)


if __name__ == "__main__":
    guard_main(TOOL, main)
