#!/usr/bin/env python3
"""Resolve any GitLab CI config path to the repo root `.gitlab-ci.yml` (#1797).

An include is not a standalone document and never validates alone -- `glab
ci lint` on an included file reports every one of its own stage references
as broken, because the stages are declared in the root config that includes
it. Trap 1 in #1797: the fix is to always lint the root, which is what pulls
in every include and produces the merged config `glab` actually checks.

This helper is the `resolve` command in the `ci-lint` validator’s own
`.supertool.json` spec (see `_validator_resolve`) -- it runs first, and its
stdout becomes the `{file}` the `ci-lint.py` adapter is actually invoked
against. Both the root file itself (`.gitlab-ci.yml`) and any include under
`.gitlab/ci/*.yml` resolve to the same target.

Usage: resolve_root.py <file>

Three outcomes on stdout, and they are deliberately not the same line (#2177,
this repo’s own named defect class -- an absence produced by the tool, read
as an absence in the world):

- the resolved root config path, when one exists;
- nothing at all, when the lookup succeeded and there genuinely is no root
  config -- `_validator_resolve` reads that as skip this validator, which
  is correct: a repo with no `.gitlab-ci.yml` at its root has nothing for
  `ci-lint` to check;
- a line starting with `RESOLVE-ERROR: ` when the lookup itself could not be
  made -- `git` is not on PATH, `git rev-parse` timed out, or `start` is not
  inside a git repository at all. `_validator_resolve` recognizes this prefix
  (it is the shared protocol any `resolve` command in this tree can use) and
  reports a distinct skip reason instead of folding it into the same silence
  as the second case.
"""
from __future__ import annotations

import os
import subprocess
import sys
from refusal import guard_main

TOOL = "ci-lint-resolve"

#: Shared protocol with `_validator_resolve` in `_supertool.py` (#2177): a
#: `resolve` command that could not even look prints this prefix plus a
#: reason, instead of the empty stdout it would print for "looked, found
#: nothing". A resolved path is a filesystem path and never starts with this,
#: so the two cannot collide.
RESOLVE_ERROR_PREFIX = "RESOLVE-ERROR: "


def _repo_root(start: str) -> tuple[str | None, str | None]:
    """The repo root above `start`.

    Returns `(root, None)` on success. Returns `(None, reason)` when the
    lookup itself could not be made -- `git` absent, `git` timed out, or
    `start` is not inside a git repository -- and `(None, None)` only if a
    caller adds a route to "looked and found nothing" here later; today every
    `None`-returning path also returns a reason, because `_repo_root` itself
    has nothing between "found a root" and "could not determine one" (#2177).
    """
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
