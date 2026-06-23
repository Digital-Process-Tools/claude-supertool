#!/usr/bin/env python3
"""Git push — update the current branch's remote + verifiable receipt.

Closes the loop for the common case the `mr` op doesn't cover: pushing a
fix to an MR that already exists. `mr` creates; this updates.

Receipt always shows:
  - branch + upstream (sets upstream on first push)
  - commits pushed (remote SHA before/after) or "already up to date"
  - ahead/behind vs the remote afterwards
  - open MR/PR for the branch + pipeline status (push triggers a run)

Philosophy — embed the mechanical recovery, surface only the decision.
A non-fast-forward (remote moved ahead) is the routine recoverable case:
instead of bailing with a hint and costing a round-trip, this op rebases
local work onto the remote itself. Clean → it pushes. Conflict → it leaves
the rebase paused (explicitly, with the conflicting files + the exact
continue/abort commands), so `git-conflicts` can inspect the blocks and
the resolution decision stays with the caller. History is never rewritten
silently and never force-pushed for you.

Flags (colon-appended: `git-push:force-with-lease:no-verify`):
  - force-with-lease — overwrite the remote only if it hasn't moved since
    we last fetched. The safe force, for legitimate history rewrites.
    Suppresses auto-rebase (the explicit force is your decision).
  - no-verify — skip the local pre-push hook. Documented escape when a
    local formatter legitimately diverges from CI.

A pre-push hook that auto-fixes files commonly amends HEAD, pushes the
corrected commit itself, then exits non-zero so git won't also push the
stale pre-amend ref. That non-zero exit is *success*, not failure — the
ref moved. We trust the live remote SHA over the exit code: when the
remote already matches local HEAD, we report PUSHED, not REJECTED.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import _first_error_line, _git, query_open_mr  # noqa: E402

_KNOWN_FLAGS = ("force-with-lease", "no-verify")


def _parse_flags(argv: list[str]) -> set[str]:
    """Collect known flags from colon-split argv tokens; ignore the rest."""
    flags: set[str] = set()
    for tok in argv:
        t = tok.strip().lower()
        if t in _KNOWN_FLAGS:
            flags.add(t)
    return flags


def _upstream_ref() -> str:
    """Configured upstream of HEAD (e.g. origin/foo), or empty if none."""
    r = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    return r.stdout.strip() if r.returncode == 0 else ""


def _remote_sha(ref: str) -> str:
    if not ref:
        return ""
    r = _git(["rev-parse", "--short", ref])
    return r.stdout.strip() if r.returncode == 0 else ""


def _local_head() -> str:
    """Full SHA of local HEAD (a pre-push hook may rewrite it mid-push)."""
    r = _git(["rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else ""


def _live_remote_sha(remote: str, ref: str) -> str:
    """Authoritative remote SHA via ls-remote (full sha), or empty.

    Reads the real remote, not the local remote-tracking ref — a hook that
    pushes on our behalf moves the remote without us having fetched.
    """
    if not remote or not ref:
        return ""
    r = _git(["ls-remote", remote, ref], timeout=30)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.split()[0]
    return ""


def _split_upstream(upstream: str, branch: str) -> tuple[str, str]:
    """(remote, ref) from an upstream like 'origin/foo'; fall back to origin."""
    if "/" in upstream:
        remote, ref = upstream.split("/", 1)
        return remote, ref
    return "origin", branch


def _is_non_fast_forward(combined: str) -> bool:
    low = combined.lower()
    return ("non-fast-forward" in low
            or "fetch first" in low
            or "tip of your current branch is behind" in low)


_LEFTOVER_CAP = 8


def _open_mr_line(mr: Optional[dict]) -> str:
    """One-line MR/PR summary for the post-push receipt, or empty."""
    if not mr:
        return ""
    if mr["source"] == "gitlab":
        pipe = mr.get("pipeline") or "triggered"
        if mr.get("pipeline_id"):
            pipe += f" #{mr['pipeline_id']}"
        line = f"MR !{mr['iid']} → {mr['target']} | pipeline: {pipe}"
        if mr.get("pipeline_url"):
            line += f"\n  {mr['pipeline_url']}"
        return line
    return f"PR #{mr['iid']} → {mr['target']} | checks triggered"


def _watch_target(mr: Optional[dict]) -> Optional[tuple[str, str]]:
    """(watch-source, id) for the open MR/PR, or None."""
    if not mr or mr.get("iid") in (None, "?"):
        return None
    source = "gitlab-mr" if mr["source"] == "gitlab" else "github-pr"
    return source, str(mr["iid"])


def _spawn_watch(source: str, iid: str) -> bool:
    """Fire-and-forget a background watch poller via the repo-root supertool."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    st = os.path.join(root, "supertool")
    try:
        subprocess.Popen([st, f"watch:{source}:{iid}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def _uncommitted_leftovers() -> list[str]:
    """Working-tree changes NOT in this push — the 'forgot to commit X' catch."""
    r = _git(["status", "--porcelain"])
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _discarded_by_force(old_remote_sha: str) -> list[str]:
    """Commits that were on the old remote tip but are now off the branch."""
    if not old_remote_sha:
        return []
    r = _git(["log", "--format=%h %an: %s", old_remote_sha, "--not", "HEAD"])
    if r.returncode != 0:
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _post_push_advisories(mr: Optional[dict], flags: set[str]) -> None:
    """Surface the next-decision signals: mergeability, stale base, leftovers, watch."""
    if mr and mr.get("merge_status") in ("cannot_be_merged", "conflict", "broken_status"):
        print(f"⚠ MR conflicts with {mr.get('target', 'target')} — "
              "won't merge until rebased/resolved")

    target = mr.get("target") if mr else ""
    if target and target != "?":
        cnt = _git(["rev-list", "--count", f"HEAD..origin/{target}"])
        if cnt.returncode == 0 and cnt.stdout.strip().isdigit():
            behind = int(cnt.stdout.strip())
            if behind:
                print(f"⚠ {behind} commit(s) behind origin/{target} — "
                      "consider rebasing (stale base under review)")

    leftovers = _uncommitted_leftovers()
    if leftovers:
        print(f"⚠ {len(leftovers)} change(s) NOT in this push (uncommitted):")
        for ln in leftovers[:_LEFTOVER_CAP]:
            print(f"  {ln}")
        if len(leftovers) > _LEFTOVER_CAP:
            print(f"  … +{len(leftovers) - _LEFTOVER_CAP} more")

    wt = _watch_target(mr)
    if wt:
        source, iid = wt
        if "watch" in flags and _spawn_watch(source, iid):
            print(f"Watching → notifies on pipeline finish/fail "
                  f"(unwatch: ./supertool 'unwatch:{source}:{iid}')")
        else:
            print(f"Watch pipeline: ./supertool 'watch:{source}:{iid}'")


def _success_receipt(branch: str, remote_before: str, upstream: str,
                     flags: set[str]) -> None:
    """Shared 'what landed' tail — remote diff, ahead/behind, MR line, advisories."""
    upstream = upstream or _upstream_ref()
    remote_after = _remote_sha(upstream)
    if remote_before and remote_after and remote_before != remote_after:
        rng = _git(["rev-list", "--count", f"{remote_before}..{remote_after}"])
        n = rng.stdout.strip() if rng.returncode == 0 else "?"
        print(f"Remote {remote_before} → {remote_after} ({n} commit(s))")
    elif not remote_before and remote_after:
        print(f"Remote now at {remote_after} (branch created)")
    elif remote_before and remote_after and remote_before == remote_after:
        print("Already up to date — nothing to push")
    else:
        # Push succeeded but the remote-tracking SHA isn't locally resolvable
        # (shallow clone, odd remote layout). Don't claim up-to-date.
        print("Pushed — remote ref not locally resolvable for a before/after diff")

    ab = _git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    if ab.returncode == 0:
        parts = ab.stdout.strip().split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
            if ahead or behind:
                print(f"vs upstream: ahead {ahead}, behind {behind}")
            else:
                print("vs upstream: in sync")

    mr = query_open_mr(branch)
    mr_line = _open_mr_line(mr)
    if mr_line:
        print(mr_line)
    _post_push_advisories(mr, flags)


def _report_hook_pushed(head_before: str, head_after: str,
                        remote: str, ref: str, remote_sha: str,
                        branch: str, flags: set[str]) -> None:
    """Receipt for the 'non-zero exit but the ref actually moved' case."""
    if head_after != head_before:
        print("Status: PUSHED (pre-push hook amended HEAD) ✓")
        print(f"Local HEAD rewritten {head_before[:7]} → {head_after[:7]}")
    else:
        print("Status: pushed ✓ (pre-push hook exited non-zero; "
              "remote already matches HEAD)")
    print(f"Remote {remote}/{ref} now at {remote_sha[:7]}")
    # This IS a landed push — surface the same next-decision signals as the
    # normal success path (mergeability, stale base, leftovers, watch).
    mr = query_open_mr(branch)
    mr_line = _open_mr_line(mr)
    if mr_line:
        print(mr_line)
    _post_push_advisories(mr, flags)


_INCOMING_CAP = 5


def _incoming_commits(ref: str) -> tuple[list[str], int, int]:
    """After a fetch: (incoming commit lines, #remote-added, #local-to-replay).

    Incoming = commits `ref` has that we lack (HEAD..ref), each as
    'sha author: subject' — the authorship is what tells force-vs-integrate
    apart: forcing over a teammate's commit destroys it. `ref` is an explicit
    remote ref (origin/foo), not @{upstream}, so this works on a first push
    that has no tracking ref yet.
    """
    log = _git(["log", "--format=%h %an: %s", f"HEAD..{ref}"])
    incoming = [ln for ln in log.stdout.splitlines() if ln.strip()]
    mine = _git(["rev-list", "--count", f"{ref}..HEAD"])
    ahead = int(mine.stdout.strip()) if mine.returncode == 0 and mine.stdout.strip().isdigit() else 0
    return incoming, len(incoming), ahead


def _recover_by_rebase(branch: str, remote_before: str, upstream: str,
                       remote_name: str, remote_ref: str, flags: set[str]) -> int:
    """Remote moved ahead — rebase local work onto it, then push.

    Fetches first so the incoming remote commits (author + subject) can be
    surfaced — that's the signal for force-vs-integrate. Rebases onto the
    explicit remote ref so it works with or without a tracking ref. Clean →
    push + normal receipt. Conflict → leave the rebase paused, list the
    conflicting files, and point at git-conflicts: the resolve/continue/
    abort decision is the caller's, not the tool's.
    """
    target = f"{remote_name}/{remote_ref}"
    print(f"Remote moved ahead — fetching to rebase onto {target}…")
    fetched = _git(["fetch", remote_name, remote_ref], timeout=120)
    if fetched.returncode != 0:
        combined = (fetched.stdout or "") + "\n" + (fetched.stderr or "")
        print(f"Status: PUSH REJECTED ✗ — fetch of {target} failed, cannot rebase")
        err = _first_error_line(combined)
        if err:
            print(f"First error: {err}")
        print("Hint: remote unreachable or ref gone — check connectivity, then retry.")
        return fetched.returncode or 1

    incoming, behind, ahead = _incoming_commits(target)
    if behind:
        print(f"Remote added {behind} commit(s) you lack; replaying {ahead} of yours:")
        for ln in incoming[:_INCOMING_CAP]:
            print(f"  {ln}")
        if behind > _INCOMING_CAP:
            print(f"  … +{behind - _INCOMING_CAP} more")

    rebase = _git(["rebase", target], timeout=120)
    if rebase.returncode != 0:
        # Distinguish a real merge conflict (unmerged paths → leave paused for
        # git-conflicts) from a rebase that never started (bad ref, etc.).
        unmerged = _git(["diff", "--name-only", "--diff-filter=U"])
        files = [f for f in unmerged.stdout.splitlines() if f.strip()]
        combined = (rebase.stdout or "") + "\n" + (rebase.stderr or "")
        if not files:
            _git(["rebase", "--abort"])  # nothing to keep paused; restore clean
            print(f"Status: PUSH REJECTED ✗ — rebase onto {target} could not start")
            err = _first_error_line(combined)
            if err:
                print(f"First error: {err}")
            print("\n--- git output ---")
            print(combined.strip() or "(no output)")
            return rebase.returncode
        # Real conflict — leave it paused (don't abort) so git-conflicts can
        # read the blocks. Non-clean but explicit; the receipt names the way out.
        print("Status: REBASE PAUSED ✗ — conflict (remote and local both changed):")
        for f in files:
            print(f"  {f}")
        print("Inspect: ./supertool 'git-conflicts'  — every conflict block + abort hint")
        if behind:
            print("Before you force: that would discard the remote commit(s) listed "
                  "above — check the author first.")
        print("Then decide:")
        print("  • keep both — resolve, then `git rebase --continue` && `git-push`")
        print("  • cancel — `git rebase --abort` (back to before the push, nothing changed)")
        print("  • force yours over remote — `git rebase --abort`, then `git-push:force-with-lease`")
        return 1

    print("Rebase clean — pushing rebased work")
    push_args = ["push"]
    if "no-verify" in flags:
        push_args.append("--no-verify")
    if not upstream:
        push_args += ["-u", remote_name, "HEAD"]
    result = _git(push_args, timeout=120)
    if result.returncode != 0:
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        print("Status: PUSH REJECTED ✗ (after rebase)")
        err = _first_error_line(combined)
        if err:
            print(f"First error: {err}")
        print("\n--- git output ---")
        print(combined.strip() or "(no output)")
        return result.returncode

    print("Status: pushed ✓ (rebased onto remote)")
    _success_receipt(branch, remote_before, upstream, flags)
    return 0


def main() -> int:
    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if not branch or branch == "HEAD":
        print("ERROR: detached HEAD — checkout a branch before pushing.")
        return 1

    flags = _parse_flags(sys.argv[1:])
    upstream = _upstream_ref()
    has_upstream = bool(upstream)
    remote_before = _remote_sha(upstream) if has_upstream else ""
    remote_name, remote_ref = _split_upstream(upstream, branch)
    head_before = _local_head()

    print(f"# git-push on {branch}")
    if has_upstream:
        print(f"Upstream: {upstream}" + (f" @ {remote_before}" if remote_before else ""))
    else:
        print("Upstream: none — setting on first push (origin)")
    if flags:
        print(f"Flags: {', '.join(sorted(flags))}")

    push_args = ["push"]
    if "force-with-lease" in flags:
        push_args.append("--force-with-lease")
    if "no-verify" in flags:
        push_args.append("--no-verify")
    if not has_upstream:
        push_args += ["-u", "origin", "HEAD"]
    result = _git(push_args, timeout=120)

    combined = (result.stdout or "") + "\n" + (result.stderr or "")

    if result.returncode != 0:
        # The exit code may lie: a pre-push hook that amends HEAD and pushes
        # the fixed commit itself exits non-zero on purpose. Ground truth is
        # the remote ref — if it already matches our (possibly rewritten)
        # HEAD, the content is on the remote. Report honestly.
        head_after = _local_head()
        live = _live_remote_sha(remote_name, remote_ref)
        if live and head_after and live == head_after:
            _report_hook_pushed(head_before, head_after,
                                 remote_name, remote_ref, live, branch, flags)
            return 0

        # Routine recoverable case: remote moved ahead. Rebase onto it and
        # push — unless the caller already chose to force (their decision).
        if _is_non_fast_forward(combined) and "force-with-lease" not in flags:
            return _recover_by_rebase(branch, remote_before, upstream,
                                      remote_name, remote_ref, flags)

        print("Status: PUSH REJECTED ✗")
        err = _first_error_line(combined)
        if err:
            print(f"First error: {err}")
        low = combined.lower()
        if "force-with-lease" in flags and ("stale info" in low or "stale" in low):
            # The lease check failed — the remote moved since you fetched.
            # NOT a server-side rule; a rebase isn't the fix either.
            print("Hint: the lease is stale — remote moved since you last fetched. "
                  "`git fetch` to review the new commits, then retry "
                  "`git-push:force-with-lease`.")
        elif "rejected" in low or "declined" in low:
            print("Hint: rejected by a server-side rule (protected branch / hook), "
                  "not a divergence — check branch protection or the hook output "
                  "above. A rebase will not help.")
        print("\n--- git output ---")
        print(combined.strip() or "(no output)")
        return result.returncode

    print("Status: pushed ✓")
    if "force-with-lease" in flags and remote_before:
        discarded = _discarded_by_force(remote_before)
        if discarded:
            print(f"Force discarded {len(discarded)} remote commit(s) — now off the branch:")
            for d in discarded[:_INCOMING_CAP]:
                print(f"  {d}")
            if len(discarded) > _INCOMING_CAP:
                print(f"  … +{len(discarded) - _INCOMING_CAP} more")
    _success_receipt(branch, remote_before, upstream, flags)
    return 0


if __name__ == "__main__":
    sys.exit(main())
