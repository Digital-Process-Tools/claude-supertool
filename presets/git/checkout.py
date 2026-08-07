#!/usr/bin/env python3
"""Git checkout — switch to REF and report new state in one call.

Combines: switch + branch info + ahead/behind + tracking + recent
commits + dirty file count. Replaces the checkout/status/log/branch
flurry with a single round-trip.
"""
from __future__ import annotations

import os
import sys

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import _git, use_utf8_stdout  # noqa: E402

def _ref_missing(ref: str) -> bool:
    """True when this repo cannot resolve `ref` to a commit.

    Every recovery path below rewrites local state — `fetch --all --prune`
    drops remote-tracking refs, `checkout -b --track` and `checkout -B <ref>
    FETCH_HEAD` create and move a local branch — and each used to be chosen by
    scanning git's human error message for `pathspec` / `did not match any`.
    That message is translated. Under `LANGUAGE=fr` git answers `le
    specificateur de chemin 'x' ne correspond a aucun fichier connu de git`,
    the substrings never match, and all three recoveries silently stop firing
    for anyone not running an English git (#649) — the same channel defect
    #641 fixed in push.py, one preset over.

    `rev-parse --verify --quiet` answers the actual question on its exit code,
    which no locale changes. It is also a strictly better question: "can this
    repo resolve the ref" is what the recoveries need to know, where the error
    string was only ever a proxy for it.

    `-` is `@{-1}`, the previous branch. No fetch can make it resolve, so a
    failed `checkout -` is never a case for recovery.
    """
    if ref == "-":
        return False
    return _git(["rev-parse", "--verify", "--quiet",
                 f"{ref}^{{commit}}"]).returncode != 0


def _in_repo() -> bool:
    return _git(["rev-parse", "--git-dir"]).returncode == 0


def _names_a_path(arg: str) -> bool:
    """True when git would understand `arg` as a pathspec in this repo.

    A tracked-file match is the question that actually matters: `git checkout
    <pathspec>` only rewrites paths git already holds in the index, so
    `ls-files --error-unmatch` answers precisely "would this argument have
    destroyed something". The on-disk fallback catches untracked paths, which
    git refuses harmlessly — named anyway, because someone who typed a filename
    needs to hear "that is a path", not "ref not found".

    `-` is `@{-1}`, the previous branch, and is never a pathspec.
    """
    if arg == "-":
        return False
    if _git(["ls-files", "--error-unmatch", "--", arg]).returncode == 0:
        return True
    return os.path.exists(arg)


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: checkout.py REF")
        return 1

    ref = sys.argv[1]
    # #150: reject refs that look like CLI flags. `--orphan`, `--detach`,
    # `--track=…` are valid git invocations that change semantics — a
    # prompt-injected REF would silently do something other than switch
    # branches. `-` (previous branch) is legitimate; allow it.
    if ref.startswith("-") and ref != "-":
        print(f"ERROR: ref starts with '-' (refusing for safety): {ref!r}")
        return 1

    # #756: `git checkout <arg>` is two operations sharing one name. Given a
    # ref it switches branches; given a pathspec it restores those paths from
    # the index, discarding uncommitted work — and that write leaves no reflog
    # entry, no stash and no object, so there is nothing anywhere to recover it
    # from. git picks between the two by what the string happens to name, and
    # #150 already established that this argument is attacker-influenced. A
    # pathspec needs no flag and no special characters, so the `-` guard above
    # cannot see it: it is the *absence* of anything suspicious that makes it
    # work.
    #
    # Two defences, in this order:
    #   1. refuse up front when the argument names a path and no commit — this
    #      is what makes the message useful;
    #   2. pass `--` on every switch below, so git cannot select the pathspec
    #      reading even for an argument this check did not anticipate (a glob,
    #      say). The `--` is the guarantee; the refusal is the explanation.
    #
    # An argument that is *both* (a `docs` branch beside a `docs/` directory)
    # resolves to the ref, always, pinned by the `--` rather than left to git's
    # DWIM — and says so, because deterministic without disclosed is still a
    # surprise.
    ambiguous = False
    if ref != "-" and _in_repo():
        resolves = not _ref_missing(ref)
        is_path = _names_a_path(ref)
        if is_path and not resolves:
            print(f"ERROR: {ref!r} names a path, not a ref (refusing for safety).")
            print("`git checkout <path>` restores files from the index and discards")
            print("uncommitted changes to them unrecoverably — no reflog entry, no")
            print("stash, no object written. git-checkout switches branches only.")
            print(f"To restore it anyway, deliberately: git checkout -- {ref}")
            return 1
        ambiguous = is_path and resolves

    prev_branch = ""
    prev = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    if prev.returncode == 0:
        prev_branch = prev.stdout.strip()

    prev_sha = ""
    prev_sha_res = _git(["rev-parse", "--short", "HEAD"])
    if prev_sha_res.returncode == 0:
        prev_sha = prev_sha_res.stdout.strip()

    stderr = ""
    s = ""
    # Set once the explicit single-ref fetch below succeeded: from that point
    # the remote demonstrably has the ref, so "not found" is a false report no
    # matter what the failing checkout said (#267, #649).
    ref_fetched = False
    result = _git(["checkout", ref, "--"])
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        s = stderr.lower()
        # Auto-fetch fallback: if ref not found locally, try once after fetch
        if _ref_missing(ref):
            fetch = _git(["fetch", "--all", "--prune", "--quiet"], timeout=30)
            if fetch.returncode == 0:
                result = _git(["checkout", ref, "--"])
                if result.returncode == 0:
                    print("# (auto-fetched before checkout)")
                    stderr = ""
                else:
                    stderr = result.stderr.strip() or result.stdout.strip()
                    s = stderr.lower()
        # #277: remote-only branch. git's DWIM (`checkout <branch>` →
        # create local tracking branch) can fail to fire — e.g.
        # checkout.guess=false, or after a fetch that only moved FETCH_HEAD.
        # Resolve it ourselves: if exactly one remote has
        # refs/remotes/<remote>/REF, create the tracking branch explicitly.
        # Multiple matches → error like git does.
        if result.returncode != 0 and _ref_missing(ref):
            remotes_res = _git(["remote"])
            remotes = remotes_res.stdout.split() if remotes_res.returncode == 0 else []
            matches = [
                r for r in remotes
                if _git(["rev-parse", "--verify", "--quiet", f"{r}/{ref}"]).returncode == 0
            ]
            if len(matches) == 1:
                track = _git(["checkout", "-b", ref, "--track", f"{matches[0]}/{ref}"])
                if track.returncode == 0:
                    print(f"# (created local branch tracking {matches[0]}/{ref})")
                    result = track
                else:
                    stderr = track.stderr.strip() or track.stdout.strip()
                    s = stderr.lower()
            elif len(matches) > 1:
                joined = ", ".join(f"{r}/{ref}" for r in matches)
                print(f"ERROR: ref {ref!r} matches multiple remotes: {joined}. "
                      f"Disambiguate, e.g. git checkout -b {ref} --track <remote>/{ref}")
                return 1
        # #267: single-branch / narrowed-refspec workspaces never create a
        # refs/remotes/origin/<branch> tracking ref, so the #277 path above
        # finds no match and `git fetch --all` only moved FETCH_HEAD. Fall
        # back to an explicit single-ref fetch + checkout of FETCH_HEAD.
        if result.returncode != 0 and _ref_missing(ref):
            single = _git(["fetch", "origin", ref], timeout=30)
            if single.returncode == 0:
                ref_fetched = True
                cob = _git(["checkout", "-B", ref, "FETCH_HEAD"])
                if cob.returncode == 0:
                    print(f"# (fetched origin {ref}, reset local branch to FETCH_HEAD)")
                    result = cob
                    stderr = ""
                    s = ""
                else:
                    # ref WAS found and fetched — the `-B FETCH_HEAD` checkout
                    # itself failed (commonly a dirty tree blocking the switch).
                    # Surface that real error instead of the stale "pathspec"
                    # one, so the reporter below names the true blocker.
                    result = cob
                    stderr = cob.stderr.strip() or cob.stdout.strip()
                    s = stderr.lower()
    if result.returncode != 0:
        if "not a git repository" in s:
            print("ERROR: not inside a git repository.")
        elif "is already used by worktree" in s or "already checked out at" in s:
            import re
            m = re.search(r"worktree at ['\"]?([^'\"\n]+?)['\"]?$", stderr, re.MULTILINE) \
                or re.search(r"checked out at ['\"]?([^'\"\n]+?)['\"]?$", stderr, re.MULTILINE)
            path = m.group(1) if m else None
            print(f"ERROR: ref {ref!r} is checked out in another worktree.")
            if path:
                print(f"Switch with: cd {path}")
                print(f"Or remove it: git worktree remove {path}")
        elif "would be overwritten" in s or "local changes" in s:
            print(f"ERROR: uncommitted changes block checkout. Stash or commit first.\n{stderr}")
        elif _ref_missing(ref) and not ref_fetched:
            # `_ref_missing` alone is not enough here. When the single-ref fetch
            # reached the ref and only `checkout -B` was blocked, the ref is
            # still unresolvable locally — but "not found" is not the blocker
            # the caller needs to hear (#267), so `ref_fetched` vetoes it.
            print(f"ERROR: ref {ref!r} not found even after fetch.")
        else:
            print(f"ERROR: checkout failed: {stderr}")
        return 1

    branch_res = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "?"

    head_sha_res = _git(["rev-parse", "--short", "HEAD"])
    head_sha = head_sha_res.stdout.strip() if head_sha_res.returncode == 0 else "?"

    print(f"# git-checkout: {prev_branch}@{prev_sha} → {branch}@{head_sha}")

    if ambiguous:
        print(f"Note: {ref!r} also names a path here; taken as a ref "
              f"(pinned with `--`, not left to git's guess).")

    # #756: identical before/after is the one signal that nothing moved, and in
    # the original report it was printed unflagged directly above `Working
    # tree: clean` — which together read as a successful switch. Say it.
    if prev_branch == branch and prev_sha == head_sha and prev_sha:
        print(f"# no branch change occurred — already on {branch}@{head_sha}")

    # Tracking + ahead/behind
    upstream_res = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    upstream = ""
    ahead_behind = ""
    if upstream_res.returncode == 0:
        upstream = upstream_res.stdout.strip()
        ab_res = _git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
        if ab_res.returncode == 0:
            parts = ab_res.stdout.strip().split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
                if ahead and behind:
                    ahead_behind = f"ahead {ahead}, behind {behind}"
                elif ahead:
                    ahead_behind = f"ahead {ahead}"
                elif behind:
                    ahead_behind = f"behind {behind}"
                else:
                    ahead_behind = "in sync"

    if upstream:
        print(f"Tracking: {upstream}" + (f" ({ahead_behind})" if ahead_behind else ""))
        if "behind" in ahead_behind:
            print(f"Next: git pull (behind {upstream})")
    else:
        print("Tracking: (none — set upstream with `git push -u`)")

    # Divergence vs base (master/main)
    base = ""
    for candidate in ("master", "main"):
        check = _git(["rev-parse", "--verify", "--quiet", candidate])
        if check.returncode == 0:
            base = candidate
            break
    if base and branch != base:
        ab = _git(["rev-list", "--left-right", "--count", f"{base}...HEAD"])
        if ab.returncode == 0:
            parts = ab.stdout.strip().split()
            if len(parts) == 2:
                behind_b, ahead_b = int(parts[0]), int(parts[1])
                print(f"vs {base}: {ahead_b} ahead, {behind_b} behind")

    # Dirty state
    status_res = _git(["status", "--porcelain=v1"])
    if status_res.returncode == 0:
        lines = [l for l in status_res.stdout.splitlines() if l.strip()]
        staged = sum(1 for l in lines if l[0] not in (" ", "?"))
        unstaged = sum(1 for l in lines if len(l) > 1 and l[1] != " " and l[0] != "?")
        untracked = sum(1 for l in lines if l.startswith("??"))
        if staged or unstaged or untracked:
            bits = []
            if staged:
                bits.append(f"{staged} staged")
            if unstaged:
                bits.append(f"{unstaged} unstaged")
            if untracked:
                bits.append(f"{untracked} untracked")
            print(f"Working tree: {', '.join(bits)}")
        else:
            print("Working tree: clean")

    # Mid-merge / rebase warning
    state_res = _git(["rev-parse", "--git-dir"])
    if state_res.returncode == 0:
        from os.path import exists, join
        gd = state_res.stdout.strip()
        if exists(join(gd, "MERGE_HEAD")):
            print("⚠ Merge in progress — resolve or `git merge --abort`")
        # #900: these two directories are the whole signal, deliberately.
        # `REBASE_HEAD` used to be ORed in here and must not come back. It is a
        # ref git writes while replaying a commit, not a state marker: on the
        # conflict -> `--continue` -> completion path git never unlinks it. The
        # rebase is over, both directories are gone, `git status` reports
        # nothing and `git rebase --abort` answers "no rebase in progress" — so
        # the warning fired on every later checkout in that repo with no remedy
        # a user could apply. Same on git 2.39.5 and 2.46.2.
        #
        # Dropping it also costs no detection, which is the part worth checking
        # before touching this line again: the directories are what git's own
        # status reads to decide it is mid-rebase, and they are present for
        # every backend and every stop — merge backend, am backend (`--apply`),
        # `--rebase-merges`, and the interactive `break` and `edit` stops. The
        # `break` stop is the proof in the other direction: it carries no
        # `REBASE_HEAD` at all, so a check leaning on the ref goes blind exactly
        # where a rebase really is in progress. All of it is pinned in
        # tests/test_git_checkout_rebase_state_900.py.
        if exists(join(gd, "rebase-merge")) or exists(join(gd, "rebase-apply")):
            print("⚠ Rebase in progress — resolve or `git rebase --abort`")

    # Recent commits
    log_res = _git(["log", "-3", "--format=%h %ad %an | %s", "--date=short"])
    if log_res.returncode == 0 and log_res.stdout.strip():
        print("\n## Last 3 commits")
        for line in log_res.stdout.strip().splitlines():
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
