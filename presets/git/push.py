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

Hook output is never evidence about the remote. The auto-rebase above is the
one path here that rewrites local history, so it fires only on git's own
machine-readable answer: the push runs `--porcelain` and the per-ref status
line for our ref, on stdout, is the sole input to the decision. A pre-push
hook shares stdout/stderr with git, and it used to be enough for one to print
`fetch first` in its own advice to make this op fetch and rebase the caller's
branch (#641). When a hook blocks the push, git never reaches the remote and
emits no status line at all — that is an undetermined state, and it fails
loudly here rather than falling through into the recovery path.

A pre-push hook that auto-fixes files commonly amends HEAD, pushes the
corrected commit itself, then exits non-zero so git won't also push the
stale pre-amend ref. That non-zero exit is *success*, not failure — the
ref moved. We trust the live remote SHA over the exit code: when the
remote already matches local HEAD, we report PUSHED, not REJECTED.

The same rule holds for a timeout. A hook running static analysis over
every changed file can outlast the budget *after* git has already handed
the refs to the remote, so a clock expiring is not evidence of failure for
an op that mutates remote state — the remote ref is. On timeout we ask
ls-remote and report PUSHED when it matches HEAD; only a remote that
genuinely did not move gets a failing verdict. This is why the push
budget must stay strictly under the op-level cap in presets/git.json: a
process killed by the outer cap can't verify anything, and the caller is
left acting on a bare `FAIL (timeout …)` for a push that landed (#399).
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import (  # noqa: E402
    _first_error_line,
    _git,
    query_open_mr,
    use_utf8_stdout,
)

_KNOWN_FLAGS = ("force-with-lease", "no-verify", "watch")

# Budget for a single `git push` invocation. Must stay strictly below the
# git-push op timeout in presets/git.json so this script — not supertool's
# outer cap — owns the timeout and can verify the remote before reporting.
_PUSH_TIMEOUT = 300

# Budget for each git call on the non-fast-forward recovery path (fetch,
# rebase). Named rather than inline because these are the calls whose expiry
# can land on a worktree git has already paused (#640) — see
# _report_recovery_timeout.
_RECOVER_TIMEOUT = 120


def _split_flags(argv: list[str]) -> tuple[set[str], list[str]]:
    """(recognised flags, unrecognised tokens) from colon-split argv.

    The second half is the point (#647). The parser used to be `if t in
    _KNOWN_FLAGS: flags.add(t)` with no else, so `git-push:no-verifyy` ran an
    ordinary *verified* push while the caller believed the hook was skipped —
    and `:watch`, advertised in presets/git.json but absent from _KNOWN_FLAGS,
    rotted undetected for the same reason. A request the op cannot honour is
    reported, never discarded.
    """
    known: set[str] = set()
    unknown: list[str] = []
    for tok in argv:
        t = tok.strip().lower()
        if not t:
            continue
        if t in _KNOWN_FLAGS:
            known.add(t)
        else:
            unknown.append(tok.strip())
    return known, unknown


def _parse_flags(argv: list[str]) -> set[str]:
    """Recognised flags only — see _split_flags for what happens to the rest."""
    return _split_flags(argv)[0]


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
    try:
        r = _git(["ls-remote", remote, ref], timeout=30)
    except subprocess.TimeoutExpired:
        return ""
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.split()[0]
    return ""


def _split_upstream(upstream: str, branch: str) -> tuple[str, str]:
    """(remote, ref) from an upstream like 'origin/foo'; fall back to origin."""
    if "/" in upstream:
        remote, ref = upstream.split("/", 1)
        return remote, ref
    return "origin", branch


# Summaries git uses for "your ref diverged from the remote's" — the one
# rejection a rebase actually recovers. Matched against the porcelain status
# summary for our own ref, never against free text (#641).
_NFF_SUMMARIES = ("non-fast-forward", "fetch first",
                  "tip of your current branch is behind")


def _ref_status(push_stdout: str, ref: str) -> str:
    """git's own rejection summary for `ref`, or '' if it never reported one.

    Reads the stdout of `git push --porcelain`, whose per-ref status lines have
    a fixed three-field grammar:

        <flag> TAB <from>:<to> TAB <summary>

    e.g. `!\\trefs/heads/feat:refs/heads/feat\\t[rejected] (fetch first)`. Only
    the `!` (rejected) flag is of interest, and only for the ref we pushed.

    This is the whole point of #641. The previous predicate scanned the merged
    stdout+stderr of the push subprocess, which is also where a pre-push hook
    writes — so a hook printing `fetch first` in its own advice was read as the
    remote rejecting the push, and the op rebased the caller's branch on the
    strength of a substring in text it did not produce. `--porcelain` separates
    the channels at the source: a push a hook blocked never reaches the remote,
    so git emits no status line at all and stdout comes back empty. For hook
    output to reach this function it would have to be a tab-separated line
    carrying the `!` flag *and* naming our exact ref — not something a hook
    does by accident.

    Returning '' therefore means "git did not say" — never "git said no". The
    caller must not treat it as a divergence; see main().
    """
    want = ref.rsplit("/", 1)[-1]
    for line in push_stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] != "!":
            continue
        if parts[1].split(":", 1)[-1].rsplit("/", 1)[-1] != want:
            continue
        return parts[2].strip()
    return ""


def _is_non_fast_forward(push_stdout: str, ref: str) -> bool:
    """True only when git itself rejected OUR ref for divergence.

    `[remote rejected]` is deliberately excluded: a pre-receive hook or branch
    protection declining the push is a server-side rule, not a divergence, and
    rebasing does not help — the receipt already says so.
    """
    status = _ref_status(push_stdout, ref)
    if not status.lower().startswith("[rejected]"):
        return False
    low = status.lower()
    return any(marker in low for marker in _NFF_SUMMARIES)


def _result(verdict: str) -> None:
    """Terminal one-line verdict — always the LAST line the op prints.

    Issue #623: a receipt whose tail is an untracked-file list makes the
    caller run `git fetch` + `git log` to learn the one thing the op was run
    to tell them. Being *present* in the output is not enough — the verdict
    has to survive `| tail -3`, which means being last. Every return path
    from main() ends here, including the ones where no push was attempted.
    """
    print(f"[result] {verdict}")


def _push_verdict(moved: bool, branch: str, remote: str, ref: str,
                  tracking_sha: str, ncommits: str) -> None:
    """Verdict for a push git reported as successful.

    The sha is read back off the real remote (ls-remote), not just the local
    remote-tracking ref, so the caller does not have to fetch to trust it —
    that fetch is precisely the round-trip this op exists to remove. When the
    remote does not answer we say `unverified` and fall back to the tracking
    sha, labelled: a sha we did not read is never printed as if we had.
    """
    live = _live_remote_sha(remote, ref)
    head = _local_head()
    target = f"{remote}/{ref}"
    if live:
        sha = live[:7]
        note = ("verified" if (head and live == head)
                else "verified, but remote != local HEAD")
    else:
        sha = tracking_sha or "unknown"
        note = "unverified - remote did not answer ls-remote"
    if moved:
        extra = f", {ncommits} commit(s)" if ncommits else ""
        _result(f"PUSHED  {branch} -> {target} @ {sha}  ({note}{extra})")
    else:
        _result(f"NOT PUSHED - already up to date  {branch} -> {target} "
                f"@ {sha}  ({note})")


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


def _repo_root() -> str:
    """Directory holding the `supertool` wrapper and `supertool.py`."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _watch_argv(source: str, iid: str) -> tuple[list[str], str]:
    """(argv, how) for the background watcher; ([], reason) when none works.

    The `./supertool` wrapper is a gitignored symlink, so in a git worktree —
    the exact environment agents work in — it is absent and the Popen used to
    fail into a swallowed OSError (#642). `python3 supertool.py` is the
    working invocation there, so it is the fallback rather than a dead end.
    """
    root = _repo_root()
    arg = f"watch:{source}:{iid}"
    wrapper = os.path.join(root, "supertool")
    if os.path.isfile(wrapper) and os.access(wrapper, os.X_OK):
        return [wrapper, arg], wrapper
    entry = os.path.join(root, "supertool.py")
    if os.path.isfile(entry):
        return [sys.executable, entry, arg], f"{sys.executable} {entry}"
    return [], f"no runnable supertool at {root} (neither ./supertool nor supertool.py)"


def _spawn_watch(source: str, iid: str) -> tuple[bool, str]:
    """Fire-and-forget a background watch poller. (started?, how-or-why-not).

    The reason is returned rather than dropped: `:watch` is an explicit
    request, and the caller who is told nothing walks away believing a
    watcher exists.
    """
    argv, how = _watch_argv(source, iid)
    if not argv:
        return False, how
    try:
        subprocess.Popen(argv, cwd=_repo_root(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, f"{how} ({exc})"
    return True, how


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


def _stale_base_advisory(target: str, remote: str) -> None:
    """Commits the review target has that we lack — or a stated skip (#642).

    The remote is the branch's *actual* upstream, never a hardcoded `origin`.
    On a fork/upstream layout `origin/<target>` does not resolve, the count
    exits non-zero, and the warning simply did not print: a genuinely stale
    base rendered exactly like a fresh one, and the caller pushed onto an
    out-of-date base believing the op had checked.

    Three states, not two (docs/validators.md). A ref we cannot resolve is an
    absence of information and says so, so that silence from this function
    means one thing only: the check ran and the base is fresh.
    """
    ref = f"{remote}/{target}"
    cnt = _git(["rev-list", "--count", f"HEAD..{ref}"])
    if cnt.returncode != 0 or not cnt.stdout.strip().isdigit():
        print(f"⚠ stale-base check skipped — {ref} does not resolve locally, "
              f"so how far behind the target you are is UNKNOWN "
              f"(enable it: git fetch {remote} {target})")
        return
    behind = int(cnt.stdout.strip())
    if behind:
        print(f"⚠ {behind} commit(s) behind {ref} — "
              "consider rebasing (stale base under review)")


def _watch_advisory(mr: Optional[dict], flags: set[str]) -> None:
    """The watch line — which of four states we are in, never silence (#642/#647).

    `:watch` used to be unreachable (dropped by the flag parser) and, once
    reached, could fail to spawn in a worktree with the OSError swallowed. So
    a requested watcher that does not exist is now named as such, together
    with the command that does work.
    """
    wt = _watch_target(mr)
    if "watch" not in flags:
        if wt:
            print(f"Watch pipeline: ./supertool 'watch:{wt[0]}:{wt[1]}'")
        return
    if not wt:
        print("⚠ :watch requested, but there is no open MR/PR for this branch "
              "yet — nothing to watch. Open one, then: "
              "./supertool 'watch:gitlab-mr:<iid>'")
        return
    source, iid = wt
    started, how = _spawn_watch(source, iid)
    if started:
        print(f"Watching → notifies on pipeline finish/fail "
              f"(unwatch: ./supertool 'unwatch:{source}:{iid}')")
        return
    print(f"⚠ :watch requested but the watcher could not be started — {how}")
    print(f"Run it yourself: ./supertool 'watch:{source}:{iid}'")


def _post_push_advisories(mr: Optional[dict], flags: set[str],
                          remote: str) -> None:
    """Surface the next-decision signals: mergeability, stale base, leftovers, watch.

    `remote` is the branch's upstream remote — required, not defaulted, so a
    new call site cannot quietly reintroduce the hardcoded `origin` of #642.
    """
    if mr and mr.get("merge_status") in ("cannot_be_merged", "conflict", "broken_status"):
        print(f"⚠ MR conflicts with {mr.get('target', 'target')} — "
              "won't merge until rebased/resolved")

    target = mr.get("target") if mr else ""
    if target and target != "?":
        _stale_base_advisory(target, remote)

    # Count, not a listing (#623): on a tree full of generated junk the list
    # crowded the push verdict off the end of the output. The "did I forget to
    # stage something?" signal is the count; the files stay one op away.
    leftovers = _uncommitted_leftovers()
    if leftovers:
        print(f"⚠ {len(leftovers)} change(s) NOT in this push (uncommitted) — "
              "list them: ./supertool 'git-status:full'")

    _watch_advisory(mr, flags)


def _success_receipt(branch: str, remote_before: str, upstream: str,
                     flags: set[str]) -> None:
    """Shared 'what landed' tail — remote diff, ahead/behind, MR line, advisories."""
    upstream = upstream or _upstream_ref()
    remote_after = _remote_sha(upstream)
    moved, ncommits = True, ""
    if remote_before and remote_after and remote_before != remote_after:
        rng = _git(["rev-list", "--count", f"{remote_before}..{remote_after}"])
        n = rng.stdout.strip() if rng.returncode == 0 else "?"
        ncommits = n
        print(f"Remote {remote_before} → {remote_after} ({n} commit(s))")
    elif not remote_before and remote_after:
        print(f"Remote now at {remote_after} (branch created)")
    elif remote_before and remote_after and remote_before == remote_after:
        moved = False
        print("Already up to date — nothing to push")
    else:
        # Push succeeded but the remote-tracking SHA isn't locally resolvable
        # (shallow clone, odd remote layout). Don't claim up-to-date.
        remote_after = ""
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
    remote_name, remote_ref = _split_upstream(upstream, branch)
    _post_push_advisories(mr, flags, remote_name)
    _push_verdict(moved, branch, remote_name, remote_ref, remote_after, ncommits)


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
    _post_push_advisories(mr, flags, remote)
    _result(f"PUSHED  {branch} -> {remote}/{ref} @ {remote_sha[:7]}  "
            "(verified - pre-push hook pushed it, remote matches HEAD)")


def _report_push_timeout(branch: str, head_before: str,
                         remote: str, ref: str, flags: set[str]) -> int:
    """Verdict for a push that outlasted its budget — decided by the remote ref.

    The clock says nothing about whether the refs landed. ls-remote does: if it
    already matches our (possibly hook-rewritten) HEAD, the push succeeded and
    reporting failure would send the caller into a re-push / force-push it must
    not do. Only a remote that did not move gets a failing verdict, and even
    then it is reported as *unverified*, not rejected — the push may still be
    in flight server-side.
    """
    head_after = _local_head()
    live = _live_remote_sha(remote, ref)
    print(f"Push exceeded its {_PUSH_TIMEOUT}s budget — asking the remote what landed…")
    if live and head_after and live == head_after:
        print("Status: pushed ✓ (push timed out locally; remote ref matches HEAD)")
        if head_after != head_before:
            print(f"Local HEAD rewritten {head_before[:7]} → {head_after[:7]}")
        print(f"Remote {remote}/{ref} now at {live[:7]}")
        print(f"Push outlasted its {_PUSH_TIMEOUT}s budget (slow pre-push hook "
              "or transfer) — raise ops.git-push.timeout in .supertool.json to "
              "see the full receipt.")
        mr = query_open_mr(branch)
        mr_line = _open_mr_line(mr)
        if mr_line:
            print(mr_line)
        _post_push_advisories(mr, flags, remote)
        _result(f"PUSHED  {branch} -> {remote}/{ref} @ {live[:7]}  "
                "(verified - push timed out locally, remote matches HEAD)")
        return 0
    print("Status: PUSH TIMED OUT ✗ — remote ref does NOT match local HEAD")
    print(f"local HEAD {head_after[:7] or 'unknown'} | "
          f"remote {remote}/{ref} at {live[:7] or 'unknown'}")
    print("The push may still be in flight — `git fetch` and re-check before "
          "retrying; do NOT force-push on a timeout alone.")
    _result(f"NOT PUSHED - UNVERIFIED  {branch} -> {remote}/{ref} - push timed "
            f"out and the remote does not match local HEAD "
            f"(remote {live[:7] or 'unknown'}, HEAD {head_after[:7] or 'unknown'})")
    return 1


def _rebase_state() -> str:
    """'in-progress' | 'not-started' | 'unknown' — three states, not two (#640).

    Read off git's own directory layout: `rebase-merge` (the merge/interactive
    backend) or `rebase-apply` (am backend) exists for exactly as long as a
    rebase is paused. Resolved through `rev-parse --git-path` rather than
    assembled by hand so it is correct in a worktree, where `.git` is a file
    and the real gitdir lives elsewhere.

    When git cannot be asked — it timed out, or the command failed — the state
    is *unknown* and says so. Defaulting to 'not-started' would tell a caller
    whose worktree git has just paused that nothing happened, which is the
    single worst thing this receipt could claim.
    """
    paths: list[str] = []
    for name in ("rebase-merge", "rebase-apply"):
        try:
            r = _git(["rev-parse", "--git-path", name], timeout=10)
        except subprocess.TimeoutExpired:
            return "unknown"
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown"
        paths.append(r.stdout.strip())
    return "in-progress" if any(os.path.exists(p) for p in paths) else "not-started"


def _report_recovery_timeout(stage: str, branch: str, target: str) -> int:
    """Verdict for a fetch/rebase that outlasted its budget (#640).

    The exception this replaces was raised out of main() as a bare traceback,
    and it can fire *after* `git rebase` has left the tree paused: the caller
    got a stack trace and a half-rebased worktree with no statement of either.
    A traceback is not a verdict — it says the tool broke, not what state the
    repository is now in.

    This is a receipt, not a suppression. The op still fails (returns 1); what
    changes is that the failure names the worktree state and the way out of it.
    """
    stage_up = stage.upper()
    state = _rebase_state()
    print(f"Status: {stage_up} TIMED OUT ✗ — exceeded its {_RECOVER_TIMEOUT}s "
          f"budget while recovering the non-fast-forward push")
    if state == "in-progress":
        print("Your worktree has a REBASE IN PROGRESS — git paused it and the "
              "clock ran out before it finished. Nothing was pushed.")
        print("Inspect: ./supertool 'git-conflicts'")
        print("Then decide:")
        print("  • finish it — resolve if needed, then `git rebase --continue`")
        print("  • undo it — `git rebase --abort` (back to before the push, "
              "nothing changed)")
        _result(f"NOT PUSHED - {stage_up} TIMED OUT ({_RECOVER_TIMEOUT}s)  "
                f"{branch} -> {target} - REBASE IN PROGRESS: finish with "
                "`git rebase --continue` or undo with `git rebase --abort`")
    elif state == "not-started":
        print("No rebase is in progress — the working tree is unchanged and "
              "your branch is where it was.")
        print(f"Retry. If this repo genuinely needs more than "
              f"{_RECOVER_TIMEOUT}s to {stage}, the budget is _RECOVER_TIMEOUT "
              "in presets/git/push.py — raising ops.git-push.timeout alone "
              "will not move it.")
        _result(f"NOT PUSHED - {stage_up} TIMED OUT ({_RECOVER_TIMEOUT}s)  "
                f"{branch} -> {target} - no rebase started, working tree "
                "unchanged")
    else:
        print("Could NOT determine whether a rebase is in progress — git did "
              "not answer. Your worktree may or may not be paused mid-rebase.")
        print("Check before anything else: `git status`")
        _result(f"NOT PUSHED - {stage_up} TIMED OUT ({_RECOVER_TIMEOUT}s)  "
                f"{branch} -> {target} - rebase state UNKNOWN, run "
                "`git status` before retrying")
    return 1


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
    try:
        fetched = _git(["fetch", remote_name, remote_ref],
                       timeout=_RECOVER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _report_recovery_timeout("fetch", branch, target)
    if fetched.returncode != 0:
        combined = (fetched.stdout or "") + "\n" + (fetched.stderr or "")
        print(f"Status: PUSH REJECTED ✗ — fetch of {target} failed, cannot rebase")
        err = _first_error_line(combined)
        if err:
            print(f"First error: {err}")
        print("Hint: remote unreachable or ref gone — check connectivity, then retry.")
        _result(f"NOT PUSHED - REJECTED (non-fast-forward)  {branch} -> {target} "
                "- fetch failed, could not rebase")
        return fetched.returncode or 1

    # Rebase onto FETCH_HEAD, not origin/<branch>: a one-shot `git fetch origin
    # <branch>` always populates FETCH_HEAD, but only updates the remote-tracking
    # ref refs/remotes/origin/<branch> when a fetch refspec is configured. A
    # fresh worktree / refspec-less clone has no such ref, so rebasing onto
    # origin/<branch> aborts with `fatal: invalid upstream` (issue #354).
    # FETCH_HEAD points at the same commit and is guaranteed present here.
    rebase_target = "FETCH_HEAD"
    incoming, behind, ahead = _incoming_commits(rebase_target)
    if behind:
        print(f"Remote added {behind} commit(s) you lack; replaying {ahead} of yours:")
        for ln in incoming[:_INCOMING_CAP]:
            print(f"  {ln}")
        if behind > _INCOMING_CAP:
            print(f"  … +{behind - _INCOMING_CAP} more")

    try:
        rebase = _git(["rebase", rebase_target], timeout=_RECOVER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _report_recovery_timeout("rebase", branch, target)
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
            _result(f"NOT PUSHED - REJECTED (non-fast-forward)  {branch} -> "
                    f"{target} - rebase could not start")
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
        _result(f"NOT PUSHED - REBASE PAUSED (conflict in {len(files)} file(s))  "
                f"{branch} -> {target} - resolve then `git rebase --continue`, "
                "or `git rebase --abort`")
        return 1

    print("Rebase clean — pushing rebased work")
    push_args = ["push"]
    if "no-verify" in flags:
        push_args.append("--no-verify")
    if not upstream:
        push_args += ["-u", remote_name, "HEAD"]
    try:
        result = _git(push_args, timeout=_PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _report_push_timeout(branch, _local_head(),
                                    remote_name, remote_ref, flags)
    if result.returncode != 0:
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        print("Status: PUSH REJECTED ✗ (after rebase)")
        err = _first_error_line(combined)
        if err:
            print(f"First error: {err}")
        print("\n--- git output ---")
        print(combined.strip() or "(no output)")
        _result(f"NOT PUSHED - REJECTED after a clean rebase  {branch} -> {target}")
        return result.returncode

    print("Status: pushed ✓ (rebased onto remote)")
    _success_receipt(branch, remote_before, upstream, flags)
    return 0


def main() -> int:
    use_utf8_stdout()
    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        _result("NOT PUSHED - no push attempted (not inside a git repository)")
        return 1

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if not branch or branch == "HEAD":
        print("ERROR: detached HEAD — checkout a branch before pushing.")
        _result("NOT PUSHED - no push attempted (detached HEAD - checkout a "
                "branch first)")
        return 1

    flags, unknown = _split_flags(sys.argv[1:])
    if unknown:
        # Refused, not warned-and-pushed (#647). Nothing has changed yet at
        # this point, so the cost of stopping is a retype; the cost of
        # continuing is a push whose behaviour is not the one that was asked
        # for — a dropped `:no-verifyy` runs the very hook it meant to skip.
        listed = ", ".join(unknown)
        print(f"ERROR: unknown flag(s): {listed}")
        print(f"Accepted: {', '.join(_KNOWN_FLAGS)}")
        print("Nothing was pushed. A flag this op cannot honour is refused "
              "rather than silently dropped — re-run without it, or fix the "
              "spelling.")
        _result(f"NOT PUSHED - no push attempted (unknown flag(s): {listed}; "
                f"accepted: {', '.join(_KNOWN_FLAGS)})")
        return 2

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

    # --porcelain is what makes the non-fast-forward decision trustworthy: it
    # moves git's per-ref status onto stdout in a machine-readable grammar,
    # out of the stream a pre-push hook shares with it (#641).
    push_args = ["push", "--porcelain"]
    if "force-with-lease" in flags:
        push_args.append("--force-with-lease")
    if "no-verify" in flags:
        push_args.append("--no-verify")
    if not has_upstream:
        push_args += ["-u", "origin", "HEAD"]
    try:
        result = _git(push_args, timeout=_PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _report_push_timeout(branch, head_before,
                                    remote_name, remote_ref, flags)

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
        if (_is_non_fast_forward(result.stdout or "", remote_ref)
                and "force-with-lease" not in flags):
            try:
                return _recover_by_rebase(branch, remote_before, upstream,
                                          remote_name, remote_ref, flags)
            except subprocess.TimeoutExpired:
                # Backstop for the recovery path's smaller git calls (#640).
                # The fetch and the rebase report their own stage; anything
                # else that expires here still owes the caller a worktree
                # state rather than a traceback.
                return _report_recovery_timeout(
                    "rebase recovery", branch, f"{remote_name}/{remote_ref}")

        print("Status: PUSH REJECTED ✗")
        err = _first_error_line(combined)
        if err:
            print(f"First error: {err}")
        # Every hint below reads git's own ref status, not the merged stream —
        # same channel discipline as the non-fast-forward decision (#641). A
        # hook's advice used to pick which hint the caller was shown.
        status = _ref_status(result.stdout or "", remote_ref)
        low = status.lower()
        if "stale info" in low:
            # The lease check failed — the remote moved since you fetched.
            # NOT a server-side rule; a rebase isn't the fix either.
            print("Hint: the lease is stale — remote moved since you last fetched. "
                  "`git fetch` to review the new commits, then retry "
                  "`git-push:force-with-lease`.")
        elif low.startswith("[remote rejected]") or low.startswith("[remote failure]"):
            print("Hint: rejected by a server-side rule (protected branch / hook), "
                  "not a divergence — check branch protection or the hook output "
                  "above. A rebase will not help.")
        elif status:
            print(f"Hint: git rejected {remote_name}/{remote_ref} — {status}")
        else:
            # No ref status at all: git never got as far as talking to the
            # remote. A local pre-push hook refused, or the transport failed.
            # Declining to guess is the point — this is exactly the state the
            # old predicate used to read as a divergence and rebase on.
            print(f"Hint: git reported no ref status for {remote_name}/{remote_ref} "
                  "— the push was stopped before it reached the remote (local "
                  "pre-push hook, or transport). Not a divergence: a rebase "
                  "would not help. The output below is what stopped it; "
                  "`git-push:no-verify` skips a local hook.")
        print("\n--- git output ---")
        print(combined.strip() or "(no output)")
        _result(f"NOT PUSHED - REJECTED  {branch} -> {remote_name}/{remote_ref}"
                + (f" - {err}" if err else ""))
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
