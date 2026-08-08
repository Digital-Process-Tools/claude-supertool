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
import traceback
from typing import Optional

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import (  # noqa: E402
    TIMEOUT_RC,
    MrLookup,
    _first_error_line,
    _git,
    install_dir,
    query_open_mr_result,
    reject_fetch_option,
    repo_label,
    st_hint,
    use_utf8_stdout,
)

# `_git` no longer raises `TimeoutExpired` — a stall comes back as a result
# carrying `TIMEOUT_RC` (#704). That is not a cosmetic change in this file: a
# timed-out push falling through to the ordinary `returncode != 0` branch would
# print `Status: PUSH REJECTED ✗` about a push that may well have landed, which
# is the exact class of claim #675 and #663 exist to prevent. Every site below
# that used to `except subprocess.TimeoutExpired` now tests for the code, and
# the `except` clauses that remain are there for `OSError`, which still raises.

# `set-upstream` and `to-upstream` are the two intents the #787 refusal makes
# the caller choose between, made sayable in this op's own vocabulary (#879).
# They are deliberately not one flag with a default: they send the same commits
# to two different refs, and a default would be the guess the refusal exists to
# prevent.
_KNOWN_FLAGS = ("force-with-lease", "no-verify", "watch",
                "set-upstream", "to-upstream")

# Budget for a single `git push` invocation. Must stay strictly below the
# git-push op timeout in presets/git.json so this script — not supertool's
# outer cap — owns the timeout and can verify the remote before reporting.
_PUSH_TIMEOUT = 300

# Budget for each git call on the non-fast-forward recovery path (fetch,
# rebase). Named rather than inline because these are the calls whose expiry
# can land on a worktree git has already paused (#640) — see
# _report_recovery_timeout.
_RECOVER_TIMEOUT = 120

# Budget for the checks that make up the receipt — everything this op runs
# *after* the push has landed. Named for the same reason _PUSH_TIMEOUT is:
# these calls all sit past the point of no return, where an expiring clock is
# not evidence about the remote and must never cost the caller the verdict
# (#675).
_CHECK_TIMEOUT = 30


# How far this run got, and whether it has already spoken. The receipt-level
# invariant — exactly one `[result]` line, and never one claiming a landed push
# failed — cannot be held up by each helper remembering to be careful. That is
# what produced #675: one guarded helper among six unguarded ones, each patched
# when somebody hit it. It is held up here instead, by main() knowing which
# phase it crashed in.
_RUN = {
    "phase": "not-attempted",   # -> "attempted" -> "landed"
    "branch": "",
    "remote": "",
    "ref": "",
    "target": "",
    "verdict": False,
}


def _note_landed(branch: str, remote: str, ref: str) -> None:
    """Record that the remote has moved — every caller of this is past no-return.

    `remote` and `ref` are kept apart as well as joined. The joined form is
    what the receipt prints; the split form is what `git ls-remote` takes, and
    a crash receipt that advises `ls-remote origin/feature` names a remote git
    cannot resolve — advice that looks actionable and is not (#663).
    """
    _RUN.update({"phase": "landed", "branch": branch, "remote": remote,
                 "ref": ref, "target": f"{remote}/{ref}"})


def _checked_git(args: list[str], label: str = "") -> tuple[
        Optional[subprocess.CompletedProcess[str]], str]:
    """Run a receipt check. `(result, "")` if it answered, `(None, why)` if not.

    The generalisation nothing ever did (#675). Every check in this receipt has
    the same three states — it ran and found nothing, it ran and found
    something, it could not run — and the third was spelled a different way at
    each call site, or not at all: `_live_remote_sha` caught `TimeoutExpired`
    but not `OSError`, `_remote_sha` and `_local_head` returned `""` for both
    "no" and "could not ask", and the rest raised straight out of `main()` for
    a push that had already landed.

    `""` is what makes this worth centralising rather than repeating. It reads
    as an *answer* at every call site here, so a per-helper guard returning it
    converts a loud crash into a silent wrong claim — the receipt saying the
    tree is clean, the base is fresh, the remote differs from a HEAD it never
    read. `None` cannot be mistaken for an answer, and `why` is what lets the
    caller name the missing check instead of just printing less receipt.

    A non-zero exit and an exception are deliberately the same state: both mean
    this check has nothing to contribute, and the caller needs to hear which
    command and why either way. Where a non-zero exit is git *answering* the
    question (`@{upstream}` on a branch that has none), the call site says so
    itself rather than coming through here.
    """
    cmd = label or "git " + " ".join(args)
    try:
        r = _git(args, timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return None, f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return None, f"`{cmd}` did not complete — {r.stderr.strip()}"
    if r.returncode != 0:
        why = _first_error_line((r.stdout or "") + "\n" + (r.stderr or ""))
        return None, (f"`{cmd}` exited {r.returncode}"
                      + (f" — {why}" if why else ""))
    return r, ""


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


def _st_hint(arg: str) -> str:
    """A runnable `supertool` invocation for `arg`. For printed remedies.

    A remedy the caller cannot run is not a remedy (#879). Raw `git push` is
    blocked by a hook in the project this op serves — a hook that exists
    precisely *because* `git-push` is better than raw git — so a refusal
    prescribing `git push -u origin HEAD` composed two correct rules into a
    dead end, and the way out the caller found was `git branch
    --unset-upstream`: git trivia that works, which is how the gap survived.

    The `./supertool` wrapper is a gitignored symlink and therefore absent in
    a git worktree, which is where agents work. The fallback is the one
    `_watch_argv` already settled on for that exact environment (#642), not a
    second guess at it.

    The rule moved to `_git_common.st_hint` for #1012, which found the same
    defect in this file's own watch advisory and in `git-conflicts`. It is
    kept as a name here because eleven call sites read better for it.
    """
    return st_hint(arg)


def _upstream_ref() -> tuple[str, str]:
    """Configured upstream of HEAD (e.g. origin/foo). `(ref, could-not-ask)`.

    A non-zero exit is git *answering* this question: this branch has no
    upstream, the ordinary first-push case, and reporting it as a failure would
    make the receipt shout on every new branch. So it does not go through
    `_checked_git`; only the exception is an absence of an answer.

    That distinction is the whole point of the second element. An empty ref
    used to mean both things at once, and the caller falls back to a hardcoded
    `origin` — the exact defect #642 fixed, re-entered through a failure path,
    and then named in a verdict about a remote nobody confirmed (#675).
    """
    cmd = "git rev-parse --abbrev-ref --symbolic-full-name @{upstream}"
    try:
        r = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                  "@{upstream}"], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return "", f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return "", f"`{cmd}` did not complete — {r.stderr.strip()}"
    return (r.stdout.strip(), "") if r.returncode == 0 else ("", "")


def _remote_sha(ref: str) -> tuple[str, str]:
    """Short SHA `ref` resolves to locally. `(sha, could-not-ask)`.

    As with `_upstream_ref`, a non-zero exit is an answer — the ref does not
    resolve in this clone (shallow, odd remote layout), which the receipt
    already knows how to say. An exception is not an answer, and used to be a
    traceback over a push that had landed.
    """
    if not ref:
        return "", ""
    cmd = f"git rev-parse --short {ref}"
    try:
        r = _git(["rev-parse", "--short", ref], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return "", f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return "", f"`{cmd}` did not complete — {r.stderr.strip()}"
    return (r.stdout.strip(), "") if r.returncode == 0 else ("", "")


def _local_head() -> tuple[str, str]:
    """Full SHA of local HEAD. `(sha, why-not)`.

    Both failure modes collapse into one state here on purpose: past the push,
    a `rev-parse HEAD` that exits non-zero and one that never ran leave the
    caller in the same position — unable to compare the remote against local
    work — and `_push_verdict` has to say which, because the alternative is
    the divergence claim #675 found on the `[result]` line.
    """
    r, why = _checked_git(["rev-parse", "HEAD"], "git rev-parse HEAD")
    return ("", why) if r is None else (r.stdout.strip(), "")


def _live_remote_sha(remote: str, ref: str) -> tuple[str, str]:
    """Authoritative remote SHA via ls-remote (full sha). `(sha, why-not)`.

    Reads the real remote, not the local remote-tracking ref — a hook that
    pushes on our behalf moves the remote without us having fetched.

    This was the only guarded helper in the receipt, and the shape of that
    guard is what #675 is about: it caught `TimeoutExpired` because that is
    what somebody hit, and let an `OSError` — git failing to start at all —
    through as a traceback over a landed push. It also returned a bare `""`,
    so every caller reported "remote did not answer ls-remote" no matter what
    actually went wrong, which is the wrong-cause advice #663 warns about.
    """
    if not remote or not ref:
        return "", ""
    cmd = f"git ls-remote {remote} {ref}"
    r, why = _checked_git(["ls-remote", remote, ref], cmd)
    if r is None:
        return "", why
    if r.stdout.strip():
        return r.stdout.split()[0], ""
    return "", f"`{cmd}` returned no ref — the remote does not have {ref}"


def _split_upstream(upstream: str, branch: str,
                    fallback_remote: str) -> tuple[str, str]:
    """(remote, ref) from an upstream like 'origin/foo'.

    `fallback_remote` is required, not defaulted — the same discipline as
    `_post_push_advisories` (#642), for the same reason. It used to be a
    hardcoded `origin`, and on a branch with no upstream that hardcode *was*
    the fallback: in a repo whose only remote is `gitlab`, every downstream
    reader of this pair — the push target, the `ls-remote` verification, the
    crash receipt's settle command — named a remote that does not exist
    (#656). The caller resolves it once through `_resolve_push_remote`, and a
    new call site cannot reintroduce the guess by omitting the argument.
    """
    if "/" in upstream:
        remote, ref = upstream.split("/", 1)
        return remote, ref
    return fallback_remote, branch


# The keys `git push` itself consults, in git's own precedence order, before it
# falls back to `origin`. Reading them is what makes this op target whatever a
# bare `git push` would target; a resolver that invented its own order would be
# a second surprise stacked on the one #656 is about.
_PUSH_REMOTE_KEYS = ("branch.{b}.pushRemote", "remote.pushDefault",
                     "branch.{b}.remote")


def _config_value(key: str) -> tuple[str, str]:
    """`git config --get <key>`. `(value, could-not-ask)`; unset is `("", "")`.

    Exit 1 here is git *answering* "not set" — the ordinary case on nearly
    every branch — so it does not go through `_checked_git`, for the same
    reason `_upstream_ref` does not.
    """
    cmd = f"git config --get {key}"
    try:
        r = _git(["config", "--get", key], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return "", f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return "", f"`{cmd}` did not complete — {r.stderr.strip()}"
    return (r.stdout.strip(), "") if r.returncode == 0 else ("", "")


def _remote_names() -> tuple[list[str], str]:
    """Configured remote names. `([names], could-not-ask)`.

    An empty list means one thing only: this repository has no remotes. A call
    that did not answer returns the reason instead, because the two lead to
    opposite receipts — "you have no remote to push to" versus "I could not
    find out" — and rendering them alike is the defect class this file is
    mostly made of.
    """
    cmd = "git remote"
    try:
        r = _git(["remote"], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return [], f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return [], f"`{cmd}` did not complete — {r.stderr.strip()}"
    if r.returncode != 0:
        return [], f"`{cmd}` exited {r.returncode} — {r.stderr.strip()}"
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()], ""


def _resolve_push_remote(branch: str) -> tuple[str, str, str]:
    """Where a branch with no upstream pushes. `(remote, how, cannot-tell)`.

    Exactly one of `remote` and `cannot-tell` is ever set. The ladder, and why
    each rung sits where it does:

    1. `branch.<b>.pushRemote`, `remote.pushDefault`, `branch.<b>.remote` — in
       git's own precedence order for a bare `git push`. Configuration is not a
       guess, and honouring it means this op pushes where plain `git push`
       would. They are taken verbatim rather than checked against `git remote`:
       git accepts a URL in these keys too, and a name that resolves to nothing
       produces git's own error, which is clearer than a second-guess of it.
    2. `origin`, if a remote by that name exists. Above the single-remote rung
       for the fork layout, where `origin` is the fork and the *other* remote
       is the canonical, plausibly public one — the push that most needs not to
       be guessed at. The two rungs can never actually disagree: with one
       remote named `origin` they give the same answer, and with one remote not
       named `origin` this rung does not fire. Ordering them is about which
       sentence the receipt prints, and about refusing to be more surprising
       than `git push` on the commonest multi-remote layout.
    3. The only remote, whatever it is called. `git clone -o gitlab`, a repo
       with a single `upstream`: there is one answer and picking it is not a
       guess.
    4. Otherwise nothing. Two or more remotes, none named `origin`, nothing
       configured — no correct answer exists, so none is invented.

    The rung deliberately *not* here is falling back to `origin` when no remote
    is called that. That was the whole of #656: the assumption was standing in
    for the fallback, so there was nothing left to fall back to.
    """
    for key in (k.format(b=branch) for k in _PUSH_REMOTE_KEYS):
        value, why = _config_value(key)
        if why:
            return "", "", why
        if value:
            return value, f"configured in {key}", ""
    names, why = _remote_names()
    if why:
        return "", "", why
    if not names:
        return "", "", "this repository has no remote configured"
    if "origin" in names:
        return "origin", "the remote named origin", ""
    if len(names) == 1:
        return names[0], "the only remote in this repository", ""
    return "", "", (f"this repository has {len(names)} remotes and none of "
                    f"them is named origin: {', '.join(names)}")


def _refuse_unresolved_remote(branch: str, why: str) -> int:
    """No upstream and no determinable remote — say so, and push nothing.

    The alternative is to pick one, and picking wrong on a *first* push does
    not fail — it succeeds. It creates a branch on a remote nobody named,
    plausibly a public one, and `-u` then aims every later push at it. That is
    the one outcome here worse than an error message, so this is a decline in
    the sense of docs/validators.md §"Declining instead of guessing": a third
    state, not a failure being hidden behind.

    A refusal the caller cannot act on is half a fix, so the reason names the
    candidates and the message names both ways out — the one-off push, and the
    config that stops this branch asking again.
    """
    print(f"ERROR: cannot determine which remote to push {branch} to — {why}")
    print("Nothing was pushed. Name the remote once, either way:")
    print("  git push -u <remote> HEAD")
    print(f"  git config branch.{branch}.remote <remote>   "
          "# then re-run git-push")
    _result(f"NOT PUSHED - no push attempted (cannot determine the push "
            f"remote for {branch}: {why})")
    return 1


def _refuse_mismatched_upstream(branch: str, remote_name: str,
                                remote_ref: str) -> int:
    """`@{upstream}` resolves, but to a different branch — decline (#787).

    This is not "no upstream": the lookup answered, cleanly, with
    `{remote_name}/{remote_ref}`. The common trigger is
    `git worktree add -b <branch> <path> <remote>/<ref>` — the normal way an
    agent starts a fresh branch. `branch.autoSetupMerge` (on by default)
    tracks the *start point*, not the new branch, so a branch that has never
    been pushed anywhere still carries a real, resolvable `@{upstream}`.

    Pushing bare here (no explicit refspec) hands the target to git's own
    `push.default`, which refuses outright — a branch name mismatch is
    exactly the case `push.default=simple` (the modern default) will not
    guess through. That refusal used to be rendered as `PUSH REJECTED` with
    `-> {remote_ref}`: a verb implying the remote acted (nothing was ever
    sent) and a target nobody asked for (`push.default` chose it, not the
    caller). Detecting the precondition ahead of the push — decidable
    without ever invoking `git push` — replaces both with an accurate
    decline.

    Two targets are both plausible reads of that config: push `branch` under
    its own name (the overwhelmingly common intent — this is what "first
    push" means for a feature branch), or push onto `remote_ref` on purpose
    (a deliberately different tracked name, set by hand). Guessing the first
    one silently would retarget a tracking config the caller may have set up
    on purpose; guessing the second would create a branch nobody named. Same
    shape as `_refuse_unresolved_remote`: name both ways out, in the
    docs/validators.md §"Declining instead of guessing" sense — a third
    state, not a failure hidden behind a wrong one.

    Both ways out are now flags on this op rather than raw `git push` lines
    (#879). That is not a softening of the refusal — the caller still has to
    say which intent they meant, which is the whole point of declining — it
    just makes the answer sayable in the vocabulary the rest of the workflow
    uses, instead of in the one command the project's hook forbids.
    """
    print(f"# git-push on {branch}")
    print(f"Upstream: {remote_name}/{remote_ref} — a different branch, "
          f"not {branch} itself")
    print(f"ERROR: {branch}'s upstream is {remote_name}/{remote_ref}. A "
          "bare push here is ambiguous — this is the exact state that "
          "makes git itself refuse with 'the upstream branch of your "
          "current branch does not match the name of your current "
          "branch', not a remote rejection.")
    print("Nothing was pushed. Name the target once:")
    print(f"  {_st_hint('git-push:set-upstream')}"
          f"   # push {branch} under its own name, tracking "
          f"{remote_name}/{branch} (the usual first push)")
    print(f"  {_st_hint('git-push:to-upstream')}"
          f"    # push onto {remote_name}/{remote_ref} on purpose, if that "
          f"is the real target")
    _result(f"NOT PUSHED - no push attempted ({branch}'s upstream is "
            f"{remote_name}/{remote_ref}, a different branch — ambiguous "
            f"target, nothing pushed)")
    return 1


def _refuse_conflicting_targets(branch: str, remote_name: str,
                                remote_ref: str) -> int:
    """`:set-upstream` and `:to-upstream` together — two refs, one push (#879).

    Resolving this by precedence would be worse than refusing. The two flags
    name *different remote refs*: `{remote_name}/{branch}` and
    `{remote_name}/{remote_ref}`. Silently honouring one would send commits
    somewhere the caller also explicitly asked them not to go, and on the
    `to-upstream` side that somewhere is routinely a shared base branch. A
    contradiction the op cannot honour is refused, exactly as an unknown flag
    is (#647) — nothing has moved yet, so the cost of stopping is a retype.
    """
    print(f"# git-push on {branch}")
    print("ERROR: :set-upstream and :to-upstream name different targets — "
          f"{remote_name}/{branch} and {remote_name}/{remote_ref}. Asking "
          "for both is not a push this op can order by precedence.")
    print("Nothing was pushed. Pick one:")
    print(f"  {_st_hint('git-push:set-upstream')}"
          f"   # push {branch} under its own name")
    print(f"  {_st_hint('git-push:to-upstream')}"
          f"    # push onto {remote_name}/{remote_ref}")
    _result(f"NOT PUSHED - no push attempted (:set-upstream and :to-upstream "
            f"name different targets: {remote_name}/{branch} vs "
            f"{remote_name}/{remote_ref})")
    return 2


# Summaries git uses for "your ref diverged from the remote's" — the one
# rejection a rebase actually recovers. Matched against the porcelain status
# summary for our own ref, never against free text (#641).
_NFF_SUMMARIES = ("non-fast-forward", "fetch first",
                  "tip of your current branch is behind")


def _ref_line(push_stdout: str, ref: str) -> tuple[str, str]:
    """(flag, summary) from git's own per-ref status line for `ref`.

    ('', '') means git never reported a line for that ref — an absence of
    information, never a "no". Every reader of the porcelain channel goes
    through here so they all share one grammar and one #641 discipline.
    """
    want = ref.rsplit("/", 1)[-1]
    for line in push_stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        if parts[1].split(":", 1)[-1].rsplit("/", 1)[-1] != want:
            continue
        return parts[0], parts[2].strip()
    return "", ""


def _push_outcome(push_stdout: str, ref: str) -> tuple[str, str, str]:
    """What git says this push did to `ref`: `(kind, old_sha, why)`.

    `kind` is `created` | `updated` | `forced` | `uptodate` | `unknown`, read
    off git's porcelain per-ref summary — the machine-readable channel #641
    established, not free text and not our local remote-tracking ref. The four
    summaries cannot be confused with each other: `[new branch]`,
    `<old>..<new>` (two dots), `<old>...<new> (forced update)` (three dots,
    behind a `+` flag), `[up to date]`.

    `unknown` carries `why` and is returned whenever git reported no line for
    this ref at all, or a summary this grammar does not cover. It exists so
    that callers of this function decline instead of guessing (#661); every
    other state here is a claim git itself made.
    """
    flag, summary = _ref_line(push_stdout, ref)
    if not flag:
        return "unknown", "", "git reported no per-ref status line for it"
    if "[new branch]" in summary:
        return "created", "", ""
    if flag == "=" or "[up to date]" in summary:
        return "uptodate", "", ""
    if flag == "+":
        if "..." in summary:
            return "forced", summary.split("...", 1)[0].strip(), ""
        return "unknown", "", ("git reported a forced update of it without the "
                               f"SHA it overwrote (`{summary}`)")
    if ".." in summary:
        return "updated", summary.split("..", 1)[0].strip(), ""
    return "unknown", "", f"git's per-ref summary for it was `{summary}`"


def _forced_update_old_sha(push_stdout: str, ref: str) -> str:
    """The SHA git says it overwrote, when it reports a forced update of `ref`.

    A thin reading of `_push_outcome`: only the `forced` kind carries a SHA
    this caller may act on. A forced update whose summary cannot be parsed is
    `unknown` there and empty here — returning the raw summary would hand
    `git log` a garbage revision and convert an unreadable answer into a
    *failed* check, the same wrong state by a longer route.

    It exists because the op's usual source for the pre-push SHA, `@{upstream}`,
    needs `branch.<name>.remote`/`.merge`, while `--force-with-lease` leases
    against the remote-tracking *ref*. Remove only the former — `git branch
    --unset-upstream`, a worktree checked out without tracking — and the push
    still overwrites the remote while the op has nothing to compare against
    (#655).
    """
    kind, old, _ = _push_outcome(push_stdout, ref)
    return old if kind == "forced" else ""


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
    flag, summary = _ref_line(push_stdout, ref)
    return summary if flag == "!" else ""


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

    Recorded, not just printed: "every return path" was a convention held up by
    each new return path remembering it, and a raised exception is not a return
    path at all (#675). main()'s catch-all needs to know whether the verdict
    already went out, so that a crash below this line adds nothing and a crash
    above it is still answered.
    """
    _RUN["verdict"] = True
    print(f"[result] {verdict}")


def _push_verdict(moved: bool, branch: str, remote: str, ref: str,
                  tracking_sha: str, ncommits: str,
                  force_note: str = "") -> None:
    """Verdict for a push git reported as successful.

    The sha is read back off the real remote (ls-remote), not just the local
    remote-tracking ref, so the caller does not have to fetch to trust it —
    that fetch is precisely the round-trip this op exists to remove. When the
    remote does not answer we say `unverified` and fall back to the tracking
    sha, labelled: a sha we did not read is never printed as if we had.

    Three states on each half of that comparison, not two (#675). Both `live`
    and `head` are reads that can fail, and the note used to be decided by
    `head and live == head` — a conjunction in which a `rev-parse HEAD` that
    never answered is indistinguishable from a remote that disagrees with local
    work. So a failed local read printed `verified, but remote != local HEAD`:
    a divergence claim, made out of an absence, on the one line #623 exists to
    make callers read. It now names which read went missing, and the reason the
    remote could not be verified is the real one rather than a fixed string.
    """
    live, live_why = _live_remote_sha(remote, ref)
    head, head_why = _local_head()
    target = f"{remote}/{ref}"
    if live:
        sha = live[:7]
        if not head:
            note = ("verified against the remote; the comparison with local "
                    f"HEAD could not be made — {head_why}")
        elif live == head:
            note = "verified"
        else:
            note = "verified, but remote != local HEAD"
    else:
        sha = tracking_sha or "unknown"
        note = f"unverified - {live_why or 'remote did not answer ls-remote'}"
    if moved:
        extra = f", {ncommits} commit(s)" if ncommits else ""
        _result(f"PUSHED  {branch} -> {target} @ {sha}  ({note}{extra})"
                f"{force_note}")
    else:
        _result(f"NOT PUSHED - already up to date  {branch} -> {target} "
                f"@ {sha}  ({note}){force_note}")


def _mr_lookup(branch: str) -> MrLookup:
    """The branch→MR/PR lookup, as one seam the tests can stand in for."""
    return query_open_mr_result(branch)


def _open_mr_line(mr: Optional[dict]) -> str:
    """One-line MR/PR summary for the post-push receipt, or empty.

    Still takes the request rather than the lookup: this line renders the MR
    and nothing else. The "did not answer" state is `_mr_unknown_line`'s, so
    an empty string here keeps meaning "no line to draw" at both call sites.
    """
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


def _mr_unknown_line(lookup: MrLookup) -> str:
    """The disclosure for a branch→MR/PR lookup that did not answer (#948).

    Empty on the healthy path — a lookup that answered prints exactly what it
    printed before, byte for byte, because a line that appears on every call
    stops being read on the call that needed it.

    When it is not empty it is a statement of ignorance, not a refusal. This
    runs *after* the push: a tracker that could not be reached is not a reason
    to withhold work that is already on the remote, and blocking here would
    trade a quiet wrong answer for a loud wrong one. Same shape and same
    wording as the stale-base and uncommitted-changes disclosures above.
    """
    if lookup.answered:
        return ""
    return (f"⚠ MR/PR LOOKUP DID NOT RUN — {lookup.reason}" + chr(10) +
            "  Whether this branch has an open MR/PR is UNKNOWN — this receipt "
            "is not saying there is none, and the mergeability and stale-base "
            "checks below are missing for the same reason. Settle it: "
            + _st_hint("git-status"))


def _watch_target(mr: Optional[dict]) -> Optional[tuple[str, str]]:
    """(watch-source, id) for the open MR/PR, or None."""
    if not mr or mr.get("iid") in (None, "?"):
        return None
    source = "gitlab-mr" if mr["source"] == "gitlab" else "github-pr"
    return source, str(mr["iid"])


def _repo_root() -> str:
    """Directory holding the `supertool` wrapper and `supertool.py`."""
    return install_dir()


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


_WATCH_START_BUDGET = 20.0


def _spawn_watch(source: str, iid: str) -> tuple[Optional[bool], str]:
    """Start a background watch poller. `(True|False|None, how-or-why-not)`.

    Three states, not two (#1010; docs/validators.md "Declining instead of
    guessing"):

      `(True,  how)`  the `watch` op ran, exited 0, and claimed the watcher
      `(False, why)`  nothing is watching, and this is the reason
      `(None,  why)`  the spawn was made and its outcome is not known

    This used to be fire-and-forget: `Popen` with both streams on `DEVNULL`,
    and `True` returned the instant the call did not raise. But `watch` reports
    its own refusals — an unknown source, a pid-file slot it could not claim,
    and on Windows an `os.fork` that does not exist — by exiting non-zero and
    naming them on stdout, and both went into `DEVNULL` while this receipt
    printed "Watching →". An absence manufactured by discarding the answer is
    not evidence that a watcher exists.

    Waiting is affordable because the process being waited on is not the
    poller. `watch` claims the slot, double-forks a detached grandchild whose
    stdio it has already silenced, and exits — so this is one process startup,
    the pipe cannot be held open by the surviving grandchild, and the budget
    bounds the case where that stops being true. The timed-out child is left
    alone rather than killed: it may already have detached a working poller,
    and killing it to make the answer tidy would destroy the thing the caller
    asked for.
    """
    argv, how = _watch_argv(source, iid)
    if not argv:
        return False, how
    try:
        proc = subprocess.Popen(argv, cwd=_repo_root(),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"{how} ({exc})"
    try:
        out, _ = proc.communicate(timeout=_WATCH_START_BUDGET)
    except subprocess.TimeoutExpired:
        return None, (f"{how} had not finished after {_WATCH_START_BUDGET:g}s "
                      f"and was left running — whether it started a watcher "
                      f"is UNKNOWN")
    if proc.returncode != 0:
        said = next((ln.strip() for ln in (out or "").splitlines() if ln.strip()),
                    "and said nothing")
        return False, f"{how} exited {proc.returncode}: {said}"
    return True, how


def _uncommitted_leftovers() -> tuple[Optional[list[str]], str]:
    """Working-tree changes NOT in this push — the 'forgot to commit X' catch.

    Three states, not two (#662; docs/validators.md "Declining instead of
    guessing"): `([], "")` the check ran and the tree is clean, `([...], "")`
    it ran and found these, `(None, why)` it could not run.

    The return code used to be ignored entirely. A `git status --porcelain`
    that exited non-zero — a broken `status.*` config, local or inherited, an
    index another process was holding — yields empty stdout, which became an
    empty list, which in this receipt renders as silence, which means "nothing
    was left behind". So the warning that exists to catch work you forgot to
    commit went quiet on precisely the run where git was not answering and you
    were least sure what state you were in.

    The timeout and OSError are caught here for the same reason they are a
    third state and not a traceback: this runs *after* the push has landed, and
    a stack trace at this point costs the caller the whole receipt of a push
    that succeeded (#399/#640).
    """
    r, why = _checked_git(["status", "--porcelain"], "git status --porcelain")
    if r is None:
        return None, why
    return [ln for ln in r.stdout.splitlines() if ln.strip()], ""


def _discarded_by_force(old_remote_sha: str) -> tuple[Optional[list[str]], str]:
    """Commits that were on the old remote tip but are now off the branch.

    Three states, not two (#655; docs/validators.md "Declining instead of
    guessing"): `([], "")` the check ran and found nothing, `([...], "")` it ran
    and found these, `(None, why)` it could not run.

    All three used to be `[]`. Two of them are absences of information and the
    third is a clean bill of health, so a `git log` that failed rendered
    byte-for-byte like a force-push that discarded nothing — on the one
    operation in this op that destroys work irrecoverably from the remote's
    point of view, and the one check here whose subject is commits that are
    very often somebody else's. The failure direction was the harmful one: the
    receipt reassured exactly where it should have warned.
    """
    if not old_remote_sha:
        return None, "no pre-push SHA recorded for the remote branch"
    r, why = _checked_git(
        ["log", "--format=%h %an: %s", old_remote_sha, "--not", "HEAD"],
        f"git log {old_remote_sha} --not HEAD")
    if r is None:
        return None, why
    return [ln for ln in r.stdout.splitlines() if ln.strip()], ""


def _report_discard_unknown(target: str, why: str, look: str) -> str:
    """The loud third state: the force-push landed, its cost is unmeasured.

    How loud is a judgment call, and this is the one it settled on. It does not
    block, abort or undo the push — the caller asked to force and that decision
    stands — and it does not default to a frightening message when the check
    simply had nothing to find, which is the silent path. What it refuses to do
    is let "could not check" render as "checked, clean" on a destructive
    operation. The named command is the point: the caller can settle it
    themselves in one step rather than walking away on a receipt that never
    looked.
    """
    print(f"⚠ DISCARD CHECK DID NOT RUN — {why}")
    print(f"  {target} was force-updated. Whether that destroyed commits that "
          "were on the remote — possibly someone else's — is UNKNOWN here: "
          "the check could not run, and --force-with-lease does not answer it "
          "either (a current lease still discards commits you never saw).")
    print(f"  Look before you walk away:  {look}")
    return (f" - DISCARD CHECK DID NOT RUN: whether this force-push destroyed "
            f"commits on {target} is UNKNOWN")


def _force_aftermath(old_remote_sha: str, push_stdout: str,
                     remote: str, ref: str) -> str:
    """What the force-push cost the remote. Returns the verdict suffix.

    Reached only from a `:force-with-lease` push git reported as successful, so
    an ordinary push never sees a word of this — a warning that fires on every
    push is one nobody reads, and this one has to be read.

    Silence from here is a positive claim in both of its cases, and both are
    decided from evidence rather than assumed: either the check ran and found
    nothing, or git's own per-ref line says this push did not force-update the
    remote at all (a fast-forward, a new branch), so nothing could have been
    discarded.
    """
    target = f"{remote}/{ref}"
    old = old_remote_sha or _forced_update_old_sha(push_stdout, ref)
    if not old:
        flag, _ = _ref_line(push_stdout, ref)
        if flag and flag != "+":
            return ""
        why = (f"git reported a forced update of {target} without the SHA it "
               "overwrote" if flag else
               f"no pre-push SHA for {target} (no upstream configured) and git "
               "reported no per-ref status line for it")
        return _report_discard_unknown(target, why, f"git reflog show {target}")

    commits, why = _discarded_by_force(old)
    if commits is None:
        return _report_discard_unknown(target, why,
                                       f"git log {old} --not HEAD")
    if not commits:
        return ""
    print(f"Force discarded {len(commits)} remote commit(s) — now off the branch:")
    for line in commits[:_INCOMING_CAP]:
        print(f"  {line}")
    if len(commits) > _INCOMING_CAP:
        print(f"  … +{len(commits) - _INCOMING_CAP} more")
    return (f" - FORCE-DISCARDED {len(commits)} remote commit(s) "
            f"(recover: git reflog show {target})")


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

    That contract is why the call could not simply be wrapped and returned from
    (#675). This function is silent on its *good* path, so a guard that
    swallowed a timeout would render "could not check" exactly like "checked,
    your base is fresh" — the loud bug traded for the quiet one it was already
    fixed for once.
    """
    ref = f"{remote}/{target}"
    cmd = f"git rev-list --count HEAD..{ref}"
    # Not routed through _checked_git: here the two failures mean different
    # things to the caller and get different words. A non-zero exit is git
    # answering — that ref is not in this clone, which `git fetch` fixes and
    # which #642 named "skipped". A call that never completed is answering
    # nothing, and takes #674's "DID NOT RUN" vocabulary, because fetching is
    # not the lever and the caller needs to know a check went missing rather
    # than that a ref is absent.
    try:
        cnt = _git(["rev-list", "--count", f"HEAD..{ref}"],
                   timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        print(f"⚠ STALE-BASE CHECK DID NOT RUN — `{cmd}` did not complete ({exc})")
        print(f"  How far behind {ref} you are is UNKNOWN — this receipt is "
              f"not saying your base is fresh. Settle it: {cmd}")
        return
    if cnt.returncode == TIMEOUT_RC:
        print(f"⚠ STALE-BASE CHECK DID NOT RUN — `{cmd}` did not complete "
              f"({cnt.stderr.strip()})")
        print(f"  How far behind {ref} you are is UNKNOWN — this receipt is "
              f"not saying your base is fresh. Settle it: {cmd}")
        return
    if cnt.returncode != 0 or not cnt.stdout.strip().isdigit():
        print(f"⚠ stale-base check skipped — {ref} does not resolve locally, "
              f"so how far behind the target you are is UNKNOWN "
              f"(enable it: git fetch {remote} {target})")
        return
    behind = int(cnt.stdout.strip())
    if behind:
        print(f"⚠ {behind} commit(s) behind {ref} — "
              "consider rebasing (stale base under review)")


def _watch_advisory(lookup: MrLookup, flags: set[str]) -> None:
    """The watch line — which state we are in, never silence (#642/#647/#1010).

    `:watch` used to be unreachable (dropped by the flag parser) and, once
    reached, could fail to spawn in a worktree with the OSError swallowed. So
    a requested watcher that does not exist is now named as such, together
    with the command that does work.

    #948's state is the lookup's: "There is no open MR/PR for this branch yet —
    open one" is a claim about the world, and it used to be printed out of a
    `None` that also meant "the lookup timed out". A caller who asked to watch
    a pipeline was told the request they had just pushed to does not exist.

    #1010's is the spawn's: an outcome that is unknown gets its own line rather
    than borrowing the failure one. "The watcher could not be started" is a
    claim, and made about a watcher that may well be running it sends the
    caller to start a second — which `watch` then refuses as a duplicate,
    leaving them with two contradicting messages and no way to tell which was
    true.
    """
    if not lookup.answered and "watch" in flags:
        print(f"⚠ :watch requested, but whether this branch has an open MR/PR "
              f"is UNKNOWN — {lookup.reason}. Nothing is being watched, and "
              "this is not saying there is nothing to watch.")
        print("Once you know the number: "
              + _st_hint("watch:gitlab-mr:<iid>")
              + " (or watch:github-pr:<number>)")
        return
    wt = _watch_target(lookup.mr)
    if "watch" not in flags:
        if wt:
            print("Watch pipeline: " + _st_hint(f"watch:{wt[0]}:{wt[1]}"))
        return
    if not wt:
        print("⚠ :watch requested, but there is no open MR/PR for this branch "
              "yet — nothing to watch. Open one, then: "
              + _st_hint("watch:gitlab-mr:<iid>"))
        return
    source, iid = wt
    started, how = _spawn_watch(source, iid)
    if started is True:
        print("Watching → notifies on pipeline finish/fail "
              "(unwatch: " + _st_hint(f"unwatch:{source}:{iid}") + ")")
        return
    if started is None:
        print(f"⚠ :watch requested — whether a watcher is running is UNKNOWN "
              f"— {how}")
        print("  This receipt is not saying one exists, and not saying one "
              "does not. Settle it: " + _st_hint("watches"))
        print("If none is listed: " + _st_hint(f"watch:{source}:{iid}"))
        return
    print(f"⚠ :watch requested but the watcher could not be started — {how}")
    # #1012: the remedy for a watcher that failed to start used to be
    # `./supertool`, i.e. a command that fails in the one environment where
    # this line is printed. #642 fixed the spawn and not the printed remedy.
    print("Run it yourself: " + _st_hint(f"watch:{source}:{iid}"))


def _post_push_advisories(lookup: MrLookup, flags: set[str],
                          remote: str) -> None:
    """Surface the next-decision signals: mergeability, stale base, leftovers, watch.

    `remote` is the branch's upstream remote — required, not defaulted, so a
    new call site cannot quietly reintroduce the hardcoded `origin` of #642.

    Takes the whole `MrLookup`, not the request, because two of these signals
    are *skipped* when there is no request and the skip was indistinguishable
    from a pass: no target branch means no mergeability warning and no
    stale-base check, and a lookup that timed out silently dropped both (#948).
    """
    unknown = _mr_unknown_line(lookup)
    if unknown:
        # Printed here rather than at each call site: it is the disclosure for
        # the two checks immediately below, and a call site that forgot it
        # would silently skip both.
        print(unknown)
    mr = lookup.mr
    if mr and mr.get("merge_status") in ("cannot_be_merged", "conflict", "broken_status"):
        print(f"⚠ MR conflicts with {mr.get('target', 'target')} — "
              "won't merge until rebased/resolved")

    target = mr.get("target") if mr else ""
    if target and target != "?":
        _stale_base_advisory(target, remote)

    # Count, not a listing (#623): on a tree full of generated junk the list
    # crowded the push verdict off the end of the output. The "did I forget to
    # stage something?" signal is the count; the files stay one op away.
    leftovers, why = _uncommitted_leftovers()
    if leftovers is None:
        print(f"⚠ UNCOMMITTED-CHANGES CHECK DID NOT RUN — {why}")
        print("  Whether this push left work behind in the working tree is "
              "UNKNOWN — this receipt is not saying the tree is clean. "
              "Settle it: " + _st_hint("git-status:full"))
    elif leftovers:
        print(f"⚠ {len(leftovers)} change(s) NOT in this push (uncommitted) — "
              "list them: " + _st_hint("git-status:full"))

    _watch_advisory(lookup, flags)


def _report_first_seen_remote(remote_after: str, push_stdout: str, ref: str,
                              target: str) -> tuple[bool, str]:
    """The remote resolves now and the op had no pre-push SHA — what happened?

    Returns `(moved, verdict_note)`.

    This used to be one `elif` printing `(branch created)`, and it inferred the
    creation from `remote_before` being empty. `remote_before` is empty
    whenever `@{upstream}` does not resolve, which is a fact about local config
    and says nothing about the remote: `--force-with-lease` leases against the
    remote-tracking *ref*, so unsetting only `branch.<name>.merge` leaves the
    lease passing and the push overwriting an existing branch while the op has
    no SHA to compare against (#655). The receipt then announced a creation —
    on the one operation here that destroys work irrecoverably, the least
    alarming possible story available, and the opposite of what happened. Read
    it and you stop looking (#661).

    So the answer is git's own per-ref line, which distinguishes all four
    outcomes unambiguously, and when git said nothing this function declines
    instead of picking the reassuring branch (three states, not two;
    docs/validators.md "Declining instead of guessing"). The doubt rides to the
    `[result]` line because a receipt is read from the bottom (#623).
    """
    kind, old, why = _push_outcome(push_stdout, ref)
    if kind == "created":
        print(f"Remote now at {remote_after} (branch created)")
        return True, ""
    if kind == "uptodate":
        print(f"Remote at {remote_after} — already up to date, ref unchanged")
        return False, ""
    if kind == "forced":
        print(f"Remote {old} → {remote_after} (force-updated — the branch "
              "already existed on the remote and was overwritten)")
        return True, ""
    if kind == "updated":
        print(f"Remote {old} → {remote_after} (the branch already existed on "
              "the remote)")
        return True, ""
    print(f"⚠ Remote now at {remote_after} — what it pointed at BEFORE this "
          "push is UNKNOWN")
    print(f"  No pre-push SHA was recorded (@{{upstream}} did not resolve) and "
          f"{why}. That is not evidence of a branch creation: the branch may "
          "have already existed and been overwritten.")
    print(f"  Settle it: git reflog show {target}")
    return True, (" - PRE-PUSH REMOTE STATE UNKNOWN: whether this push created "
                  f"{target} or overwrote it is not established")


def _ahead_behind_line() -> None:
    """`vs upstream: …` — three states, not two (#662).

    The guard here used to be `if ab.returncode == 0:` with no `else`. An
    in-sync push legitimately prints nothing extra elsewhere in this receipt,
    and a caller reads a missing block the same way either time, so a check
    that *could not run* was indistinguishable from one that ran and found
    agreement. Silence in this receipt is a positive claim; it has to be
    earned, which means the failing path says so out loud and names the command
    that settles it.

    Deliberately no `[result]` suffix: the verdict already carries the verified
    remote SHA against local HEAD, which is the load-bearing half of this
    block, and a suffix for every soft check dilutes the one channel that has
    to stay readable (#623/#655).
    """
    cmd = "git rev-list --left-right --count HEAD...@{upstream}"
    ab, why = _checked_git(
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], cmd)
    if ab is None:
        print(f"⚠ vs upstream: UNKNOWN — {why}")
        return
    parts = ab.stdout.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        print(f"⚠ vs upstream: UNKNOWN — `{cmd}` answered "
              f"`{ab.stdout.strip()}`, which is not an ahead/behind pair")
        return
    ahead, behind = int(parts[0]), int(parts[1])
    if ahead or behind:
        print(f"vs upstream: ahead {ahead}, behind {behind}")
    else:
        print("vs upstream: in sync")


def _pushed_commit_count(before: str, after: str) -> tuple[str, str]:
    """How many commits the remote moved by. `(count, why-not)`.

    #674's summary lists this call as fixed. It was `_ahead_behind_line`'s
    `rev-list` that was fixed — a different call, extracted out of this same
    function in the same commit — and this one stayed unguarded, which is the
    reason #675 says to read each function rather than a window around it.

    Its old fallback was the string `"?"`, printed as `(? commit(s))` and then
    carried into the verdict. That is a decline nobody reads as one, and it was
    no decline at all when the call raised instead of exiting non-zero.
    """
    cmd = f"git rev-list --count {before}..{after}"
    r, why = _checked_git(["rev-list", "--count", f"{before}..{after}"], cmd)
    if r is None:
        return "", why
    n = r.stdout.strip()
    if not n.isdigit():
        return "", f"`{cmd}` answered `{n}`, which is not a count"
    return n, ""


def _success_receipt(branch: str, remote_before: str, upstream: str,
                     flags: set[str], fallback_remote: str,
                     force_note: str = "", push_stdout: str = "") -> None:
    """Shared 'what landed' tail — remote diff, ahead/behind, MR line, advisories.

    `force_note` rides all the way to the `[result]` line on purpose. The force
    aftermath is printed several lines above it, and a receipt is read from the
    bottom (#623) — a caller reading only the verdict would otherwise walk away
    from a destructive push believing it was clean (#655).

    `push_stdout` is git's `--porcelain` output for the push that just landed.
    It is the only evidence here about what the remote ref was *before*, on the
    path where `@{upstream}` never resolved — the local absence of a pre-push
    SHA is not evidence of anything (#661).

    `fallback_remote` is the remote the caller resolved for a branch with no
    upstream (#656) — required rather than defaulted, so this receipt cannot
    quietly name `origin` in a repo that has no such remote.
    """
    if not upstream:
        upstream, up_why = _upstream_ref()
        if up_why:
            print(f"⚠ UPSTREAM LOOKUP DID NOT RUN — {up_why}")
            print(f"  The remote named below falls back to "
                  f"{fallback_remote}/{branch} and may not be this branch's "
                  "real upstream (#642), so the stale-base check and the "
                  "verified SHA below may be about the wrong ref.")
    remote_name, remote_ref = _split_upstream(upstream, branch,
                                              fallback_remote)
    remote_after, after_why = _remote_sha(upstream)
    moved, ncommits, unknown_note = True, "", ""
    if remote_before and remote_after and remote_before != remote_after:
        ncommits, cnt_why = _pushed_commit_count(remote_before, remote_after)
        if cnt_why:
            # Body-only, and the verdict simply omits the count: how many
            # commits moved is a decoration on a line that already carries both
            # SHAs, and a verdict that grows a clause per soft check stops
            # being readable (#623/#674).
            print(f"Remote {remote_before} → {remote_after} — how many "
                  f"commit(s) that is: UNKNOWN ({cnt_why})")
        else:
            print(f"Remote {remote_before} → {remote_after} ({ncommits} commit(s))")
    elif not remote_before and remote_after:
        moved, unknown_note = _report_first_seen_remote(
            remote_after, push_stdout, remote_ref,
            f"{remote_name}/{remote_ref}")
    elif remote_before and remote_after and remote_before == remote_after:
        moved = False
        print("Already up to date — nothing to push")
    else:
        # Push succeeded but the remote-tracking SHA isn't locally resolvable
        # (shallow clone, odd remote layout). Don't claim up-to-date.
        remote_after = ""
        print("Pushed — remote ref not locally resolvable for a before/after "
              "diff" + (f" ({after_why})" if after_why else ""))

    _ahead_behind_line()

    lookup = _mr_lookup(branch)
    mr_line = _open_mr_line(lookup.mr)
    if mr_line:
        print(mr_line)
    _post_push_advisories(lookup, flags, remote_name)
    _push_verdict(moved, branch, remote_name, remote_ref, remote_after,
                  ncommits, force_note + unknown_note)


def _report_hook_pushed(head_before: str, head_after: str,
                        remote: str, ref: str, remote_sha: str,
                        branch: str, flags: set[str]) -> None:
    """Receipt for the 'non-zero exit but the ref actually moved' case."""
    _note_landed(branch, remote, ref)
    if head_after != head_before:
        print("Status: PUSHED (pre-push hook amended HEAD) ✓")
        print(f"Local HEAD rewritten {head_before[:7]} → {head_after[:7]}")
    else:
        print("Status: pushed ✓ (pre-push hook exited non-zero; "
              "remote already matches HEAD)")
    print(f"Remote {remote}/{ref} now at {remote_sha[:7]}")
    # This IS a landed push — surface the same next-decision signals as the
    # normal success path (mergeability, stale base, leftovers, watch).
    lookup = _mr_lookup(branch)
    mr_line = _open_mr_line(lookup.mr)
    if mr_line:
        print(mr_line)
    _post_push_advisories(lookup, flags, remote)
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
    head_after, _head_why = _local_head()
    live, live_why = _live_remote_sha(remote, ref)
    print(f"Push exceeded its {_PUSH_TIMEOUT}s budget — asking the remote what landed…")
    if live and head_after and live == head_after:
        _note_landed(branch, remote, ref)
        print("Status: pushed ✓ (push timed out locally; remote ref matches HEAD)")
        if head_after != head_before:
            print(f"Local HEAD rewritten {head_before[:7]} → {head_after[:7]}")
        print(f"Remote {remote}/{ref} now at {live[:7]}")
        print(f"Push outlasted its {_PUSH_TIMEOUT}s budget (slow pre-push hook "
              "or transfer), so the receipt above is only what fit in the "
              "time. The push landed — re-run `git-push` for the full receipt "
              "(it will report already up to date).")
        print(f"That budget is _PUSH_TIMEOUT in presets/git/push.py, NOT "
              "ops.git-push.timeout: the op-level cap in .supertool.json "
              "bounds the whole process and raising it alone will not move "
              "this one. The two are not interchangeable — this budget has to "
              "stay strictly under that cap, or a push killed by the outer "
              "one can verify nothing (#399).")
        lookup = _mr_lookup(branch)
        mr_line = _open_mr_line(lookup.mr)
        if mr_line:
            print(mr_line)
        _post_push_advisories(lookup, flags, remote)
        _result(f"PUSHED  {branch} -> {remote}/{ref} @ {live[:7]}  "
                "(verified - push timed out locally, remote matches HEAD)")
        return 0
    print("Status: PUSH TIMED OUT ✗ — remote ref does NOT match local HEAD")
    print(f"local HEAD {head_after[:7] or 'unknown'} | "
          f"remote {remote}/{ref} at {live[:7] or 'unknown'}"
          + (f" ({live_why})" if not live and live_why else ""))
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
        r = _git(["rev-parse", "--git-path", name], timeout=10)
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
        print("Inspect: " + _st_hint("git-conflicts"))
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
    # #818: `remote_name`/`remote_ref` come from `_split_upstream(@{upstream})`,
    # a remote-tracking ref name an attacker who controls the remote can choose.
    # `remote_ref` lands as a bare argv element in the fetch below, and a value
    # like `--upload-pack=<cmd>` executes on fetch. The #787 mismatch guard in
    # main() (remote_ref != branch) shields this path today, but that is a
    # semantic check, not a security one — the refusal belongs at the sink.
    refuse = reject_fetch_option(remote_name, remote_ref)
    if refuse:
        print(f"Status: PUSH REJECTED ✗ — {refuse}")
        _result(f"NOT PUSHED - REJECTED  {branch} -> {target} - {refuse}")
        return 1
    print(f"Remote moved ahead — fetching to rebase onto {target}…")
    fetched = _git(["fetch", remote_name, remote_ref],
                   timeout=_RECOVER_TIMEOUT)
    if fetched.returncode == TIMEOUT_RC:
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

    rebase = _git(["rebase", rebase_target], timeout=_RECOVER_TIMEOUT)
    if rebase.returncode == TIMEOUT_RC:
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
        print("Inspect: " + _st_hint("git-conflicts")
              + "  — every conflict block + abort hint")
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
    # --porcelain here too: the success receipt reads git's own per-ref line to
    # say what the remote ref was before this push, and on this path
    # `remote_before` is empty whenever `@{upstream}` never resolved (#661).
    push_args = ["push", "--porcelain"]
    if "no-verify" in flags:
        push_args.append("--no-verify")
    if not upstream:
        push_args += ["-u", remote_name, "HEAD"]
    elif remote_ref != branch:
        # `:to-upstream` again — the recovery re-push has to carry the same
        # explicit refspec, or a non-fast-forward turns a deliberate target
        # into a bare push git will refuse for a reason unrelated to the
        # rebase that just succeeded.
        push_args += [remote_name, f"HEAD:{remote_ref}"]
    result = _git(push_args, timeout=_PUSH_TIMEOUT)
    if result.returncode == TIMEOUT_RC:
        return _report_push_timeout(branch, _local_head()[0],
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
    _note_landed(branch, remote_name, remote_ref)
    _success_receipt(branch, remote_before, upstream, flags, remote_name,
                     push_stdout=result.stdout or "")
    return 0


def _crash_receipt(exc: BaseException) -> int:
    """Something unforeseen raised. Say what, and still answer the question.

    The one guard here that is not about a call site. Every other fix in #675
    makes a known check decline by name; this one makes the *next* check
    harmless, because a helper added to this receipt tomorrow is unguarded the
    moment it is written, and the caller of a push that already landed must not
    pay for that with a stack trace where the verdict should be.

    That is the argument for a structural guard over six more `try/except`
    blocks. Per-site guards are still the better fix *per site* — they keep the
    remaining checks running and name the one that went missing, which this
    cannot do — so both exist, with different jobs. What this adds is that the
    invariant stops depending on anyone remembering.

    Deliberately not a quiet catch. The traceback is printed in full, and on
    **stdout**: supertool discards a preset's stderr entirely when the preset
    exits zero, and a landed push exits zero, so a crash reported on stderr
    would be invisible through the op. Converting a loud bug into a silent one
    is the failure mode this issue exists to avoid — the verdict is appended to
    the crash, it does not replace it.

    The exit code follows the *push*, not the receipt. A landed push that could
    not be described is still a landed push, and returning non-zero would tell
    every caller treating that as "the push failed" exactly the wrong thing.
    """
    print("\n--- receipt crash ---")
    traceback.print_exc(file=sys.stdout)
    detail = f"{exc.__class__.__name__}: {exc}"
    if _RUN["phase"] == "landed":
        print("The push itself LANDED — the remote moved. What broke is the "
              "receipt describing it, so any check below the crash point never "
              "ran and nothing here is claiming otherwise.")
        print("Re-run `git-push` for the full receipt (it will report already "
              "up to date).")
        if not _RUN["verdict"]:
            _result(f"PUSHED  {_RUN['branch']} -> {_RUN['target']} @ unknown  "
                    f"(RECEIPT INCOMPLETE - git-push crashed after the push "
                    f"landed: {detail})")
        return 0
    if _RUN["phase"] == "attempted":
        print("The push was started and git-push crashed before it could "
              "establish what reached the remote.")
        if not _RUN["verdict"]:
            # Two arguments, not one path: `git ls-remote origin/feature`
            # asks git for a remote named `origin/feature`, which does not
            # exist. A verdict that hands the caller a command that cannot run
            # is #663's defect wearing this issue's clothes — the whole point
            # of naming a lever is that pulling it settles the question.
            settle = (f"git ls-remote {_RUN['remote']} {_RUN['ref']}"
                      if _RUN["remote"] and _RUN["ref"] else "git ls-remote")
            _result(f"NOT PUSHED - UNVERIFIED  {_RUN['branch'] or 'branch'} -> "
                    f"{_RUN['target'] or 'remote'} - git-push crashed mid-push "
                    f"({detail}); whether anything landed is UNKNOWN - settle "
                    f"it: {settle}")
        return 1
    if not _RUN["verdict"]:
        _result("NOT PUSHED - no push attempted (git-push crashed before "
                f"pushing: {detail})")
    return 1


def main() -> int:
    # Entry point: run the op, and guarantee it ends on a verdict either way.
    #
    # use_utf8_stdout() before the try, not inside it: _crash_receipt prints ⚠
    # and ✓ too, and a cp1252 console kills the process on the first one
    # (#308) — the guard that exists to keep a receipt alive must not be the
    # thing that loses it. It also has to stay the literal first statement of
    # main(), which is what tests/test_encoding_seam.py checks, so this is a
    # comment rather than a docstring.
    use_utf8_stdout()
    _RUN.update({"phase": "not-attempted", "branch": "", "remote": "",
                 "ref": "", "target": "", "verdict": False})
    try:
        return _push_op()
    except Exception as exc:  # noqa: BLE001 — deliberate; see _crash_receipt
        return _crash_receipt(exc)


def _push_op() -> int:
    """The op itself. Reached only through main(), which owns the guard."""
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

    upstream, upstream_why = _upstream_ref()
    # #879: the inherited-wrong upstream. `git worktree add -b <new> <base>`
    # leaves the new branch tracking <base>, and `:set-upstream` is the
    # caller saying "push me under my own name and retarget the tracking".
    # Dropping `upstream` here — rather than special-casing every reader of
    # it below — is what makes the rest of this function take the ordinary
    # no-upstream path, including `-u` on the rebase-recovery re-push.
    # `retarget_from` survives so the receipt can say what was retargeted;
    # rendering this as "Upstream: none" would hide the very thing the flag
    # authorised.
    retarget_from = ""
    if upstream:
        _tracked_ref = _split_upstream(upstream, branch, "")[1]
        if _tracked_ref != branch:
            if {"set-upstream", "to-upstream"} <= flags:
                return _refuse_conflicting_targets(
                    branch, _split_upstream(upstream, branch, "")[0],
                    _tracked_ref)
            if "set-upstream" in flags:
                retarget_from, upstream = upstream, ""
    has_upstream = bool(upstream)
    remote_before, before_why = (_remote_sha(upstream) if has_upstream
                                 else ("", ""))
    # Resolved before anything is printed, because a repo this op cannot pick a
    # remote for must not get as far as a push line (#656). `has_upstream` is
    # false both when the branch has no upstream and when the lookup did not
    # run, and both need a resolved remote — the second one especially, since
    # that is the path #675 caught naming a remote nobody confirmed.
    push_remote, chosen_how = "", ""
    if not has_upstream:
        push_remote, chosen_how, cannot_tell = _resolve_push_remote(branch)
        if not push_remote:
            return _refuse_unresolved_remote(branch, cannot_tell)
    remote_name, remote_ref = _split_upstream(upstream, branch, push_remote)
    if has_upstream and remote_ref != branch and "to-upstream" not in flags:
        # @{upstream} resolved, but not to this branch's own name — a bare
        # push here is git's own ambiguity, not ours to guess through (#787).
        # `:to-upstream` is the caller resolving it: they mean this ref, so
        # the push below names it explicitly rather than leaving the target
        # to `push.default` (which is what git refuses in the first place).
        return _refuse_mismatched_upstream(branch, remote_name, remote_ref)
    head_before, _head_before_why = _local_head()

    print(f"# git-push on {branch}")
    # Which LOCAL repo these commits came from. `Upstream:` below says where
    # they are going; neither answered "from where" before #692, and push is
    # the op whose wrong answer is hardest to take back.
    print(f"Repo: {repo_label()}")
    if has_upstream:
        print(f"Upstream: {upstream}" + (f" @ {remote_before}" if remote_before else ""))
        if before_why:
            print(f"⚠ Pre-push remote SHA UNKNOWN — {before_why}")
        if remote_ref != branch:
            print(f"  :to-upstream — pushing onto {remote_name}/{remote_ref} "
                  f"on purpose, not onto {remote_name}/{branch}")
    elif retarget_from:
        print(f"Upstream: {retarget_from} — inherited, not this branch. "
              f":set-upstream retargets it to {remote_name}/{branch} "
              f"({chosen_how})")
    elif upstream_why:
        # Not the same as having no upstream, and the difference is load-bearing
        # on the next line: `-u <remote> HEAD` is about to be chosen from an
        # answer git never gave (#675).
        print(f"⚠ UPSTREAM LOOKUP DID NOT RUN — {upstream_why}")
        print("  Treating this as 'no upstream' and setting one on push, to "
              f"{remote_name} ({chosen_how}). If {branch} already tracks "
              "something else, this push will retarget it.")
    else:
        print(f"Upstream: none — setting on first push to "
              f"{remote_name}/{branch} ({chosen_how})")
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
        push_args += ["-u", remote_name, "HEAD"]
    elif remote_ref != branch:
        # `:to-upstream`. Explicit refspec, never a bare push: a bare push
        # here is the exact input `push.default=simple` refuses, and the
        # rendering of that refusal is what #787 was filed about.
        push_args += [remote_name, f"HEAD:{remote_ref}"]
    _RUN.update({"phase": "attempted", "branch": branch,
                 "remote": remote_name, "ref": remote_ref,
                 "target": f"{remote_name}/{remote_ref}"})
    result = _git(push_args, timeout=_PUSH_TIMEOUT)
    if result.returncode == TIMEOUT_RC:
        return _report_push_timeout(branch, head_before,
                                    remote_name, remote_ref, flags)

    combined = (result.stdout or "") + "\n" + (result.stderr or "")

    if result.returncode != 0:
        # The exit code may lie: a pre-push hook that amends HEAD and pushes
        # the fixed commit itself exits non-zero on purpose. Ground truth is
        # the remote ref — if it already matches our (possibly rewritten)
        # HEAD, the content is on the remote. Report honestly.
        head_after, _ = _local_head()
        live, _ = _live_remote_sha(remote_name, remote_ref)
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
    _note_landed(branch, remote_name, remote_ref)
    force_note = ""
    if "force-with-lease" in flags:
        # No `and remote_before` guard any more: that guard was itself a silent
        # third state. `remote_before` is empty whenever `@{upstream}` does not
        # resolve, which does not stop `--force-with-lease` from overwriting
        # the remote — so the one push that most needed the check was the one
        # that skipped it without a word (#655).
        force_note = _force_aftermath(remote_before, result.stdout or "",
                                      remote_name, remote_ref)
    _success_receipt(branch, remote_before, upstream, flags, remote_name,
                     force_note, push_stdout=result.stdout or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
