#!/usr/bin/env python3
"""oss_train — rebase / resolve / push a merge train of this repo's branches.

Usage: oss_train.py <ARG>
    862,860,861   explicit worktree NAMES -> ~/Documents/st-wt/NNN. Names, never
                  paths: see target_error(), and #1246 for what the absence of
                  that check let a caller rebase and force-push
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
import ntpath
import os
import re
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


#: Both separators, on every platform. A backslash is a path separator on
#: Windows and an ordinary filename character on POSIX, so a check reading only
#: `os.sep` would accept `..\outside\seed` on the one platform where it
#: traverses — and this op is developed on macOS.
_SEPARATORS = ("/", "\\")


def resolve_target(num):
    """``(root, path)`` for a target, both fully resolved.

    THE ONLY PLACE THIS IS CALLED FROM IS `check_target()`. Everything else
    takes the resolved path as an argument. That is not tidiness: this function
    reads the filesystem, and a second read after the containment answer has
    been given is the whole of the round-2 finding on #1246.

    This docstring previously said the resolution was threaded from the check
    to the use, and `main()` was calling it again two hundred lines below. The
    sentence is worth leaving in its corrected form because the false version
    is what stopped a reviewer looking: an artifact asserting a property reads,
    to the next person, exactly like the property.
    """
    root = os.path.realpath(wt_root())
    return root, os.path.realpath(os.path.join(root, num))


def target_error(num):
    """The message-only view of `check_target()`, for callers that only ask.

    `main()` must NOT use this: it discards the resolved path, and a caller
    that then derives the path again has re-opened the window this whole
    check exists to close. See `check_target`.
    """
    return check_target(num)[0]


def check_target(num):
    """``(error, path)`` — the verdict, and the path the verdict is ABOUT.

    ONE `realpath` per target, returned to the caller so that the directory
    acted on is the directory that was approved. The first fix for #1246
    unified the two *spellings* of the resolution into `resolve_target()` and
    left the two *resolutions* in place — `target_error()` resolved to decide
    and `main()` resolved again to act — and three artifacts then asserted a
    property the code did not have.

    The round-2 audit reproduced it without monkeypatching: `st-wt/2 ->
    st-wt/benign` at launch, relinked to an outside repository while target
    `1`'s fetch was in flight, and the outside repository was rebased. For
    target k the window is the whole of trains 1..k-1 — a fetch, a rebase and
    a push each — so this is not a theoretical race with a microsecond gap.

    What this does NOT close: the resolved path could itself be replaced at the
    inode level between here and git running. Closing that needs file
    descriptors git does not accept, so it is out of reach rather than
    overlooked. What is closed is the reproduced attack — a name under the root
    re-pointed after it was checked no longer redirects anything, because the
    name is not consulted a second time.

    The verdict itself is `_target_error()`, which takes the resolved path
    rather than fetching its own — that split is what makes "one realpath per
    target" a property of the code rather than a claim about it.
    """
    root, resolved = resolve_target(num)
    return _target_error(num, root, resolved), resolved


def _target_error(num, root, resolved):
    """Why `num` is not a usable worktree name, or None when it is one.

    `resolved` is passed in, never re-derived here: see `check_target`.

    `discover()` — the `all` path — yields directory names it read out of
    `wt_root()`, so `all` could never name anything else. The explicit-list
    path applied no contract at all, and `train()` joins its target straight
    onto the root: an absolute `num` makes `os.path.join` discard the root
    entirely, a `../` walks out of it, and `git fetch` / `git rebase` /
    `git-push --force-with-lease` then run in whatever repository that landed
    on. The asymmetry between the two paths was the whole of #1246.

    Two checks, and they answer different questions:

    * the NAME check refuses anything that is not a single directory entry.
      `nested/999` stays inside the root and is still refused: a target the
      `all` path could never produce is a target this op has no reading for,
      and naming the mistake beats naming the symptom.
    * the CONTAINMENT check resolves the join and requires the result to sit
      under the resolved root. This is the load-bearing one — a symlink
      `st-wt/evil -> ~/Documents/claude-supertool` is a plain name holding no
      separator, and no amount of string inspection can see through it.

    Note which boundary this is. The core's `_containment_error` measures
    against the CWD, so `$PWD/seed` passes it while pointing outside
    `wt_root()`. The boundary that matters here is the root, and this op is the
    only thing that knows what its root is.

    `isdigit()` — `discover()`'s own filter — is deliberately NOT the contract.
    It is a rule about what `all` SWEEPS, not about what a caller may name:
    `st-wt/scope`, `st-wt/jit` and `st-wt/contrib-skill` are real worktrees
    holding real branches, and since `all` already skips them, an explicit name
    is the only way to train one. Refusing non-digits would remove a working
    capability to close a hole that containment closes anyway.
    """
    if not num:
        return "empty target"
    if num in (".", ".."):
        return "a directory reference, not a worktree name"
    # Absolute FIRST, so the message names what actually happens. Both orders
    # refuse, but on POSIX every absolute path also holds a separator, and
    # "contains a path separator" is a weaker thing to say about `/etc` than
    # naming the join that silently drops the root. `splitdrive` is the Windows
    # half: `C:foo` is drive-relative, holds no separator, and `os.path.join`
    # discards the root for it too.
    if os.path.isabs(num) or os.path.splitdrive(num)[0] or ntpath.splitdrive(num)[0]:
        return ("is a path, not a worktree name — os.path.join would discard "
                + wt_root() + " entirely")
    if any(sep in num for sep in _SEPARATORS):
        return ("contains a path separator — targets are worktree NAMES under "
                + wt_root() + ", not paths")
    if resolved == root:
        return "resolves to the worktree root itself, not to a worktree in it"
    if not resolved.startswith(root + os.sep):
        return "resolves to " + resolved + ", outside " + root
    return None


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


#: A Windows drive prefix: exactly one ASCII letter, then the colon, at the
#: start of a token. Anchored and single-letter on purpose — `all:dry` and
#: `999:dry` must keep splitting.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _colon_split(token):
    """Split one comma element on ':', leaving a Windows drive's colon alone."""
    prefix = ""
    if _DRIVE_PREFIX.match(token):
        prefix, token = token[:2], token[2:]
    parts = token.split(":")
    parts[0] = prefix + parts[0]
    return parts


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

    ONE colon is not a separator: a Windows drive prefix (#1247). The blanket
    rewrite cut `C:\\Users\\x\\seed` in half at its drive letter and produced the
    two targets `C` and `\\Users\\x\\seed`. The VERDICT survived that — the
    second half is still rooted, so the run was refused and nothing was
    fetched — but the ECHO did not: the refusal named a string the caller never
    typed, while the wt_root half of the same sentence still carried its drive.
    A refusal that misnames what it refused is a misreport in the one line
    whose entire job is naming the rejected target, and it invites the reader
    to conclude a different argument was rejected. It also made
    `target_error`'s drive-relative arm unreachable from the CLI, so the fix
    turns an advertised-but-dead message back into a live one.

    The narrowing costs one shape: a single-letter target followed by the colon
    flag, `oss_train:a:dry`, now reads as the drive-ish token `a:dry` rather
    than as target `a`. A one-character worktree name is not a thing here and a
    drive letter is; the comma form `a,dry` is unaffected either way.
    """
    tokens = [t.strip() for a in argv for c in a.split(",")
              for t in _colon_split(c) if t.strip()]
    dry = "dry" in tokens
    return ",".join(t for t in tokens if t != "dry"), dry


def classify_push(push_output):
    """(state, detail) from git-push's own `[result]` verdict.

    Read as a PREFIX of the verdict text, never as `"PUSHED" in verdict`:
    `PUSHED` is a substring of `NOT PUSHED`, so that membership test was true
    for every failure git-push emits — `NOT PUSHED - REJECTED`, `- UNVERIFIED`,
    `- REBASE PAUSED`, `- TIMED OUT`, `- already up to date`, `- no push
    attempted`. All six fell through and were tallied as PUSHED, under a detail
    line that read "NOT PUSHED - ..." out loud. An op whose stated purpose is to
    relay git-push's verified verdict rather than assume its own success
    finished by assuming its own success.
    """
    verdict = ""
    for line in push_output.splitlines():
        if line.startswith("[result]"):
            verdict = line[len("[result]"):].strip()
            break
    if not verdict:
        return "FAILED", "git-push printed no [result] verdict — " + push_output.strip()[-250:]
    if not verdict.startswith("PUSHED"):
        return "FAILED", verdict
    return "PUSHED", verdict


def train(num, dry, wt):
    """`wt` is the path `check_target()` resolved and contained (#1246).

    REQUIRED, with no default. It had one — `wt=None`, falling back to a fresh
    `resolve_target(num)` for the convenience of a direct caller in the tests —
    and that default is a second filesystem read sitting behind an argument
    nobody has to pass. The round-2 finding was exactly a second read, so a
    convenience default that reintroduces one is not a convenience. `num`
    remains only for the label and the messages.
    """
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
    if rc != 0:
        # A status this op could not read is not a clean tree. `rc == 0 and
        # dirty.strip()` read it as one, so the single guard between this op and
        # a live agent's worktree opened exactly when the command it depends on
        # stopped answering, and the run went on to fetch, rebase and force-push.
        return "FAILED", f"could not read the worktree status: {dirty.strip()[:200]}"
    if dirty.strip():
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
    return classify_push(push)


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

    # ONE contract, applied to BOTH lists (#1246). `discover()` already returns
    # only names, so running its output through the same check asserts a
    # single-implementation property rather than adding a second behaviour —
    # #882 was a copy of a containment rule written beside the real one, and
    # the copy covered a case the original missed.
    #
    # A bad target refuses the WHOLE run, above the header, before any fetch.
    # A train that rebased the first three targets and then declined the fourth
    # would be a warning it proceeded past, not a refusal.
    checked = [(n,) + check_target(n) for n in nums]
    rejected = [(n, why) for n, why, _wt in checked if why]
    if rejected:
        for n, why in rejected:
            # Quoted, NOT repr()'d. repr escapes, and on Windows every path
            # separator is a backslash, so `C:\Users\x\seed` renders with each
            # one doubled — a string the caller never typed, in the one line
            # whose whole job is naming which target was rejected. The quotes
            # are what make an empty or space-padded target visible; the
            # escaping is what corrupts the common case (#1247).
            print("ERROR: refusing '" + n + "': " + why)
        print("Targets are worktree directory NAMES under " + wt_root()
              + " — nothing was fetched, rebased or pushed:")
        print("  oss_train:862,860      only these")
        print("  oss_train:all          every numbered worktree there")
        return 2

    # Header first, so the mode is stated even when there is nothing to do — a
    # run that says nothing about whether it would have pushed is unreadable.
    print(f"# oss_train ({len(nums)} branch(es)){' — DRY RUN' if dry else ''}")
    if not nums:
        print("no worktrees to run — nothing under " + wt_root())
        return 0
    tally = {}
    for num, _why, wt in checked:
        # `wt` comes from the check above and is NOT re-derived here. That
        # second derivation was the round-2 finding: it re-read the filesystem
        # after the containment answer had been given, so a symlink re-pointed
        # during an earlier target's fetch redirected this one (#1246).
        #
        # Label by the branch git reports, never by the directory (#910).
        # `fix/{num}` was invisible whenever the convention held and
        # actionable-but-wrong when it did not: st-wt/749 renders as fix/749,
        # and no such branch exists anywhere.
        label = branch_of(wt) or f"st-wt/{num} (no branch)"
        state, detail = train(num, dry, wt)
        tally[state] = tally.get(state, 0) + 1
        mark = {"PUSHED": "✓", "CURRENT": "·", "DRY": "~",
                "REFUSED": "⊘", "BUSY": "⏸"}.get(state, "✗")
        print(f"  {mark} {label}: {state} — {detail}")

    print("[result] " + " | ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    # Non-zero when anything needs a human: a refusal or a failure.
    return 1 if (tally.get("REFUSED") or tally.get("FAILED")) else 0


if __name__ == "__main__":
    sys.exit(main())
