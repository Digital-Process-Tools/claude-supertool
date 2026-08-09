#!/usr/bin/env python3
"""oss_train — rebase / resolve / push a merge train of this repo's branches.

Usage: oss_train.py <ARG>
    862,860,861   explicit issue numbers -> worktree ~/Documents/st-wt/NNN
    all           every ~/Documents/st-wt/NNN worktree that exists
    dry           as a COMMA element (all,dry) -> report only; reads, rebases
                  nothing, pushes nothing. Stops ABOVE the rebase, not below it.

A bare `oss_train` with no argument is REFUSED rather than treated as `all`.

The flag is comma-separated rather than colon-separated because supertool passes
only the first ':'-token into {file} and drops the rest silently, so `:dry` never
arrives.

Where this came from
--------------------
It was a DVSI project op, which is an accident of where it was written: DVSI is
a different repository on a different forge and this train drives *this* one, so
the op only answered from a checkout that has nothing to do with it. Every other
op the maintainer loop needs already answers here; this was the single measured
reason a session rooted in claude-supertool could not run the loop (#1216).

Why it exists at all
--------------------
Eight PRs in a milestone all touch CHANGELOG.md, so every merge re-conflicts
every other open PR. The loop below therefore runs once per PR *per merge* —
closer to eighteen times across a release than eight. Every individual step
already had an op (git-conflicts, git-resolve, git-push); only the composition
was hand-written, and every mistake made during the 2026-08-05 train was in that
glue rather than in the ops: a resolver that assumed one conflicted file when
there were two, a `rebase --continue` redirected to /dev/null, a
`git push | tail -1` that swallowed the verdict for five branches at once.

Five states per branch, never two
---------------------------------
    PUSHED    rebased (or already rebased) and the new sha was read back off the
              remote
    CURRENT   already on top of the default branch with nothing to push — a
              no-op, said out loud, which is what makes the op idempotent
    BUSY      uncommitted changes; someone is working there, leave it
    REFUSED   git-resolve declined (a source file, or a Markdown heading a union
              would duplicate). The branch is LEFT CONFLICTED on purpose so git
              itself blocks rebase --continue. Never skipped silently.
    FAILED    anything else, with the command output that caused it
    DRY       nothing was done, and the report says what would have been

A train that quietly drops the one branch it could not resolve is this repo's own
defect class wearing a new hat, so REFUSED is a first-class outcome and the exit
code reflects it.

Never pushes without reading the result. `git-push` verifies the remote sha
itself; this op relays that verdict rather than assuming its own success.
"""
import os
import subprocess
import sys

#: Overridable through the op's `wt_root` config key, which supertool passes in
#: as SUPERTOOL_WT_ROOT. A constant here would make every path below reach the
#: real worktrees of the machine running the tests.
DEFAULT_WT_ROOT = "~/Documents/st-wt"

#: The branch every train rebases onto. claude-supertool's default branch is
#: master; claude-remember's is main, and porting this there means changing this
#: line rather than discovering it from a failed fetch.
UPSTREAM = "origin/master"


def wt_root():
    """The directory holding the per-issue worktrees, expanded."""
    return os.path.expanduser(os.environ.get("SUPERTOOL_WT_ROOT") or DEFAULT_WT_ROOT)


def run(args, cwd, timeout=900):
    """Run a command and ALWAYS return (rc, combined output). Never raises."""
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(args)}"
    except OSError as exc:
        return 125, f"could not run {' '.join(args)}: {exc}"


def st(wt, *ops):
    """Run supertool from inside the worktree.

    `sys.executable supertool.py`, never the global `supertool`: inside a
    claude-supertool worktree the PATH wrapper resolves to the live clone
    (master), so a global call would exercise master's code and report on the
    branch's (#678).
    """
    return run([sys.executable, "supertool.py", *ops], wt)


def discover():
    """Every numbered worktree directory under the root, sorted."""
    root = wt_root()
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root)
                  if n.isdigit() and os.path.isdir(os.path.join(root, n)))


def branch_of(wt):
    """The branch actually checked out, or None when detached.

    NEVER infer it from the directory name. st-wt/749 holds `lane-watch`, not
    `fix/749`, so every `fix/{num}` this op used to print was a guess that
    happened to be right while the convention held (#910). A guessed branch name
    breaks two ways at once: the CURRENT check looks up a ref that does not
    exist, so an up-to-date branch gets rebased anyway, and every follow-up
    command printed for the reader names a branch git will not resolve.
    """
    rc, out = run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], wt)
    if rc != 0 or not out.strip():
        return None
    return out.strip()


def parse_tokens(argv):
    """(target, dry) from the raw argument list.

    COMMA, not colon. supertool passes only the FIRST ':'-token after the op
    name into {file} and silently discards the rest, so `oss_train:all:dry`
    arrives as plain "all" and the run pushes. Measured: `oss_train:dry`
    honours the flag, `oss_train:999:dry` does not. Two earlier parses "fixed"
    this and neither did, because both were tested against a shape the tool
    never produces.

        oss_train:all,dry        oss_train:862,860,dry        oss_train:dry

    A colon is still read as a separator, for the case where one does survive:
    treating `all:dry` as an unknown target and running the un-dry path is the
    worse of the two failures by a wide margin.
    """
    tokens = [t.strip() for a in argv for t in a.replace(":", ",").split(",")
              if t.strip()]
    dry = "dry" in tokens
    return ",".join(t for t in tokens if t != "dry"), dry


def train(num, dry):
    wt = os.path.join(wt_root(), num)
    if not os.path.isdir(wt):
        return "FAILED", f"no worktree at {wt}"

    branch = branch_of(wt)
    if branch is None:
        return "FAILED", "detached HEAD — no branch here to rebase or push"

    # A dirty tree means somebody is working here — almost always a live agent.
    # Never touch an agent's active worktree: that rule predates this op and this
    # op broke it on its own first run, walking into st-wt/835 mid-task. git
    # itself refused the rebase (which is the only reason nothing was lost), so
    # this guard turns luck into a decision.
    rc, dirty = run(["git", "status", "--porcelain"], wt)
    if rc == 0 and dirty.strip():
        n = len(dirty.strip().splitlines())
        return "BUSY", f"{n} uncommitted change(s) — someone is working here, not touching it"

    remote, _, ref = UPSTREAM.partition("/")
    rc, out = run(["git", "fetch", "-q", remote, ref], wt)
    if rc != 0:
        return "FAILED", f"fetch: {out.strip()[:200]}"

    # Already on top of the default branch and nothing unpushed? Say so; do not
    # force-push.
    rc, behind = run(["git", "rev-list", "--count", f"HEAD..{UPSTREAM}"], wt)
    rc2, ahead = run(["git", "rev-list", "--count", f"{UPSTREAM}..HEAD"], wt)
    if rc == 0 and rc2 == 0 and behind.strip() == "0":
        rc3, local = run(["git", "rev-parse", "HEAD"], wt)
        rc4, remote_ls = run(["git", "ls-remote", remote, f"refs/heads/{branch}"], wt)
        if rc3 == 0 and rc4 == 0 and remote_ls.split():
            if local.strip() == remote_ls.split()[0]:
                return "CURRENT", (f"{branch} on top of {ref}, remote matches "
                                   f"({ahead.strip()} commit(s))")

    # DRY STOPS HERE, above the rebase — not below it.
    # It used to sit after `git rebase` and skip only the push, so a run whose
    # header said DRY RUN left every branch rewritten. Those branches are checked
    # out in worktrees where agents are working, so the safe-sounding flag moved
    # HEAD underneath live work (#910). A preview that mutates is not a preview,
    # and the three surfaces asserting simulation made it worse.
    #
    # The other reading — keep rebasing, rename the flag honestly — was rejected:
    # what the rebase adds to the report is "would it conflict", and the BUSY
    # guard above only sees UNCOMMITTED work, so an agent between two commits
    # reads as idle. Paying for that answer with somebody else's HEAD is the
    # wrong trade, and a caller who wants it can run the train.
    if dry:
        n_behind = behind.strip() if rc == 0 else "?"
        return "DRY", (f"{branch} is {n_behind} commit(s) behind {UPSTREAM} — would "
                       "rebase and force-push; nothing was touched")

    rc, out = run(["git", "rebase", UPSTREAM], wt)
    if rc != 0:
        # Enumerate EVERY conflicted file. Assuming one is how a branch ended
        # detached with the failure invisible on 2026-08-05.
        _, conflicts = st(wt, "git-conflicts")
        n_files = ""
        for line in conflicts.splitlines():
            if line.startswith("Conflicts:"):
                n_files = line.strip()
                break

        _, res = st(wt, "git-resolve:both:all")
        refused = [ln.strip() for ln in res.splitlines() if ln.strip().startswith("⊘")]
        if refused:
            # Leave it conflicted. git blocks rebase --continue, which is the
            # only signal that does not depend on reading a receipt.
            return "REFUSED", f"{n_files}; " + " | ".join(refused)[:400]

        rc_c, cont = run(["git", "rebase", "--continue"], wt, timeout=300)
        if rc_c != 0:
            run(["git", "rebase", "--abort"], wt)
            return "FAILED", f"rebase --continue: {cont.strip()[:250]} (aborted, branch restored)"

        _, still = st(wt, "git-conflicts")
        if "No conflicted files" not in still:
            return "FAILED", "conflicts remain after resolve+continue"

    _, push = st(wt, "git-push:force-with-lease:no-verify")
    verdict = ""
    for line in push.splitlines():
        if line.startswith("[result]"):
            verdict = line.strip()
            break
    if not verdict:
        return "FAILED", "git-push printed no [result] verdict — " + push.strip()[-250:]
    if "PUSHED" not in verdict:
        return "FAILED", verdict
    return "PUSHED", verdict[len("[result]"):].strip()


def main():
    arg, dry = parse_tokens(sys.argv[1:])

    # A BARE INVOCATION IS REFUSED. It used to mean `all`, so `oss_train` typed
    # to find out whether the op was even registered rebased and force-pushed
    # every worktree on the machine — fourteen of them on 2026-08-07 (#993).
    #
    # A force-push is not `fails-to-preserve`, it is `destroys`: the commits it
    # drops were never in this op's custody, and any commit reachable only from
    # the old ref — a colleague's push you have not fetched, a reflog on a
    # machine you do not own — is gone from the only place it existed. That
    # afternoon survived because every flattened branch happened to be a merged
    # PR, which is a property of that board and not of this op. The correlation
    # runs the wrong way, too: a bare run is most likely on a board whose state
    # you do not know, which is the board most likely to hold a branch nobody
    # has fetched.
    #
    # So a better error message is the wrong fix — the failure is that the
    # caller never formed an intention. Refuse, and name the live count, because
    # "every branch" is abstract and "14 right now" is not.
    if arg == "":
        found = discover()
        print(f"ERROR: oss_train needs an explicit target. This would rebase and "
              f"force-push {len(found)} branch(es) right now"
              + (": " + ", ".join(found) if found else ""))
        print("Say which, once:")
        print("  oss_train:all          every worktree under " + wt_root())
        print("  oss_train:862,860      only these")
        print("  oss_train:all,dry      report only — reads, rebases nothing, pushes nothing")
        return 2

    nums = discover() if arg == "all" else [n.strip() for n in arg.split(",") if n.strip()]
    # Header first, so the mode is stated even when there is nothing to do — a
    # run that says nothing about whether it would have pushed is unreadable.
    print(f"# oss_train ({len(nums)} branch(es)){' — DRY RUN' if dry else ''}")
    if not nums:
        print("no worktrees to run — nothing under " + wt_root())
        return 0
    tally = {}
    for num in nums:
        # Label by the branch git reports, never by the directory (#910).
        # `fix/{num}` was invisible whenever the convention held and
        # actionable-but-wrong when it did not: st-wt/749 renders as fix/749,
        # and no such branch exists anywhere.
        label = branch_of(os.path.join(wt_root(), num)) or f"st-wt/{num} (no branch)"
        state, detail = train(num, dry)
        tally[state] = tally.get(state, 0) + 1
        mark = {"PUSHED": "✓", "CURRENT": "·", "DRY": "~",
                "REFUSED": "⊘", "BUSY": "⏸"}.get(state, "✗")
        print(f"  {mark} {label}: {state} — {detail}")

    print("[result] " + " | ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    # Non-zero when anything needs a human: a refusal or a failure.
    return 1 if (tally.get("REFUSED") or tally.get("FAILED")) else 0


if __name__ == "__main__":
    sys.exit(main())
