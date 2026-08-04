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

A message containing ':' must arrive via `git-commit:::MSG` or the @payload
route: the single-colon CLI tokenizes on ':', so `git-commit:fix: thing`
reaches this script as MSG='fix' plus a PATH of ' thing'. That shape is
REFUSED here rather than re-parsed — see _spilled_message_paths (#751).
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
    repo_label,
    use_utf8_stdout,
)

# triple-colon separator handled by supertool; we receive plain argv here.

# The commit itself, which runs whatever the pre-commit hook chain is. Every
# other call in this preset is rev-parse / diff --cached plumbing on the shared
# 10s default; this one carries the 30s the whole module used to assume, and is
# the only place in it where 30s was ever doing work.
_COMMIT_TIMEOUT = 30


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


# Characters a caller does not put in a pathspec they typed at this CLI, but
# that prose spilled by the ':' tokenizer always carries. Glob magic (*, ?, [)
# is deliberately absent: `src/*.py` is a legitimate pathspec.
_PROSE_CHARS = (" ", "\t", "\n", "\r", '"', "'")

# The @payload example quotes the message with a triple-single-quote block.
# Built rather than typed so this file stays editable through supertool's own
# TOML payload route, where a literal triple quote would close the block.
_TRIPLE = "'" * 3


def _looks_like_pathspec(tok: str) -> bool:
    """Could *tok* be a pathspec the caller actually typed? (#751)"""
    return bool(tok) and tok == tok.strip() and not any(
        c in tok for c in _PROSE_CHARS
    )


def _known_to_git(path: str, staged_deletions: set) -> bool:
    """Does git recognise *path* as something it could stage?

    On disk, already staged as a deletion (`git rm`, issue #324), or tracked.
    Existence alone is not the test: the deletion case is exactly why a
    'does it resolve to a real file' discriminator cannot be trusted.
    """
    if os.path.exists(path) or path in staged_deletions:
        return True
    r = _git(["ls-files", "--", path])
    return r.returncode == 0 and bool(r.stdout.strip())


def _spilled_message_paths(paths, staged_deletions):
    """PATH args that are neither path-shaped nor known to git (#751).

    An empty result means every PATH is plausibly a path and the call proceeds
    exactly as it always has — including a typo'd path, which stays git's error
    to report rather than something this script reinterprets.
    """
    return [
        p for p in paths
        if not _looks_like_pathspec(p) and not _known_to_git(p, staged_deletions)
    ]


def _colon_split_refusal(msg, paths, spilled):
    """The error printed instead of guessing what the caller meant (#751).

    Reconstructs the message the caller almost certainly typed — the leading
    run of spilled segments, rejoined on ':' — and hands back both routes that
    carry a ':' intact. Nothing is staged and nothing is committed.
    """
    rest = list(paths)
    head = []
    while rest and rest[0] in spilled:
        head.append(rest.pop(0))
    rebuilt = ":".join([msg] + head)

    suggestion = "git-commit:::" + rebuilt
    if rest:
        suggestion += ":::" + ":::".join(rest)

    lines = [
        "ERROR: commit message was split on ':' — nothing staged, nothing committed.",
        "  Parsed as: message=%r" % (msg,),
    ]
    for p in paths:
        why = " (not a path, and unknown to git)" if p in spilled else ""
        lines.append("             path=%r%s" % (p, why))
    lines += [
        "  supertool's single-colon CLI splits on every ':', so a Conventional",
        "  Commits subject cannot survive it. Use a route that does not tokenize:",
        "    ./supertool '%s'" % (suggestion,),
        "  or, for paths with spaces or a multi-line body:",
        "    ./supertool 'git-commit:@-' <<'EOF'",
        "    message = " + _TRIPLE + rebuilt + _TRIPLE,
        "    paths = [" + ", ".join('"%s"' % p for p in rest or ["path/to/file"]) + "]",
        "    EOF",
    ]
    return "\n".join(lines)


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
    # Printed before anything is staged, so it is on the receipt whether the
    # commit lands, is refused by a hook, or finds nothing staged (#692).
    print(f"Repo: {repo_label()}")
    print(f"HEAD before: {head_before}")

    # Stage PATHS if given. A path that's already a staged deletion (gone from
    # disk after `git rm`) would make `git add` abort with "pathspec did not
    # match any files" — so drop those from the add list; their deletion is
    # already staged and will be committed (issue #324). Genuinely-unknown
    # paths stay in the list, so they still error as before.
    if paths:
        deleted = _git(["diff", "--cached", "--diff-filter=D", "--name-only"])
        staged_deletions = {l for l in deleted.stdout.splitlines() if l.strip()}
        # #751 — a PATH that is neither path-shaped nor known to git is far more
        # likely the tail of a ':'-split message than a file. Refuse before
        # anything is staged; do NOT fold it back into the message, because a
        # wrong guess in that direction commits whatever was already staged
        # under a mangled subject and prints a success receipt for it.
        spilled = _spilled_message_paths(paths, staged_deletions)
        if spilled:
            print(_colon_split_refusal(msg, paths, spilled))
            return 1
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
        result = _git(["commit", "--no-edit"], timeout=_COMMIT_TIMEOUT)
    else:
        result = _git(["commit", "-m", _with_coauthor(msg)],
                      timeout=_COMMIT_TIMEOUT)
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
