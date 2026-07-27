#!/usr/bin/env python3
"""Git commit — stage PATHS (optional) + commit MSG + verifiable receipt.

Receipt always shows:
  - HEAD before/after SHA
  - files committed + +/- lines
  - hook exit code + first error line if pre/post-commit blocks

Surfaces silent rollbacks: if HEAD is unchanged after the call,
that's printed loudly. Replaces the add/commit/log-1 cycle.

Special MSG values:
  --no-edit   Use prepared commit message (MERGE_MSG / CHERRY_PICK_HEAD).
              Only valid when a merge or cherry-pick is in progress.
"""
from __future__ import annotations

import os
import sys

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import (  # noqa: E402
    _first_error_line,
    _git,
    query_open_mr,
    use_utf8_stdout,
)

# triple-colon separator handled by supertool; we receive plain argv here.


def _existing_mr_for_branch(branch: str) -> str:
    """Open MR/PR identifier for `branch` (e.g. !42 / #7), or empty when none.

    Thin formatter over the shared lookup — kept for the post-commit hint.
    """
    mr = query_open_mr(branch)
    if not mr:
        return ""
    prefix = "!" if mr["source"] == "gitlab" else "#"
    return f"{prefix}{mr['iid']}"


def _head_sha() -> str:
    r = _git(["rev-parse", "--short", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else ""


# Default co-author trailer. Configurable via the git-commit op:
#   .supertool.json -> ops.git-commit.coauthor  (exported as SUPERTOOL_COAUTHOR)
# Set to an empty string / "none" / "off" / "false" to disable.
_DEFAULT_COAUTHOR = "Max <noreply>"
_DISABLE_VALUES = {"", "none", "off", "false", "no", "0"}


def _coauthor_value() -> str:
    """Trailer identity ('Name <email>') or '' when disabled.

    Env SUPERTOOL_COAUTHOR (set from .supertool.json ops.git-commit.coauthor)
    wins; falls back to the built-in default. Same env-over-config convention
    used by the other git/gitlab presets.
    """
    raw = os.environ.get("SUPERTOOL_COAUTHOR")
    val = _DEFAULT_COAUTHOR if raw is None else raw
    return "" if val.strip().lower() in _DISABLE_VALUES else val.strip()


def _with_coauthor(msg: str) -> str:
    """Append a `Co-Authored-By:` trailer when absent and one is configured.

    Skips entirely if the message already carries a `Co-Authored-By:` line
    (case-insensitive) or if the trailer is disabled via config.
    """
    identity = _coauthor_value()
    if not identity:
        return msg
    if any(l.strip().lower().startswith("co-authored-by:")
           for l in msg.splitlines()):
        return msg
    trailer = f"Co-Authored-By: {identity}"
    body = msg.rstrip("\n")
    return f"{body}\n\n{trailer}"


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: commit.py MSG [PATH ...]")
        return 1

    msg = sys.argv[1]
    paths = sys.argv[2:]
    no_edit = msg.strip() == "--no-edit"

    if not no_edit and not msg.strip():
        print("ERROR: commit message is empty.")
        return 1

    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    head_before = _head_sha()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    if no_edit:
        gd = _git(["rev-parse", "--git-dir"]).stdout.strip()
        in_merge = bool(gd) and (
            os.path.exists(os.path.join(gd, "MERGE_HEAD"))
            or os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD"))
        )
        if not in_merge:
            print("ERROR: --no-edit requires a merge or cherry-pick in progress "
                  "(no MERGE_HEAD/CHERRY_PICK_HEAD found).")
            return 1

    print(f"# git-commit on {branch}")
    print(f"HEAD before: {head_before}")

    # Stage PATHS if given. A path that's already a staged deletion (gone from
    # disk after `git rm`) would make `git add` abort with "pathspec did not
    # match any files" — so drop those from the add list; their deletion is
    # already staged and will be committed (issue #324). Genuinely-unknown
    # paths stay in the list, so they still error as before.
    if paths:
        deleted = _git(["diff", "--cached", "--diff-filter=D", "--name-only"])
        staged_deletions = {l for l in deleted.stdout.splitlines() if l.strip()}
        to_add = [p for p in paths if p not in staged_deletions]
        if to_add:
            add = _git(["add", "--"] + to_add)
            if add.returncode != 0:
                print(f"ERROR: git add failed: {add.stderr.strip() or add.stdout.strip()}")
                return 1
        print(f"Staged: {len(paths)} path(s)")

    # Pre-commit staged check
    staged = _git(["diff", "--cached", "--name-only"])
    if staged.returncode != 0 or not staged.stdout.strip():
        print("ERROR: nothing staged. Use `git-commit:::MESSAGE:::PATHS` or stage manually first.")
        return 1
    staged_files = [l for l in staged.stdout.splitlines() if l.strip()]

    # Commit
    if no_edit:
        result = _git(["commit", "--no-edit"])
    else:
        result = _git(["commit", "-m", _with_coauthor(msg)])
    head_after = _head_sha()

    if result.returncode == 0 and head_after and head_after != head_before:
        new_sha = head_after
        print(f"HEAD after:  {new_sha} ✓")
        # Files + line stats from new commit
        stat = _git(["show", "--shortstat", "--format=", new_sha])
        if stat.returncode == 0 and stat.stdout.strip():
            print(stat.stdout.strip().splitlines()[-1].strip())
        print(f"Files committed: {len(staged_files)}")
        for f in staged_files[:20]:
            print(f"  {f}")
        if len(staged_files) > 20:
            print(f"  … {len(staged_files) - 20} more")
        # Next-step hint
        upstream_res = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if upstream_res.returncode == 0 and upstream_res.stdout.strip():
            existing = _existing_mr_for_branch(branch)
            if existing:
                print(f"Next: git push (updates {existing})")
            else:
                print("Next: ./supertool 'git-push' (or ./supertool 'mr:.max/mr.md|TIME|LABELS' for push+MR)")
        else:
            print("Next: git push -u origin HEAD (no upstream set)")
        return 0

    # Failure path — could be hook block, validation, or silent rollback
    print(f"HEAD after:  {head_after or '?'} ✗")
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    err = _first_error_line(combined)

    if head_after and head_before and head_after == head_before:
        print("Status: COMMIT NOT APPLIED (HEAD unchanged)")
    else:
        print(f"Status: commit returned exit {result.returncode}")

    if err:
        print(f"First error: {err}")
    print("\n--- git output ---")
    print(combined.strip() or "(no output)")
    print("\nBypass hooks (only if intentional): git commit --no-verify -m '...'")
    return result.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
