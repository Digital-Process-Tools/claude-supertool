#!/usr/bin/env python3
"""git-status validator adapter. Emits SCHEMA.md JSON.

Reports the working-tree delta as metrics and never triggers rollback, so a
verdict from this adapter is `ok: true` or an absence.

The absence is the part that keeps being missing. With git absent this answered
`ok: true` with a zeroed `metrics` block whose `state` was `clean` — a positive
claim about a file it had not looked at, and the one shape a reader cannot tell
from a real answer. #1202 routed that arm to `skipped`, escalating to a loud
error when this validator is named in `$SUPERTOOL_REQUIRE_VALIDATORS`.

**The timeout arm was left behind, and it is the louder one** (#1882), because
git being present is the normal case. Each git call ran under a hard-coded
`timeout=5` and returned `""` on `TimeoutExpired`; `_parse_state("")` is
`"clean"` and `_parse_numstat("")` is `(0, 0)`, so a repository too slow to
answer produced the same fabricated clean measurement, on every edit, forever.
It now declines with `code: "adapter"` — the word `validators/SCHEMA.md`
reserves for a timeout, and the one the core routes to `NOT CHECKED`, never
caches and never rolls back over. Not `skipped()`: an adapter cannot set
`no_verdict`, and `_validator_no_verdict` returns `None` the instant `skipped`
is a key, so a fault spelled that way exits 0 — quieter than the bug.

**One budget for the adapter, not one per call.** Four sequential 5s budgets
could spend 20s while `.supertool.json` gave the whole adapter 5s, so on any
real stall the core's budget won and SIGKILLed this process mid-git. The budget
is now a single deadline shared by every call, and it sits below the configured
validator timeout so the adapter can state its own decline.

**A stalled git is asked to stop before it is killed.** `subprocess.run(
timeout=)` calls `Popen.kill()` — SIGKILL on POSIX, no grace — so git never
runs its own cleanup and the `.git/index.lock` it holds is stranded, failing
every later write in that repository until somebody deletes the file by hand.
Removing that lock ourselves is deliberately NOT done: this process cannot tell
a lock it orphaned from one a concurrent `git commit` legitimately holds, and
deleting the second corrupts that commit. Letting git clean up after itself
fixes the damage without the inference.

Usage: git-status.py <file>

Env vars:
  GIT_BIN               git binary (default: git)
  SUPERTOOL_GIT_TIMEOUT whole-adapter budget in seconds, >= 1
                        (default: GIT_TIMEOUT_DEFAULT below). Same knob every
                        other git call in this repo reads.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from refusal import absent, guard_main

TOOL = "git-status"
INSTALL_HINT = ("git not found on PATH — the working-tree delta for this file "
                "was NOT measured (set $GIT_BIN if git lives elsewhere)")

#: Seconds for the WHOLE adapter — all four git calls together — when the
#: environment does not say otherwise. 5 was the old per-call literal and the
#: reporter measured `git status` at >15s on a large cold clone, so it was not
#: a budget that a slow repository occasionally blew; it was one that never
#: held. This sits above `presets/git/_git_common._GIT_TIMEOUT_DEFAULT` (10)
#: because that one covers a single call and this one covers four, and below
#: the `.supertool.json` validator timeout so the decline below is reachable at
#: all. A repository genuinely slower than this raises the env var; the point
#: of the default is that blowing it is now rare AND loud, rather than common
#: and silent.
GIT_TIMEOUT_DEFAULT = 15

#: How long a stalled git is given to remove its own lock after SIGTERM before
#: SIGKILL. Short on purpose: this is a courtesy to a process that is already
#: over budget, not a second budget. Real git handles SIGTERM and unlinks its
#: lockfile well inside this.
TERM_GRACE_S = 2

TIMEOUT_ENV = "SUPERTOOL_GIT_TIMEOUT"


class _NoAnswer(Exception):
    """A git call that produced no answer, and which of the two ways it did.

    Two causes reach the same decline — the call outlived the deadline, or the
    spawn never happened — and they must not reach it wearing the same words.
    A message that says `timed out` about a git binary that could not be
    executed sends the reader to raise a budget that was never the problem,
    which is the quiet-wrong-answer half of the bug this file is fixing rather
    than a smaller version of it.
    """

    def __init__(self, argv: "tuple[str, ...]", detail: str) -> None:
        super().__init__(" ".join(argv))
        self.argv = argv
        self.detail = detail


def _budget() -> int:
    """The adapter's whole-run budget, in seconds.

    Deliberately NOT `presets/_env.env_int`, which is the right helper for a
    preset and fatal here: it announces an unusable value on **stdout**, and
    the core parses this adapter's stdout as JSON. One notice line would turn a
    working validator into `no_verdict`. So an unusable value falls back
    silently, and the only place that could report it is a channel this process
    does not own.
    """
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None:
        return GIT_TIMEOUT_DEFAULT
    try:
        value = int(raw.strip())
    except (AttributeError, TypeError, ValueError):
        return GIT_TIMEOUT_DEFAULT
    return value if value >= 1 else GIT_TIMEOUT_DEFAULT


def _stop(proc: "subprocess.Popen") -> None:
    """Ask the child to stop, then insist (#1882).

    The ask is what stops `.git/index.lock` being stranded: git traps SIGTERM
    and unlinks the lock it is holding, which is the only actor entitled to
    decide that lock is stale. The insist is what stops this being a promise to
    wait — a child that ignores SIGTERM is killed anyway, one grace period
    later.

    **Windows: reasoned, not observed.** `Popen.terminate()` and `Popen.kill()`
    are both `TerminateProcess` there, so the grace period costs one extra
    bounded wait and buys nothing. That is a property of the platform rather
    than of this adapter, so it is not branched on: a `sys.platform` test here
    would make the Windows path a different, less exercised shape to save two
    seconds on a path that has already blown its budget.
    """
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.communicate(timeout=TERM_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    try:
        proc.kill()
        proc.communicate(timeout=TERM_GRACE_S)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass


def _adapter_error(file: str, msg: str, dur_ms: int) -> dict:
    """`code: "adapter"` — the channel SCHEMA.md reserves for "no verdict"."""
    return {"tool": TOOL, "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": msg}],
            "duration_ms": dur_ms}


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def _parse_numstat(output: str) -> tuple[int, int]:
    """Parse `git diff --numstat` single-file output → (added, removed)."""
    line = output.strip()
    if not line:
        return 0, 0
    parts = line.split("\t")
    if len(parts) < 2:
        return 0, 0
    try:
        added = int(parts[0]) if parts[0] != "-" else 0
        removed = int(parts[1]) if parts[1] != "-" else 0
        return added, removed
    except ValueError:
        return 0, 0


def _parse_state(porcelain: str) -> str:
    """Parse `git status --porcelain` single-file output → state string."""
    line = porcelain.rstrip("\n")
    if not line:
        return "clean"
    if len(line) < 2:
        return "unknown"
    xy = line[:2]
    x = xy[0]  # index (staged)
    y = xy[1]  # worktree (unstaged)
    if xy == "??":
        return "untracked"
    if x != " " and x != "?" and y == " ":
        return "staged"
    if y != " " and y != "?":
        return "modified"
    if x != " " and x != "?":
        return "staged"
    return "clean"


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "git-status", "file": "", "ok": True, "count": 0,
            "errors": [], "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "lines_staged_added": 0, "lines_staged_removed": 0,
                        "state": "clean"},
        })
        return

    file = sys.argv[1]
    git_bin = os.environ.get("GIT_BIN", "git")

    if not shutil.which(git_bin):
        # Not `state: "clean"`. A zeroed metrics block is a measurement, and no
        # measurement was taken.
        emit(absent(TOOL, file, INSTALL_HINT, 0))
        return

    start = time.monotonic()
    budget = _budget()
    deadline = start + budget
    file_dir = str(pathlib.Path(file).resolve().parent)

    def ms() -> int:
        return int((time.monotonic() - start) * 1000)

    def run(*args: str) -> str:
        """One git call, inside the deadline the whole adapter shares.

        A call that does not answer raises `_Stalled` rather than returning
        `""`. That return value was the bug (#1882): every parser downstream
        reads an empty string as a real, clean measurement, so the shape of "we
        never found out" was byte-identical to "we looked and it is fine".

        A non-zero exit is NOT a stall and keeps returning stdout, because
        `rev-parse --is-inside-work-tree` outside a repository is an ordinary
        answer this adapter is built to read.
        """
        timed_out = ("timed out after " + str(budget) + "s (the whole-adapter "
                     "budget; raise $" + TIMEOUT_ENV + " if this repository is "
                     "genuinely this slow)")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _NoAnswer(args, timed_out)
        try:
            proc = subprocess.Popen(
                [git_bin, *args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=file_dir, text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            # `shutil.which` said git was there; the spawn says otherwise — it
            # was removed under us, or is not executable by this user. Not a
            # measurement either way, and NOT a timeout: naming it one sends
            # the reader to raise a budget that was never the problem.
            raise _NoAnswer(args, "could not be run: "
                            + exc.__class__.__name__ + " - "
                            + (str(exc) or "no reason given")) from exc
        try:
            out, _err = proc.communicate(timeout=remaining)
            return out
        except subprocess.TimeoutExpired:
            _stop(proc)
            raise _NoAnswer(args, timed_out) from None

    try:
        # Check we're inside a git repo (fast — runs rev-parse)
        rev = run("rev-parse", "--is-inside-work-tree")
        if rev.strip() != "true":
            emit({
                "tool": "git-status", "file": file, "ok": True, "count": 0,
                "errors": [], "duration_ms": ms(),
                "metrics": {"lines_added": 0, "lines_removed": 0,
                            "lines_staged_added": 0, "lines_staged_removed": 0,
                            "state": "clean"},
            })
            return

        worktree_out = run("diff", "--numstat", "--", file)
        staged_out = run("diff", "--cached", "--numstat", "--", file)
        porcelain_out = run("status", "--porcelain", "--", file)
    except _NoAnswer as stall:
        emit(_adapter_error(file, (
            "`git " + " ".join(stall.argv) + "` " + stall.detail + ", so the "
            "working-tree delta for this file was NOT measured - this is a "
            "git-status failure, not a finding about the file."), ms()))
        return

    dur = ms()

    added, removed = _parse_numstat(worktree_out)
    staged_added, staged_removed = _parse_numstat(staged_out)
    state = _parse_state(porcelain_out)

    emit({
        "tool": "git-status",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {
            "lines_added": added,
            "lines_removed": removed,
            "lines_staged_added": staged_added,
            "lines_staged_removed": staged_removed,
            "state": state,
        },
    })


if __name__ == "__main__":
    guard_main(TOOL, main)
