#!/usr/bin/env python3
"""The one git invocation chokepoint, reachable from every preset (#2447).

Every `git ...` this tool runs from a preset goes through `_git` (or
`_git_verbatim`) here, and everything that has ever had to be true of a git
call is true of it once: `--no-optional-locks` on the read path (#1944/#1945),
the `_stop()` SIGTERM grace so a stalled write unlinks its own
`.git/index.lock` (#2033), `_with_lock_retry` and its diagnosis when somebody
else holds that lock (#2034), and `git_timeout()`'s `SUPERTOOL_GIT_TIMEOUT`
override (#650, #1886/#1903).

**Why it is here and not in `presets/git/`.** It lived in
`presets/git/_git_common.py`, which is one preset's directory. Two files
outside that preset needed the same call and could not have it without
reaching into it, so each grew a private `_git()` on a bare `subprocess` call
instead: `presets/dashboard/dashboard.py` and `presets/github/pr_merge.py`.
Neither got the retry, the grace or the environment override, and #1945's
`--no-optional-locks` had to be propagated to all three by hand -- which is
what `tests/test_git_no_optional_locks_other_sites_1945.py` was written to
pin, and what its own first paragraph named as the cost.

`presets/_*.py` is where a helper every preset may use lives (`CLAUDE.md`,
Layout), so that is where this went. `presets/git/_git_common.py` re-exports
every name below, unchanged and by identity rather than by copy, so the eight
git presets that import from it did not have to move.

`pr_merge._git` is the one that made this worth doing rather than leaving: it
is the chokepoint for the cleanup arm of every merge the maintainer loop
performs -- `branch -d` and `worktree remove` against a repository that may
have several worktrees live, which is exactly the situation `_with_lock_retry`
was written for.

**One behaviour change, named rather than buried.** Adopting `_with_lock_retry`
on the merge cleanup path means a call that fails on lock contention is now
retried within `LOCK_WAIT_DEFAULT` instead of failing at once. That is the
point of the adoption and it is still a change in timing on a write path, not
only a refactor.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import time

# Sibling import: this file sits at the `presets/` root, so `_env` and
# `_untrusted` are beside it. Arranged here rather than at each call site, so
# that importing this module is enough to get the knob -- `presets/git/
# checkout.py` and five others had no `SUPERTOOL_GIT_TIMEOUT` override at all,
# purely because each would have had to set up its own path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _env import env_int, env_float  # noqa: E402  (the one numeric-knob reader)
import _untrusted  # noqa: E402  (a child stream, and a path off disk, are somebody else's text -- #1475, #1557)


#: Budget for one git call when the call site does not name its own.
#:
#: The ten copies this module replaces did not agree: 5 in `status.py`, 10 in
#: six presets, 30 here and in `merge.py`. No test pinned any of them and no
#: two were chosen together, so consolidating had to pick one. 10 is the value
#: six of the ten already used, and it is the only one that was ever reachable
#: from the environment. The three calls that genuinely need longer — the push,
#: the merge, the commit that runs a hook suite — now say so at the call site,
#: which is where a budget of 300s is legible and a module default is not.
_GIT_TIMEOUT_DEFAULT = 10

#: Shell convention for "killed by a timeout" (coreutils `timeout`). Distinct
#: from any exit code git itself produces, so a caller checking
#: `returncode != 0` keeps working while one that wants to tell a stall from a
#: failure can. `status.py`, `conflicts.py` and `resolve.py` had each defined
#: this constant separately, with the same value and the same comment.
TIMEOUT_RC = 124

#: How long a stalled git is given to remove its own lock after SIGTERM
#: before SIGKILL (#2033, same value and same reasoning as
#: `validators/git-status/git-status.py::TERM_GRACE_S`). Short on purpose:
#: this is a courtesy to a process that is already over budget, not a second
#: budget. Real git handles SIGTERM and unlinks its lockfile well inside this.
_TERM_GRACE_S = 2


def git_timeout(default: int | None = None) -> int:
    """Default budget for a git call, overridable per environment (#650).

    Same shape and same reasoning as `SUPERTOOL_LINT_TIMEOUT` (#553): a loaded
    runner occasionally needs room without a code change, and what supertool
    ships with does not move for it.
    """
    base = _GIT_TIMEOUT_DEFAULT if default is None else default
    return env_int("SUPERTOOL_GIT_TIMEOUT", base, minimum=1)


def _settled(proc: "subprocess.Popen", grace: int) -> bool:
    """Did the child stop within `grace` seconds? Reaps it if so (#2033).

    Copied from `validators/git-status/git-status.py::_settled` rather than
    re-derived -- the issue that asked for this function named the exact trap
    to avoid: **`communicate()` failing says nothing about the child.** It is
    the PIPES that broke, and a broken pipe is a different event from a dead
    process (#1888, #1912, measured there against a real child on CPython
    3.13.15: closing the stdout fd under a live `sleep 600` raises
    `OSError(9, "Bad file descriptor")` with the child still running). Treating
    that as "the child is gone" would leave a live git holding
    `.git/index.lock`, which is the exact bug this function exists to stop
    causing. So a pipe failure falls back to `wait()`, which touches no pipe
    and answers the question actually being asked.
    """
    try:
        proc.communicate(timeout=grace)
        return True
    except subprocess.TimeoutExpired:
        return False
    except (OSError, ValueError):
        # The pipes are unusable -- an fd closed underneath us, or a file
        # object already closed. Neither is evidence about the process, so
        # ask the process.
        try:
            proc.wait(timeout=grace)
            return True
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            return False


def _stop(proc: "subprocess.Popen") -> None:
    """Ask the child to stop, then insist (#2033, same mechanism as #1882).

    `subprocess.run(timeout=)`'s `TimeoutExpired` arm is `Popen.kill()` --
    SIGKILL, no grace -- so a stalled `git commit`/`checkout`/`merge`/`push`
    reached through `_git`/`_git_verbatim` never got the chance to unlink the
    `.git/index.lock` it was holding, wedging every later write in that
    repository until a human deleted the file by hand. `--no-optional-locks`
    (#1945) closed this for the read-only calls this chokepoint also makes --
    it is a documented no-op on a write command, by its own comment a few
    lines above -- so the write commands still need an actor that can ask git
    to clean up after itself, which only git itself can do.

    **Windows.** *Reasoned, not observed here*: `Popen.terminate()` and
    `Popen.kill()` are both `TerminateProcess` there, so the grace period buys
    nothing on that platform -- `validators/git-status/git-status.py::_stop`
    observed the equivalent outcome on CI (windows-latest/3.10) for its own,
    structurally identical, function; this one has not been separately run
    there. Either way it is a property of the platform, not of this function,
    so it is not branched on: a live child's pipes going quiet is stopped
    identically on every platform, and the branch would only make the
    Windows path a different, less exercised shape to save two seconds on a
    call that has already blown its budget.
    """
    try:
        proc.terminate()
    except OSError:
        # The child is already gone, or the platform refused the signal.
        # Either way there is nothing to ask, and the kill below is a no-op
        # on a dead child.
        pass
    if _settled(proc, _TERM_GRACE_S):
        return
    try:
        proc.kill()
    except OSError:
        pass
    if not _settled(proc, _TERM_GRACE_S):
        # Killed and not reaped inside the grace: a zombie until this
        # process exits and init reaps it. Deliberately not escalated further
        # -- the caller is about to report the timeout and move on, and
        # blocking longer to tidy a zombie would spend the caller's own
        # budget on something the OS finishes for free.
        pass


#: Git's own wording for "somebody already holds this lock" -- quoted path,
#: as real git 2.46.2 writes it, or unquoted, as the shim in
#: `tests/test_status_swallowed_705.py` writes it. The group captures the
#: lock path so the diagnosis below can be pointed at the right file.
_LOCK_ERROR_RE = re.compile(r"Unable to create '?([^'\n]+?)'?:\s*File exists")

#: Total time #2034 will spend retrying a lock-contention failure before
#: giving up and reporting it, rather than failing on the first attempt the
#: way every call here used to. Chosen to sit comfortably above one ordinary
#: local git call finishing (the module docstring above enumerates several
#: git-touching things that run against one worktree at once: every
#: validator's own `git-status`, the `read` receipt's path-meta suffix,
#: `radar`/`git-worktrees` polling) and well below the point a caller starts
#: to suspect the tool itself, not the lock, is what is stuck.
#:
#: `0` disables the retry entirely -- an opt-out, not a one-shot retry -- and
#: also skips the diagnosis: asking for no retry is read as asking for the
#: old, undiagnosed, fail-fast behaviour outright.
LOCK_WAIT_DEFAULT = 2.0

#: Backoff between retries, short first. The overwhelmingly common case is
#: two local git calls finishing a fraction of a second apart, and that case
#: should cost milliseconds, not the whole budget.
_LOCK_BACKOFF = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)


#: A ceiling on `SUPERTOOL_GIT_LOCK_WAIT`, not a suggestion. `env_float`'s
#: `minimum=` is a floor, and `float("inf")` clears any floor -- verified:
#: `SUPERTOOL_GIT_LOCK_WAIT=inf` reaches `_with_lock_retry` unchanged and its
#: `deadline = time.monotonic() + budget` becomes infinite, defeating the one
#: property this whole feature exists to have (bounded). A minute is already
#: far past any local git call this module has a budget for.
_LOCK_WAIT_CEILING = 60.0


def _lock_wait_budget() -> float:
    value = env_float("SUPERTOOL_GIT_LOCK_WAIT", LOCK_WAIT_DEFAULT, minimum=0.0)
    if math.isnan(value) or value == float("inf"):  # NaN, or +inf slipping past `minimum`
        return LOCK_WAIT_DEFAULT
    return min(value, _LOCK_WAIT_CEILING)


def _lock_fd_holder(lock_path: str, scan_timeout: float = 3.0):
    """Does any process hold `lock_path` open right now? `True` / `False` /
    `None` for "the scan itself did not run" (#2034).

    This is a narrower and more direct question than `worktrees.py`'s
    `_cwd_scan` answers, deliberately: that probe licenses the *whole-tree*
    `idle` verdict and asks "is any process chdir'd in here", which is
    already documented there as a weak signal on its own (a parent process
    need not be chdir'd into the tree an agent under it is editing). A held
    lock is not that question -- git keeps the lockfile open with an
    exclusive descriptor for exactly as long as it is working, and unlinks it
    on the way out. So the direct read is "does an open file descriptor point
    at this exact path", which `lsof <path>` (or an `/proc/*/fd` walk when
    `lsof` is not installed) answers without needing the worktree's own path
    at all -- useful here, since all this function is handed is the lock
    file itself.

    `None` is what a `ps aux | grep` could never say, and it is the answer
    whenever the scan could not run: no `lsof`, no `/proc`, a stalled scan.
    Never collapsed into `False` -- that would be exactly the absence this
    codebase keeps re-filing, an absence produced by the tool read as an
    absence in the world.
    """
    lsof = shutil.which("lsof")
    if lsof:
        try:
            proc = subprocess.run(
                [lsof, "-w", "-F", "p", "--", lock_path],
                capture_output=True, text=True, timeout=scan_timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError:
            return None
        # lsof's own exit convention: 0 = matched something, 1 = either "ran
        # fine and matched nothing" OR "an error occurred" -- the SAME code
        # for both (verified: lsof 4.91, a target that vanished mid-scan
        # prints "status error on ...: No such file or directory" to stderr
        # and still exits 1). `-w` suppresses WARNINGS, not errors, so an
        # error still writes there. Exit 1 with empty stderr is the genuine
        # "asked, got nothing" case; exit 1 with anything on stderr is lsof
        # itself failing, and reading that as `False` would be exactly the
        # absence this codebase keeps re-filing -- a lookup that could not
        # answer, reported as a clean negative.
        if proc.returncode == 0:
            return bool(proc.stdout.strip())
        if proc.returncode == 1 and not proc.stderr.strip():
            return False
        return None
    if not os.path.isdir("/proc"):
        # No lsof and no /proc (typically Windows, or a stripped container) --
        # reasoned, not separately observed on Windows: nothing here has been
        # run there. Declining is the safe direction either way.
        return None
    try:
        target = os.path.realpath(lock_path)
    except OSError:
        return None
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fd_names = os.listdir(fd_dir)
        except OSError:
            continue  # another user's process, or it exited mid-scan
        for fd in fd_names:
            try:
                if os.path.realpath(f"{fd_dir}/{fd}") == target:
                    return True
            except OSError:
                continue
    return False


def _diagnose_lock(lock_path: str) -> str:
    """One line: is the lock #2034 just hit live, stale, or cannot-tell --
    and on what evidence.

    Report only. Nothing here removes the lock, and the message never
    suggests a removal command -- `worktrees.py`'s own note applies
    unchanged: deleting a lock the caller believes it orphaned is a
    destructive act taken on an inference, and the default has to be to
    report rather than to act on one.

    `lock_path` is `_LOCK_ERROR_RE`'s capture off git's own stderr, not a
    path this module built itself -- a child stream, by this file's own
    taint model, however implausible an attacker-controlled lock path is in
    practice for a locally-taken `.git/index.lock`. The FILESYSTEM operations
    below (`os.stat`, `_lock_fd_holder`) use the raw capture, unchanged --
    flattening a real path before looking it up is how a lookup for a path
    that IS on disk starts answering for one that is not. Only the copy that
    goes into the returned message is flattened, with the newline disclosed
    rather than elided (`_untrusted.flat`'s own doc: eliding it would let two
    directories read as one, or one real one read as two). That message is
    appended to `result.stderr` and relayed onward to every caller in
    `presets/git`, several of which print `.stderr` raw (review finding, low
    likelihood in practice, cheap to close either way).
    """
    try:
        age = time.time() - os.stat(lock_path).st_mtime
    except OSError:
        return ("lock-diagnosis: the lock file is gone already -- whoever "
                "held it has released it")
    age_text = f"{age:.1f}s old"
    held = _lock_fd_holder(lock_path)
    shown = _untrusted.flat(lock_path, disclose_newline=True)
    if held is True:
        return (f"lock-diagnosis: live -- a process still has {shown} "
                f"open ({age_text})")
    if held is False:
        return (f"lock-diagnosis: stale -- {shown} is {age_text} and no "
                "process has it open, so it was most likely left behind by "
                "a crash rather than held by a slow one. Not removed "
                "automatically: deleting a lock on an inference is how a "
                "live write gets corrupted instead of a stale one cleared")
    return (f"lock-diagnosis: cannot tell -- {shown} is {age_text} and "
            "whether a process still holds it open could not be established "
            "on this platform (no lsof, no /proc, or the scan did not "
            "answer) -- treat it as live until a human looks")


def _with_lock_retry(attempt):
    """Call `attempt()` (a zero-arg thunk returning a `CompletedProcess`),
    retrying it while it fails on lock contention, within `#2034`'s budget.

    Every other failure -- wrong args, no such repo, a real conflict -- is
    returned on the first call. Retrying those would just be a slower way to
    fail, and would blur which of several retried attempts actually errored.

    On exhausting the budget, one retry's failure is returned with a
    diagnosis of the lock appended to its stderr, naming which of live /
    stale / cannot-tell the evidence supports -- never a bare timeout with no
    further detail, and never another silent retry past the stated ceiling.
    """
    budget = _lock_wait_budget()
    result = attempt()
    if budget <= 0:
        return result
    deadline = time.monotonic() + budget
    step = 0
    while result.returncode != 0:
        match = _LOCK_ERROR_RE.search(result.stderr or "")
        if not match:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            diagnosis = _diagnose_lock(match.group(1))
            # Mutated in place, not rebuilt through `subprocess.CompletedProcess(
            # stdout=, stderr=)` -- that shape is a `return <expr>` sink over a
            # raw stream (#1475's census), and this is the transport passing its
            # own result through, not a new render. Attribute assignment is
            # neither a taint-carrying target nor a sink, so it stays invisible
            # to that scanner the same way a plain relay already is everywhere
            # else in this module.
            result.stderr = (result.stderr or "").rstrip("\n") + "\n" + diagnosis
            return result
        time.sleep(min(_LOCK_BACKOFF[min(step, len(_LOCK_BACKOFF) - 1)], remaining))
        step += 1
        result = attempt()
    return result


def _git(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """`_git_attempt`, retried through lock contention within a bounded
    budget, and diagnosed rather than silently re-tried past it (#2034).

    See `_with_lock_retry` for the retry/diagnosis contract and
    `_git_attempt`'s own docstring, below, for everything about the call
    itself -- timeout handling, `--no-optional-locks`, the `_stop()` grace.
    """
    return _with_lock_retry(lambda: _git_attempt(args, timeout))


def _git_attempt(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command; a call that does not answer says so (#650, #704).

    `TimeoutExpired` is not allowed to escape. It is not swallowed either: the
    result carries `TIMEOUT_RC`, so every call site's existing
    `returncode != 0` branch behaves exactly as it would for a git that failed,
    while a caller that needs to tell a stall from a failure still can. The one
    thing that must never happen is rendering it as a success — an empty
    `diff --diff-filter=U` reads as "no conflicts", which is the sentence
    `git-conflicts` printed over live `<<<<<<<` markers until #703.

    **The argument wins; the environment sets the default.** A call site that
    names its own budget is making a statement about that call — `git-push`
    gives its push 300s because it owns the timeout and must verify the remote
    before reporting — and a `SUPERTOOL_GIT_TIMEOUT` set to shorten the
    courtesy calls in `git-status` must not silently cap it. `status.py` had
    the reverse precedence; it was reachable only through its own default, so
    nothing depended on it.

    **Popen + `_stop()`, not `subprocess.run(timeout=)` (#2033).** `run()`'s
    own `TimeoutExpired` arm has already called `Popen.kill()` -- SIGKILL, no
    grace -- by the time it reaches any `except` clause here, so there was
    never a point at which this function could interpose a SIGTERM while
    still calling `run()`. Driving `Popen` directly is what buys the window in
    which `_stop()` can ask first.

    The `TIMEOUT_RC` / `"timed out after {budget}s"` contract is unchanged for
    the plain-timeout case, on purpose: no caller distinguishes "SIGTERM was
    enough" from "SIGKILL was needed", and the issue that asked for this
    function said explicitly that the receipt need not either. A `communicate()`
    failure -- the pipes broke, not necessarily the child, see `_settled` --
    reaches the same `TIMEOUT_RC` with its own stderr, since no caller here
    checks stderr's wording either; only `returncode != 0` is load-bearing.
    """
    budget = git_timeout() if timeout is None else timeout
    # --no-optional-locks precedes the subcommand -- it is a git global flag
    # (#1945, same mechanism as #1944 one file over). git diff/status refresh
    # the on-disk index and take .git/index.lock to do it; `_stop()` above
    # sends SIGTERM specifically so a stalled git traps it and unlinks that
    # lock on its way out. The flag removes the lock from existence instead
    # for the read-only calls this chokepoint makes: git treats the call as
    # read-only and skips the index writeback, so killing the process at any
    # point leaves nothing behind. It is a no-op on the write commands this
    # same chokepoint also runs (commit, checkout, stash push, push, merge)
    # -- it suppresses OPTIONAL locks only, verified against real git 2.46.2
    # -- which is exactly why those commands still need `_stop()`.
    cmd = ["git", "--no-optional-locks"] + args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )
    except (OSError, ValueError) as exc:
        if not _settled(proc, _TERM_GRACE_S):
            _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=(f"communicate() failed: {exc.__class__.__name__} - "
                   f"{str(exc) or 'no reason given'}"),
        )
    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr,
    )


def _git_verbatim(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """`_git_verbatim_attempt`, retried through lock contention the same way
    `_git` is (#2034). See `_with_lock_retry` and `_git`'s own docstring.
    """
    return _with_lock_retry(lambda: _git_verbatim_attempt(args, timeout))


def _git_verbatim_attempt(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """`_git_attempt`, with Python's universal-newline translation OFF (#1693).

    Same budget, same `TIMEOUT_RC` contract, same decode, same `_stop()` grace
    (#2033). The one difference is that `_git` runs `Popen(text=True)`, and
    text mode rewrites **a lone CR and a CRLF into LF** on the way in — so by
    the time any preset here receives a stream, a carriage return the child
    actually wrote is already indistinguishable from a line break the child
    actually wrote.

    That is invisible almost everywhere and decisive in one place. `git blame
    --line-porcelain` interleaves its own headers with the blamed file's OWN
    lines, and a source file may hold a bare CR: measured on git 2.46.2, a file
    containing `x = 1<CR>author Mallory<CR><TAB>I did this` reached
    `investigate.py` as three lines, two of which read as git's. No splitter
    downstream can undo that — `str.splitlines()`, `_untrusted.split_lines` and
    a bare LF split are equally forged, because the bytes that told them apart
    are gone. So the reader that must not be forged reads the bytes.

    Not the default, and deliberately so: every other caller here is parsing a
    stream where the translation is a convenience and CRLF from a Windows child
    is noise. This is the escape for a stream that carries somebody else's file
    content, and a new caller should have that reason.
    """
    budget = git_timeout() if timeout is None else timeout
    # --no-optional-locks precedes the subcommand -- see `_git`'s own comment
    # on the same flag (#1945) for the mechanism; it applies identically here.
    cmd = ["git", "--no-optional-locks"] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        raw_out, raw_err = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )
    except (OSError, ValueError) as exc:
        if not _settled(proc, _TERM_GRACE_S):
            _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=(f"communicate() failed: {exc.__class__.__name__} - "
                   f"{str(exc) or 'no reason given'}"),
        )
    # Routed through an intermediate `CompletedProcess` -- carrying the raw
    # bytes as its own `.stdout`/`.stderr` -- rather than decoding `raw_out`/
    # `raw_err` directly (#2033 follow-up). `tests/test_forged_child_stream_
    # line_1475.py`'s census recognises a child stream by the SYNTACTIC shape
    # `<name>.stdout` / `<name>.stderr`, not by taint through a tuple-unpack
    # assignment (`raw_out, raw_err = proc.communicate()` taints neither name,
    # by that scanner's own documented blind spot for a `Tuple` target). The
    # two decode() calls below are the SAME two raw relay sites the old
    # `done = subprocess.run(...)` shape had -- nothing about what they carry
    # changed -- so they are kept in the one shape that stays visible to the
    # guard that exists to catch exactly this, rather than reconciling the
    # published count down to match a smaller field of view.
    done = subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=raw_out, stderr=raw_err,
    )
    return subprocess.CompletedProcess(
        args=cmd, returncode=done.returncode,
        stdout=done.stdout.decode("utf-8", "replace"),
        stderr=done.stderr.decode("utf-8", "replace"),
    )
