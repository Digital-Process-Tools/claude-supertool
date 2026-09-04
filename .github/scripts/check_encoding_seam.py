#!/usr/bin/env python3
"""Scoped, pre-push runner for the #418 encoding-seam guard (#2287).

`tests/test_encoding_seam.py` enforces this tree-wide, but only inside the
full pytest suite -- which only runs in CI, so a violation is caught only
*after* a lane has already pushed and burned a full CI leg (often the ~9-10
minute Windows leg) finding out. Measured 2026-09-04: this guard fired 6
times across 6 different lanes dispatched in one tick, every single time on
the lane's own brand-new test file.

This script runs the SAME two scan functions -- `encoding_violations` and
`subprocess_encoding_violations` -- imported from `tests/test_encoding_seam.py`
via `validators/common/encoding_seam.py` rather than re-implemented, over
just the files a lane actually changed: git-diff'd against a base ref by
default, or named explicitly on argv. Cheap and narrow on purpose -- this
repo's own house style warns that a slow or false-positive local check
teaches lanes to route around it, which is the exact failure this exists to
avoid one level down.

Usage:
    check_encoding_seam.py                 # files changed vs merge-base with origin/<default branch>
    check_encoding_seam.py --base REF      # files changed vs REF (no merge-base lookup)
    check_encoding_seam.py FILE [FILE...]  # explicit files, no git diff at all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                       / "validators" / "common"))
from encoding_seam import (  # noqa: E402
    find_test_module, load_scan_module, repo_root, scan_one, scope_kinds,
)

DEFAULT_BRANCH_FALLBACK = "master"
TIMEOUT_S = 30


def _default_branch(root: Path) -> str:
    cfg = root / ".oss.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        branch = data.get("default_branch") if isinstance(data, dict) else None
        if isinstance(branch, str) and branch:
            return branch
    return DEFAULT_BRANCH_FALLBACK


def _merge_base(root: Path, branch: str) -> "str | None":
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "merge-base", "HEAD", "origin/" + branch],
            capture_output=True, timeout=TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def _changed_files(root: Path, base: str) -> "list[str] | None":
    """Files a push from `HEAD` would actually carry, relative to `base`.

    Diffed against `HEAD`, not the working tree: an untracked or unstaged
    file never leaves this machine on a push, so it is not this script's
    business, and folding it in would flag files nobody is about to send.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only",
             "--diff-filter=ACMR", base, "HEAD"],
            capture_output=True, timeout=TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print("check-encoding-seam: could not run git diff against {0}: "
              "{1}".format(base, exc), file=sys.stderr)
        return None
    if r.returncode != 0:
        print("check-encoding-seam: git diff against {0} failed: {1}".format(
            base, (r.stderr or "").strip()), file=sys.stderr)
        return None
    return [line for line in (r.stdout or "").splitlines() if line]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*",
                         help="explicit files to check (skips git diff entirely)")
    parser.add_argument("--base", default=None,
                         help="ref to diff against (default: merge-base with "
                              "origin/<default branch>)")
    args = parser.parse_args(argv)

    root = repo_root(Path.cwd())
    if root is None:
        print("check-encoding-seam: not inside a git repository, nothing to "
              "check", file=sys.stderr)
        return 0

    module_path = find_test_module(root)
    if module_path is None:
        print("check-encoding-seam: no tests/test_encoding_seam.py found at "
              "{0} -- this project has not adopted the encoding-seam guard, "
              "nothing to run scoped".format(root), file=sys.stderr)
        return 0

    try:
        module = load_scan_module(module_path)
    except Exception as exc:  # the guard module is the project's own
        print("check-encoding-seam: {0} could not be imported, so nothing "
              "was checked: {1}: {2}".format(
                  module_path, type(exc).__name__, exc), file=sys.stderr)
        return 0

    if args.files:
        candidates = args.files
    else:
        base = args.base
        if base is None:
            branch = _default_branch(root)
            base = _merge_base(root, branch)
            if base is None:
                print("check-encoding-seam: could not find a merge-base with "
                      "origin/{0}, nothing to diff against".format(branch),
                      file=sys.stderr)
                return 0
        candidates = _changed_files(root, base)
        if candidates is None:
            return 0

    py_files = [f for f in candidates
                if f.endswith(".py") and (root / f).is_file()]

    all_records = []  # (relpath, record)
    for relpath in py_files:
        kinds = scope_kinds(relpath, module.SHIPPED)
        for record in scan_one(module, root / relpath, kinds):
            all_records.append((relpath, record))

    if not all_records:
        print("check-encoding-seam: {0} changed .py file(s) checked, "
              "clean".format(len(py_files)))
        return 0

    errors = [(p, r) for p, r in all_records if r["severity"] == "error"]
    warnings = [(p, r) for p, r in all_records if r["severity"] != "error"]

    if errors:
        print("check-encoding-seam: encoding-seam violations in changed "
              "files (full rule: tests/test_encoding_seam.py):")
        for relpath, r in errors:
            print("  {0}:{1}: {2}".format(relpath, r["line"], r["msg"]))
    if warnings:
        print("check-encoding-seam: calls the scan cannot judge -- pin "
              "encoding=/errors= literally, or justify in review:")
        for relpath, r in warnings:
            print("  {0}:{1}: {2}".format(relpath, r["line"], r["msg"]))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
