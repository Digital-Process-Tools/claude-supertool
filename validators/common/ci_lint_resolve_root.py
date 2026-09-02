#!/usr/bin/env python3
"""Resolve any GitLab CI config path to the repo's root `.gitlab-ci.yml` (#1797).

An include is not a standalone document and never validates alone -- `glab
ci lint` on an included file reports every one of its own stage references
as broken, because the stages are declared in the root config that includes
it. Trap 1 in #1797: the fix is to always lint the root, which is what pulls
in every include and produces the merged config `glab` actually checks.

This helper is the `resolve` command in the `ci-lint` validator's own
`.supertool.json` spec (see `_validator_resolve`) -- it runs first, and its
stdout becomes the `{file}` the `ci-lint.py` adapter is actually invoked
against. Both the root file itself (`.gitlab-ci.yml`) and any include under
`.gitlab/ci/*.yml` resolve to the same target.

Usage: resolve_root.py <file>

Prints the resolved root config path on stdout, or nothing at all when no
root config exists in this repository -- the caller (`_validator_resolve`)
reads empty output as "skip this validator", which is correct here: a repo
with no `.gitlab-ci.yml` at its root has nothing for `ci-lint` to check.
"""
from __future__ import annotations

import os
import subprocess
import sys
from refusal import guard_main

TOOL = "ci-lint-resolve"


def _repo_root(start: str) -> str | None:
    start_dir = os.path.dirname(os.path.abspath(start)) or "."
    try:
        r = subprocess.run(
            ["git", "-C", start_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    top = r.stdout.strip()
    return top or None


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        return
    root = _repo_root(sys.argv[1])
    if root is None:
        return
    candidate = os.path.join(root, ".gitlab-ci.yml")
    if os.path.isfile(candidate):
        print(candidate)


if __name__ == "__main__":
    guard_main(TOOL, main)
