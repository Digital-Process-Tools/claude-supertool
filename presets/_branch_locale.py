"""Where is this branch checked out? — one answer for every op that asks (#850).

Five ops printed the same hand-built line under their `Branch:` field:

    You are on: master ⚠ MISMATCH — switch with: ./supertool 'git-checkout:fix/900'

`⚠ MISMATCH` was true only of the current directory, and it reads as a claim
about the repository. With `fix/900` held by a linked worktree one directory
over, a reader concludes the branch is checked out nowhere — the opposite of the
truth — and then runs a command `git` refuses outright:

    fatal: 'fix/900' is already used by worktree at '/…/st-wt/900'

`git-checkout` already knew this: it catches that stderr and answers
`Switch with: cd <path>`. The knowledge existed one op over and the five call
sites had not adopted it. This module is that adoption, in one place.

**Three states, not two.** `here` / `elsewhere` / `nowhere` — plus `unknown`
when the lookup did not answer. An unanswered `git worktree list` must never
render as `nowhere`: "checked out nowhere" is a positive claim, and inferring it
from a failed probe is the shape of defect this tracker keeps paying for.

**The warning is not deleted.** Removing it would make the symptom vanish and
would also drop a real signal for the ordinary single-worktree case, which is
still the common one. `MISMATCH` survives untouched where it is true.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _untrusted  # noqa: E402  (branch names and worktree paths are not our text — #851/#876)
import _refname  # noqa: E402  (the ordinary-refname rule this line prints into — #694/#924)

#: `git worktree list` is a local read of one file; a slow answer means
#: something is wrong with the repo, not that we should wait longer.
_TIMEOUT = 3


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def current_branch() -> str | None:
    """The cwd's branch, or None when detached / not a repo / git unavailable."""
    r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if r is None or r.returncode != 0:
        return None
    local = r.stdout.strip()
    if not local or local == "HEAD":
        return None
    return local


def holding_worktree(source: str) -> tuple[str, str]:
    """Which worktree holds `source`, as `(path, detail)`.

    - `("", "")`               — no worktree holds it (established).
    - `(path, "")`             — that worktree holds it.
    - `("", reason)`           — the question was not answered; `reason` says why.

    Only the `branch` field counts. A *detached* worktree parked on the same
    commit is a different fact, and folding it in here would recreate the
    ambiguity this module exists to remove.
    """
    r = _run(["git", "worktree", "list", "--porcelain"])
    if r is None:
        return "", "git worktree list could not be run"
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip().splitlines()
        return "", (why[0] if why else f"git worktree list exited {r.returncode}")

    path = ""
    for block in r.stdout.split("\n\n"):
        entry_path = ""
        entry_branch = ""
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                entry_path = value
            elif key == "branch":
                entry_branch = value.replace("refs/heads/", "", 1)
        if entry_branch and entry_branch == source and entry_path:
            path = entry_path
            break
    return path, ""


def check(source: str, actionable: bool = True) -> str:
    """The one-line `You are on: …` field, in whichever of the states holds.

    Empty string when there is no branch to compare against — not a git repo,
    a detached HEAD, or an empty `source`. That silence predates this module and
    is left alone: those callers print no `Branch:` field to hang a check under.

    `actionable=False` withholds the `git-checkout` imperative on the read-only
    sub-ops (#531) — a radar session must not be told to move HEAD. It is only
    ever the imperative that is withheld; the state is always stated, so a
    missing command can never be read as "you are on the right branch". The
    `cd` suggestion is exempt: it moves you, not HEAD, which is exactly what a
    session that must not move HEAD wants to hear.
    """
    if not source or source == "?":
        return ""
    raw_local = current_branch()
    if raw_local is None:
        return ""
    if raw_local == source:
        return f"You are on: {_untrusted.flat(raw_local)} ✓"

    # One line the reader takes as the tool's, built from two names the tool
    # did not write: the branch comes off the API, the path off the filesystem.
    # Both go through `flat` for the reason #851 gives — a newline in either
    # forges a line here as readily as it did in the check header.
    local = _untrusted.flat(raw_local)
    named = _untrusted.flat(source)

    path, why = holding_worktree(source)
    if why:
        return (f"You are on: {local} — whether {named} is checked out in "
                f"another worktree is UNKNOWN ({_untrusted.flat(why)}), so "
                f"no switch is suggested")
    if path:
        return (f"You are on: {local} — {named} is checked out in another "
                f"worktree: {_untrusted.flat(path)} (cd there; a checkout "
                f"here would be refused)")
    if not actionable:
        return (f"You are on: {local} ⚠ MISMATCH — this is {named}; "
                f"read-only op, HEAD left alone")
    if not _refname.ordinary(source):
        # #924: `source` is the head branch of a pull/merge request, named by
        # whoever opened it — a fork PR needs no permission here. Below, it is
        # interpolated between two single quotes in a line whose whole form is
        # the tool saying what to run next, and `flat()` above removes line
        # separators, not `'`. A partial sanitiser reads as a complete one.
        #
        # Not quoted, refused — the third state, same vocabulary as the UNKNOWN
        # branch above. Quoting is what `_refname.shell_ref` is for and it is
        # right where the command is the deliverable (`mr.py`'s conflict
        # recipe). Here the command is a convenience, and for a name outside
        # the set it would be wrong as well as unsafe: this suggestion is read
        # back through supertool's colon CLI, which splits `git-checkout:REF`
        # on `:`, so a ref holding one cannot be delivered by any quoting; and
        # `flat()` has already rewritten the name, so the quoted command would
        # faithfully name a ref that does not exist. A safe command that
        # silently does the wrong thing is the trade this repo does not make.
        #
        # The name is still stated, and so is the reason — a suggestion that
        # simply vanished would read as "you are on the right branch", which is
        # the #531 failure at this same line.
        return (f"You are on: {local} ⚠ MISMATCH — this is {named}, a name "
                f"outside the ordinary-refname set (letters, digits, "
                f"`. _ / -`, no leading `-`), so no switch command is "
                f"suggested — check it out yourself, deliberately")
    return f"You are on: {local} ⚠ MISMATCH — switch with: ./supertool 'git-checkout:{named}'"
