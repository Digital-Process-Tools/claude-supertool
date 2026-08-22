#!/usr/bin/env python3
"""Shared helpers for the git/* preset scripts.

Holds the bits that were drifting across commit.py / push.py:
  - _git            : thin subprocess wrapper
  - _first_error_line: pick the salient line out of git/hook output
  - query_open_mr_result : open MR/PR for a branch, or why that is unknown
  - use_utf8_stdout : stop the ✓/✗ glyphs crashing a cp1252 console

Each script formats the lookup's output its own way — the lookup itself
(glab → gh fallback) lives here once, and since #948 it reports which of
three things happened rather than returning the same `None` for "no MR"
and "the lookup did not happen".
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional

# Sibling import: `_env` lives one directory up. Arranged here rather than at
# each call site, so that importing this module is enough to get the knob —
# `presets/git/checkout.py` and five others had no `SUPERTOOL_GIT_TIMEOUT`
# override at all, purely because each would have had to set up its own path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _untrusted  # noqa: E402  (a child stream, and a path off disk, are somebody else's text — #1475, #1557)


def use_utf8_stdout() -> None:
    """Force UTF-8 on this process's stdout/stderr.

    Windows stdout defaults to cp1252, which cannot encode the ✓/✗/⚠ glyphs
    these scripts print — writing one kills the process with
    UnicodeEncodeError, so a commit that actually succeeded reports as a
    crash. supertool.py reconfigures its own streams, but each preset runs as
    a separate process and does not inherit that. diff.py carried this inline
    from issue #308; it lives here so the rest do not each need a copy.

    A stream without ``reconfigure`` (wrapped or replaced, as under pytest's
    capture) is left alone rather than treated as an error.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


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


def git_timeout(default: int | None = None) -> int:
    """Default budget for a git call, overridable per environment (#650).

    Same shape and same reasoning as `SUPERTOOL_LINT_TIMEOUT` (#553): a loaded
    runner occasionally needs room without a code change, and what supertool
    ships with does not move for it.
    """
    base = _GIT_TIMEOUT_DEFAULT if default is None else default
    return env_int("SUPERTOOL_GIT_TIMEOUT", base, minimum=1)


def _git(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
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
    """
    budget = git_timeout() if timeout is None else timeout
    cmd = ["git"] + args
    try:
        return subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=budget, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )


def _git_verbatim(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """`_git`, with Python's universal-newline translation OFF (#1693).

    Same budget, same `TIMEOUT_RC` contract, same decode. The one difference is
    that `_git` runs `subprocess.run(text=True)`, and text mode rewrites **a
    lone CR and a CRLF into LF** on the way in — so by the time any preset here
    receives a stream, a carriage return the child actually wrote is already
    indistinguishable from a line break the child actually wrote.

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
    cmd = ["git"] + args
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=budget)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )
    return subprocess.CompletedProcess(
        args=cmd, returncode=done.returncode,
        stdout=done.stdout.decode("utf-8", "replace"),
        stderr=done.stderr.decode("utf-8", "replace"),
    )


#: The sentence seven ops print when git says this is not a repository. Three
#: more print it too — `trail.py`, `investigate.py`, `checkout.py` — but reach
#: it by matching git's own stderr, so they are not callers of `probe_repo`.
NOT_A_REPO = "ERROR: not inside a git repository."


def probe_repo(git_fn=None, args: list[str] | None = None) -> tuple[bool | None, str]:
    """Is the cwd inside a git repository — established, not inferred (#1858).

    *git_fn* is the CALLER's `_git`, and passing it is not ceremony. Every
    preset here wraps or rebinds that name and some of them hang behaviour off
    it — `status.py`'s records each unanswered call in `_UNANSWERED` so the
    footer can name it, and the whole suite mocks git at that seam. A helper
    that reached for `_git_common._git` directly would silently escape both:
    the probe would go unrecorded in the very op whose footer exists to record
    it, and three existing tests that mock `mod._git` would keep passing while
    exercising real git. Defaulting to this module's own `_git` is for a caller
    that has no seam of its own.

    Three states, because there are three:

    * `(True, <stdout>)` — git answered yes, and the answer is carried so a
      caller that wants the git dir does not ask twice.
    * `(False, "")` — git answered no.
    * `(None, <why>)` — **the call did not answer**, and nothing has been
      established about the repository at all.

    The third one is the whole point. Seven ops opened by reading this probe's
    `returncode != 0` as *no* and printing `not inside a git repository` — over
    a repository that was mid-merge with live conflict markers on disk. That is
    not a missing section, it is a positive false claim about the world, and a
    caller told it has no reason to retry.

    The failure path is deliberately unchanged: git exiting non-zero for its own
    reasons still returns `False` and still gets the loud refusal. A fix that
    folded every non-zero return into "could not tell" would have bought the
    false claim's silence with a real refusal's silence, which is the more
    expensive of the two.

    *why* is flattened even though this arm is reached only on `TIMEOUT_RC`,
    where the stderr is `_git`'s own (`timed out after Ns`). The first cut said
    so and left it raw, and #1475's census caught it: "our own text by
    construction" is a claim about a call graph, and the whole point of that
    census is that such claims go stale one refactor later. `flat` is
    idempotent, so being right costs nothing and being wrong costs a forged
    line at column 0 in a receipt.
    """
    run = _git if git_fn is None else git_fn
    res = run(args or ["rev-parse", "--git-dir"])
    if res.returncode == TIMEOUT_RC:
        return None, (_untrusted.flat(res.stderr.strip())
                      or f"git exited {TIMEOUT_RC}")
    if res.returncode != 0:
        return False, ""
    # Flattened by the same argument `repo_label` uses ~290 lines below (#1557,
    # #1475): git prints the directory's real name here, and a repository whose
    # path carries a line separator turns any line rendering it into two, the
    # second at column 0. Today `commit.py` only joins `MERGE_HEAD` onto this
    # and never prints it — but "no caller renders it" is a claim about a call
    # graph, which is exactly what the census exists to stop anyone relying on.
    #
    # The failure direction is checked, not assumed: on a path `flat` would
    # alter, the join stops matching, `in_merge` reads False, the pathspec
    # scoping stays on, and git refuses the partial commit outright. Loud.
    return True, _untrusted.flat(res.stdout.strip())


def unanswered_repo_lines(why: str, probe: str = "git rev-parse --git-dir") -> list[str]:
    """The third state's render, identical in every op that prints it (#1858).

    One wording in one place because the sentence is the fix: what a caller does
    next depends entirely on being able to tell "you are not in a repository"
    from "I could not find out". Deliberately does NOT contain the string
    `not inside a git repository`, so a reader — and a test — cannot mistake one
    for the other by substring.
    """
    # `why` is flattened here as well as at its source: `status.py` builds its
    # own from `branch_result.stderr` and never passes through `probe_repo`, so
    # the render is the only place that covers every caller. Idempotent (#1475).
    return [
        f"ERROR: could not tell whether this is a git repository — "
        f"`{probe}` did not answer ({_untrusted.flat(why)}).",
        "  Nothing was inspected. That is not the same as being outside a "
        "repository, so re-run rather than acting on this.",
    ]


def reject_fetch_option(remote: str, ref: str) -> str:
    """Non-empty error when a fetch (remote, ref) pair would smuggle an option.

    Both values land as bare argv elements in `git fetch <remote> <ref>`, and
    git parses any element beginning with '-' as an option before it reaches
    the refspec grammar. `--upload-pack=<cmd>` is the weaponised case: git
    *executes* <cmd> on fetch (proven on 2.46.2, #818) — unlike `ls-remote`,
    where the same value does not run.

    The dangerous values do not come from operator argv (checkout.py / merge.py
    already refuse a leading-dash argv REF, #150). They come from `@{upstream}`
    split into (remote, ref) — i.e. a remote-tracking ref name, which anyone who
    controls the remote can choose. A real remote or branch never starts with
    '-'; git refuses to create one. So a leading-dash value here is never a
    legitimate ref — it is an option in ref position, and it is refused by name
    rather than fetched silently (a dropped ref would fetch the wrong thing).

    Returns "" when the pair is safe. Callers decide loudness: a mutation
    (push) aborts; a refresh (merge) falls back to the local ref.
    """
    for label, val in (("remote", remote), ("ref", ref)):
        if val.startswith("-"):
            return (f"{label} {val!r} looks like a git option, not a {label} — "
                    f"a tracking ref named like `--upload-pack=…` executes a "
                    f"command on fetch (#818); refusing")
    return ""


def _list_conflicts() -> tuple[list[str], str]:
    """`(paths, why_unavailable)` — three states, not two (#650).

    `([...], "")` git answered and named conflicts; `([], "")` git answered and
    the tree is clean; `([], why)` git did not answer.

    The third state used to be the second, in all three copies of this
    function, and `git-conflicts` is the worst place in the tool to make that
    mistake: `main` renders an empty list as `No conflicted files.`, and you
    only run it because you are already stopped mid-merge — so a lookup that
    failed printed the one sentence that says "go ahead and commit". An index
    lock held by a concurrent git is enough to trigger it; no load required
    (docs/validators.md, "Declining instead of guessing").

    `resolve.py` reached the same hazard by a different route — its
    `Remaining: 0` line is followed by `Next: git-commit ...` — and `merge.py`
    was the lowest-stakes of the three. One function now, so the next fix does
    not have to find all three.

    **`-z`, and `core.quotePath` is deliberately not pinned (#1708).** The read
    was `diff --name-only --diff-filter=U`, line-separated, and five `resolve.py`
    receipt rows were written down as resting on `core.quotePath` octal-quoting
    every byte >= 0x80 out of it. That is a config default, not a git guarantee,
    and #1708 asked for `-c core.quotePath=true` to make the ground true by
    construction. Driving it showed the setting has no correct value, measured
    on git 2.46.2 against a real conflicted merge:

    * `quotePath=true` (the default, and what that pin would have frozen) hands
      back the octal-escaped *spelling* of an accented name, double quotes and
      all. That is not a filename. `git-resolve` fed it straight back as a
      pathspec and printed `did not match any file(s) known to git`, so **no
      conflicted path holding a byte >= 0x80 could be resolved at all** — and
      pinning the setting would have removed the one config under which it
      worked.
    * `quotePath=false` hands the name back raw, which is usable, but
      `str.splitlines()` folds on U+2028 / U+0085 / VT / FF, so one conflicted
      file named `sep<U+2028>two.txt` came back as **two** records, neither of
      them a file. C0 is quoted whatever this is set to, so LF and CR were never
      the exposure here; the separators Python invents are.

    `-z` removes the question instead of answering it. Paths come back raw and
    usable, and the records are NUL-separated — the one byte a pathname cannot
    contain on any platform, so a filename cannot forge the split rather than
    merely being unlikely to. `commit.py` reached the same conclusion at
    `diff --cached --name-only -z` (#1003, the same accented filename) and says
    at line 639 that it leaves `core.quotePath` alone on purpose: pinning it
    beside `-z` advertises a dependency the read does not have.

    **And it reads through `_git_verbatim`, because `-z` is only exact if
    nothing rewrites the bytes on the way back.** `_git` runs
    `subprocess.run(text=True)`, and Python's universal-newline translation
    turns a bare CR — and a CRLF — into LF *inside* a NUL record, before any
    split sees it. A POSIX filename may hold a CR (every byte but NUL and '/'
    is legal), so with text mode on, the name handed back was not the name on
    disk and the `git add -- <path>` that follows was aimed at a file that is
    not there. #1693 built `_git_verbatim` for exactly this shape.

    What `-z` costs is paid by the callers, not here. A raw path can carry a
    separator into a receipt row, so `resolve.py`, `conflicts.py` and `merge.py`
    put every rendered path through `_untrusted.flat(..., disclose_newline=True)`
    — the path spelling of #1557: the default renders a newline as a space,
    which turns "this file's name has a newline in it" into a plausible name
    that is not on disk, and makes two different real files render identically.
    """
    res = _git_verbatim(["diff", "--name-only", "--diff-filter=U", "-z"])
    if res.returncode != 0:
        return [], (res.stderr.strip() or f"git exited {res.returncode}")
    # NUL, not `splitlines()`: the record separator is the one character a
    # pathname cannot hold. Only genuinely empty records are dropped (git
    # terminates the last one, so the tail is always empty) — the old
    # `.strip()` test would also have dropped a name made of spaces.
    return [p for p in res.stdout.split(chr(0)) if p], ""


#: Said out loud wherever a `Repo:` line or a status header is printed, and the
#: string the tests key on. Short, upper-case and unpunctuated so a reader
#: scanning a wall of git output cannot mistake it for prose.
FOREIGN_WORKTREE_MARKER = "COPIED WORKTREE"


def _gitfile_target(dot: str) -> Optional[str]:
    """The git directory a `.git` *file* names, absolute — or None.

    A linked worktree and a submodule both have a gitfile; this only reads it,
    it does not decide which of the two it found.
    """
    try:
        with open(dot, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("gitdir:"):
                    target = line.split(":", 1)[1].strip()
                    if not target:
                        return None
                    if not os.path.isabs(target):
                        target = os.path.join(os.path.dirname(dot), target)
                    return os.path.normpath(target)
    except OSError:
        return None
    return None


def _same_path(a: str, b: str) -> bool:
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


class ForeignWorktree(NamedTuple):
    """The two paths the copied-worktree banner names, **ready to render**.

    Both fields are flattened (#1557) and neither is usable as a path any more:
    a separator in one arrives as ``␊``/``[U+000A]``, so `open()` on it would
    miss. That is the point — every consumer of this tuple prints it, and the
    field names say so at the next call site rather than in a comment three
    files away. The comparison that decides whether there is a copy at all runs
    on the raw values, inside `foreign_worktree()`, before either is flattened.
    """

    here_display: str
    registered_display: str


def foreign_worktree(start: Optional[str] = None) -> Optional[ForeignWorktree]:
    """`(this tree, the tree git has registered)` when they are not the same one.

    A linked worktree's `.git` is a gitfile — one line of text naming the real
    git directory — so `cp -a` copies the *pointer*. Every git command in the
    copy then reads and writes the ORIGINAL worktree's index, HEAD and refs,
    with nothing in any output saying so. That is how #1536 happened: a
    `git checkout <sha> -- <path>` inside a copy staged a revert of two files
    into a worktree nobody was watching.

    It is decidable exactly and locally, with no filesystem scan and no spawn:
    `.git/worktrees/<name>/gitdir` holds the path of the `.git` file git
    registered for that worktree. If this directory's own `.git` is not that
    file, this directory is not the registered one.

    None means "no reason to think otherwise", and is deliberately returned for
    every case that cannot be settled — an unreadable back-pointer, a submodule
    gitfile (`.git/modules/...`, not `worktrees/...`), no repository at all. A
    banner printed on a run that could not tell is a banner nobody reads on the
    run that could.
    """
    here = os.path.abspath(start if start is not None else os.getcwd())
    # Walk up the way git does: `cd src && git status` must reach the same
    # answer as one run at the top, or `cd` hides the disclosure.
    while True:
        dot = os.path.join(here, ".git")
        if os.path.exists(dot):
            break
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent
    if not os.path.isfile(dot):
        return None  # an ordinary repository: `.git` is the directory itself
    admin = _gitfile_target(dot)
    if admin is None:
        return None
    if os.path.basename(os.path.dirname(admin)) != "worktrees":
        return None  # a submodule, or a layout this cannot speak about
    try:
        with open(os.path.join(admin, "gitdir"), "r",
                  encoding="utf-8", errors="replace") as handle:
            registered_dot = handle.read().strip()
    except OSError:
        return None
    if not registered_dot or _same_path(registered_dot, dot):
        return None
    # Flattened HERE, at the producer, rather than in `foreign_worktree_note()`
    # (#1557). Both values are paths off disk that nothing gates: `gitdir` holds
    # whatever wrote it, and git itself writes the path handed to
    # `git worktree add`, so a worktree directory whose *name* contains a line
    # separator forges through the ordinary route with no attacker involved.
    # There are three renders, not one — this note, and the prose line under it
    # in `status.py` and in `worktrees.py`, each of which prints `here` — plus
    # `repo_label()`, i.e. the `Repo:` line of `git-commit`, `git-push` and —
    # since #1569, which is this same argument re-filed — `git-diff`. Flattening
    # at the note would have covered one of the four.
    #
    # `disclose_newline=True` because the value is a path: the space `flat()`
    # gives a title would render a directory name that is not on disk, and the
    # reader of this banner has to be able to identify which tree is meant.
    return ForeignWorktree(
        _untrusted.flat(here, disclose_newline=True),
        _untrusted.flat(os.path.dirname(registered_dot), disclose_newline=True),
    )


def _with_foreign_note(label: str) -> str:
    """#1536: in a copied worktree, `--show-toplevel` names the copy — which is
    the one directory the write does NOT reach. This line exists to say where a
    write landed (#692), so it has to name the other tree, not only this one."""
    found = foreign_worktree()
    if found is None:
        return label
    return f"{label}\n  {foreign_worktree_note(found)}"


def foreign_worktree_note(found: ForeignWorktree) -> str:
    """One line naming the tree a write from here will actually land in.

    One line, and nothing off disk can make it two: `found` is flattened by its
    producer (#1557).
    """
    return (f"⚠ {FOREIGN_WORKTREE_MARKER} — this directory is not the one git "
            f"registered for its git directory; the index, HEAD and refs "
            f"reached from here belong to {found[1]} (#1536)")


def repo_label() -> str:
    """Absolute path of the repo the calling op is acting on, **to print**.

    A display string, not an openable path (#1557, same reason as
    `ForeignWorktree`): the directory's real name is flattened on the way out,
    so a separator in it arrives as ``␊``/``[U+000A]`` rather than making a
    second line at column 0 under a `Repo:` the reader takes as the tool's.
    All three call sites — `git-commit`, `git-push` and, since #1569,
    `git-diff` — interpolate it into one printed line and nothing else.

    `git-diff` has printed a `Repo:` line for a long time; `git-commit` and
    `git-push` did not — so the two ops that WRITE were the two that never said
    where they wrote. When a commit lands somewhere unexpected, that line is
    the difference between noticing within the minute and noticing next week
    (#692). `git-diff` then kept building its own line out of a raw
    `--show-toplevel` for two more rounds of this defect class, which is #1569:
    a seam nobody is routed to is a seam that covers one caller.

    The work tree when there is one, the git dir otherwise: a bare repo has no
    top level, and printing an empty string there would be worse than printing
    nothing. "unknown" rather than a guess when git answers neither — a wrong
    repo name is the one output worse than no repo name.
    """
    # Flattened for the same reason the foreign-worktree paths are, and by the
    # same argument (#1557): git prints the directory's real name here, and a
    # repository checked out under a name carrying a line separator makes this
    # line into two — the second at column 0, in the render of an op that
    # WRITES. That needs no copied worktree and no `gitdir` file at all.
    top = _git(["rev-parse", "--show-toplevel"])
    if top.returncode == 0 and top.stdout.strip():
        return _with_foreign_note(
            _untrusted.flat(top.stdout.strip(), disclose_newline=True))
    bare = _git(["rev-parse", "--absolute-git-dir"])
    if bare.returncode == 0 and bare.stdout.strip():
        return f"{_untrusted.flat(bare.stdout.strip(), disclose_newline=True)} (bare)"
    return "unknown"


def install_dir() -> str:
    """Directory holding the `supertool` wrapper and `supertool.py`.

    Two levels above `presets/git/`. Named rather than inlined because every
    printed remedy depends on it and a test has to be able to stand a fake
    install somewhere else; `push.py` carried its own copy as `_repo_root`.
    """
    return os.path.dirname(os.path.dirname(_HERE))


def _quoted_interpreter() -> str:
    """`sys.executable`, quoted for the shell that will receive it if it must be.

    An interpreter path with a space in it is the ordinary Windows install —
    `C:\\Program Files\\Python312\\python.exe` — and a POSIX box gets one from any
    user whose home has a space. Unquoted, the hint asks the shell to run a program
    named `C:\\Program` and the remedy fails for the same reason #1017 filed:
    a printed command that is wrong about where it will be pasted.

    Double quotes on Windows because they are the only form both `cmd.exe` and
    PowerShell honour; `shlex.quote` elsewhere because it is POSIX's own answer.
    """
    exe = sys.executable
    if " " not in exe:
        return exe
    return '"' + exe + '"' if os.name == "nt" else shlex.quote(exe)


def _wrapper_is_runnable(path: str) -> bool:
    """Best-effort probe: is `path` runnable the way `./supertool` prints it?

    POSIX has an execute bit -- `os.access(path, os.X_OK)` reads it straight
    off the mode. Windows has none: `os.access` answers true for any file
    that merely exists there, so the probe degrades to "does this name
    exist" and the helper's third state (no runnable supertool found)
    becomes unreachable (#1919). In its place: the first two bytes must be a
    shebang (`#!`), matching every wrapper this project's own install
    instructions produce (README.md) -- a symlink to `supertool.py`, itself
    `#!/usr/bin/env python3`. This is the cheapest of the three checks the
    issue weighed, deliberately -- this runs on a refusal path where the
    caller is already stuck, so a spawn attempt is the most truthful answer
    and also the most expensive one to hand a caller who is waiting. What it
    cannot establish: that the interpreter the shebang names exists or is on
    PATH, that a POSIX-aware shell is what receives `./supertool` on this
    machine at all (native `cmd.exe` and PowerShell ignore shebangs
    entirely), or that the file is not truncated past those two bytes -- only
    that whoever put it there did not leave a stray, unrelated file wearing
    this name. Duplicated in `presets/_st_hint.py` for the same reason
    `st_hint` itself is -- see this module's docstring.
    """
    if os.name == "nt":
        try:
            with open(path, "rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return os.access(path, os.X_OK)


def st_hint(arg: str) -> str:
    """A runnable supertool invocation for `arg`, for printed remedies.

    A printed command is a claim about the environment it will be pasted into,
    and every one of these was written from the environment of whoever wrote
    it (#1012). `./supertool` is a gitignored symlink: present in a clone,
    absent in a linked worktree — which is where agents work. In *this* repo it
    is worse than absent there, because the global `supertool` on PATH then
    resolves to the live clone and runs master's core against the branch's
    presets, the mixed tree #678 discloses after the fact and the repo's own
    rule forbids in advance. So the hint is decided by what is on disk beside
    the presets, never by what the author had.

    That matters most where it is printed. A rule in a docs page is consulted;
    a command in an error is pasted, by a reader who is mid-conflict and least
    likely to second-guess it.

    The interpreter is `sys.executable` — the one demonstrably running this
    code — never the literal `python3` (#1017). `python3` is not the launcher on
    Windows, where it is `py` or `python`, so the hard-coded spelling printed a
    remedy that did not run on the platform this project cannot see. `_watch_argv`
    had already resolved the spawn that way in #642; this is the same answer to
    the same question, which is what its docstring claimed and the code did not.

    Three states. With neither route present the invocation is unknown, and an
    invented one would be a remedy that cannot be run — the defect one layer
    down. Grown out of `push.py::_st_hint` (#879), which had the first two.
    """
    root = install_dir()
    wrapper = os.path.join(root, "supertool")
    if os.path.isfile(wrapper) and _wrapper_is_runnable(wrapper):
        return "./supertool " + chr(39) + arg + chr(39)
    if os.path.isfile(os.path.join(root, "supertool.py")):
        return _quoted_interpreter() + " supertool.py " + chr(39) + arg + chr(39)
    return ("(no runnable supertool found in " + root + " — the op is "
            + chr(39) + arg + chr(39) + ")")


def _looks_like_success(line: str) -> bool:
    """True for lines that report success — must never be picked as an error.

    Pre-push / pre-receive hooks that auto-format print green '✅ … 0 errors.'
    lines and 'pushed successfully' notices; the substrings 'errors' /
    'fatal' would otherwise match the error scan and surface a success line
    as the "first error".
    """
    s = line.strip()
    if not s:
        return False
    low = s.lower()
    has_success = ("✅" in s or "✓" in s or any(m in low for m in (
        "0 errors", "no errors", "pushed successfully", "successfully pushed")))
    if not has_success:
        return False
    # A success marker doesn't win if the same line also carries a hard error
    # signal (e.g. 'lint ✓ — push blocked: error: …'). 'error:' (with colon)
    # avoids matching the '0 errors' / 'no errors' success phrases.
    has_error = any(k in low for k in (
        "error:", "fatal", "rejected", "aborted", "failed", "declined")) \
        or "! [" in s or "❌" in s
    return not has_error


#: The keywords that make a line an error line, as WORDS (#1669).
#:
#: `"error" in low` was the selector, and on 2026-08-14 it promoted a pre-push
#: hook's own SUCCESS disclosure to `First error:` on a push that died in
#: transport — the substring it matched was the one inside the identifier
#: `OSError`, in a footnote explaining a skip counter. Measured, because the
#: issue asked which of three candidates it was: not proximity (the line sat
#: ~200 lines earlier in a passing summary), not the head/tail elision (this
#: scan runs over the whole stream), a bare substring.
#:
#: `errors?` rather than `error`, so `3 errors found` still selects; the plural
#: is the only inflection any of these five take in git's or a test runner's
#: output, and a stem match is what put the defect here.
_ERROR_WORD_RE = re.compile(
    r"\b(errors?|fatal|rejected|aborted|failed)\b", re.IGNORECASE)


def _first_error_line(text: str) -> str:
    """First line mentioning an error/rejection, else last non-empty line.

    Skips success lines (green ✅, '0 errors', 'pushed successfully') so a
    hook's success banner is never misreported as the failure cause.

    **The keywords are words, not substrings (#1669).** `"error" in low` read
    `OSError` as an error and promoted a footnote out of a passing suite's
    disclosure block to `git-push`'s `First error:` on a push that died in
    transport. `_ERROR_WORD_RE` above is where the narrowing lives, and
    `push._push_error_line` is the other half: which process wrote the line
    decides more than which words are in it.

    **Flattened here, not at the callers (#1475).** `text` is a child's stream
    — a commit hook's, a remote's, `gh`'s — and every caller prints what comes
    back at column 0 or interpolates it into the `[result]` line itself. Fixing
    that per call site is how #1470 closed one op and left seven more, so the
    flatten lives at the seam every one of them already goes through.
    `_untrusted.flat` is idempotent, so `git-push`, which flattens the return
    again at its own render, pays a no-op rather than a second substitution.

    `_untrusted.split_lines`, not `str.splitlines()`, for the same issue's
    other half: this is a line-oriented *parse*, and `str.splitlines()` folds
    on ten separators no git stream defines (#1081). A writer who put a U+2028
    in a line therefore chose which line this scan returned — and the tail it
    hid was dropped from the receipt entirely rather than disclosed.
    """
    lines = _untrusted.split_lines(text)
    for line in lines:
        s = line.strip()
        if not s or _looks_like_success(s):
            continue
        if _ERROR_WORD_RE.search(s) or "! [" in s or "❌" in s:
            return _untrusted.flat(s)
    for line in reversed(lines):
        s = line.strip()
        if s and not _looks_like_success(s):
            return _untrusted.flat(s)
    return ""


# How much of a child's transcript a `--- git output ---` dump carries. Head
# *and* tail, never a plain tail: a hook announces which arm it took on its
# FIRST line and pytest names the failing tests in a summary at the very END,
# so one end alone drops either the disclosure or the cause. Measured
# 2026-08-12 pushing to a local `master`: the unbounded dump was an 11,449-item
# pytest run, ~11,000 lines, inside a receipt (#1454, #1490).
GIT_OUTPUT_HEAD_LINES = 5
GIT_OUTPUT_TAIL_LINES = 30

#: What marks a relayed line as the child's rather than the tool's. `> ` and
#: not `| `, matching the stderr half of `push._report_prepush_hook`: a
#: `--- git output ---` dump is stdout and stderr concatenated, so its
#: provenance is exactly the "three processes write here and nothing marks the
#: boundary" case that prefix already means.
RELAY_PREFIX = "> "

#: The one opening delimiter a relayed transcript has. Printed only by
#: `relayed_block`, so that nothing can print it without the prefix below it
#: (#1569) — `tests/test_column_zero_renders_1569.py` is the census.
RELAY_HEADER = "--- git output ---"


def bounded_lines(lines: list[str], head: int = GIT_OUTPUT_HEAD_LINES,
                  tail: int = GIT_OUTPUT_TAIL_LINES) -> list[str]:
    """`lines`, elided in the middle if long — and never silently.

    Both ends are kept rather than a tail, because both ends carry the answer:
    a hook announces which arm it took on its first line and its outcome on its
    last, and a pytest run names the failing tests in a summary at the very end.
    The message itself says only what was dropped — this is also the generic
    dump for a push git refused, where no hook need be involved at all.
    """
    if len(lines) <= head + tail + 1:
        return lines
    return (lines[:head]
            + [f"... {len(lines) - head - tail} line(s) not shown - first "
               f"{head} and last {tail} kept; re-run the push by hand to see "
               "all of it"]
            + lines[-tail:])


def relayed_lines(lines: list[str]) -> list[str]:
    """Each line of a child's output, kept to one line of ours (#1470).

    The `| ` / `> ` prefix a relay puts in front of a line is the only thing
    separating a third party's bytes from supertool's own output — and a prefix
    holds only for as long as the line stays one line. `_untrusted.split_lines`
    cuts on LF / CR / CRLF alone, by design (#1081), so a U+2028 survives
    *inside* a relayed line and puts everything after it back at column 0 for
    every consumer that splits the way `str.splitlines()` does. #623 made
    `[result]` the line a caller reads as the verdict, and a forged one sorts
    first.

    `remote:` lines are written by whatever server you push to, so on the
    stderr path the text is a third party's outright. The local pre-push hook
    is code already running on this machine and is no escalation on its own; it
    goes through the same call because the seam is the same one, it costs
    nothing, and a half-flattened seam is the one that gets re-filed.

    ESC goes with it, which is the other way to rewrite a receipt — a relayed
    `ESC [2K ESC [1A` deletes the line above it (#851's argument, applied to a
    child stream). Disclosed, never stripped: the forged text stays legible as
    `[U+2028]` and `[U+001B]`.

    Tabs are kept, which is why this is not `flat()`. `flat()` drops them
    because it renders a one-line *field* on a line the tool owns, where a tab
    can imitate a board's column structure; a relayed transcript is neither —
    it is the child's own lines under a prefix or a header, and no consumer
    parses it by column. A tab cannot make a line and cannot move a cursor
    anywhere it has not already been, so keeping it is exactly the trade
    `scrub()` makes for a block, and dropping it would render every
    tab-aligned hook transcript and git porcelain block as `[U+0009]` soup —
    breaking the thing #1448 shipped four hours earlier, for no forgery it
    prevents.

    **In `_git_common`, not in `push` (#1569).** It was defined in `push.py`,
    where no sibling preset can reach it, so `commit._failure_receipt` restated
    its body — and dropped the prefix while claiming in its own docstring to
    relay "exactly as `push._relayed_lines` does it". A seam a caller cannot
    import is a seam that gets copied, and a copy is where the property is lost.
    """
    return [_untrusted.visible(ln, keep=chr(9)) for ln in lines]


def relayed_block(text: str, *, head: int = GIT_OUTPUT_HEAD_LINES,
                  tail: int = GIT_OUTPUT_TAIL_LINES) -> list[str]:
    """A child's whole transcript as lines of ours: header, then every line
    under `> `.

    The header alone was the containment until #1569, and a header is an
    *opening* delimiter with no close: under it the child's lines sat at column
    0, so a pre-commit hook printing `Status: COMMITTED` or `[result] 1 op run`
    wrote lines a consumer cannot tell from supertool's own. `relayed_lines`
    guarantees one line stays one line; the prefix is what says whose line it
    is. Neither half works without the other, so they are emitted together
    here rather than left to four call sites to pair up correctly.

    Bounded, because the commonest thing that produces one of these is a hook
    that ran a test suite and its transcript is not the receipt (#1448, #1490).
    `(no output)` rather than an empty block: a dump that prints nothing and a
    child that printed nothing are the same shape on screen, which is this
    repo's own defect class.
    """
    lines = relayed_lines(bounded_lines(
        _untrusted.split_lines(text.strip()), head, tail))
    if not lines:
        return [RELAY_HEADER, "(no output)"]
    return [RELAY_HEADER] + [RELAY_PREFIX + ln for ln in lines]


#: One page of open PRs is enough for every worktree a fleet realistically
#: holds, and the cap is remembered so a page that *hit* it can decline for the
#: branches it did not name (see `PrIndex.truncated`).
PR_INDEX_LIMIT = 100

#: The batched lookup is one network call on an otherwise local op, so it gets
#: a short budget. A slow answer is `unknown`, never "no PR".
PR_INDEX_TIMEOUT = 8

#: Fields the index needs. `statusCheckRollup` rides the same call, which is
#: what makes the tally free — the alternative was a per-PR fetch, i.e. the N
#: calls this function exists to avoid.
PR_INDEX_FIELDS = ("number,headRefName,baseRefName,isDraft,mergeable,"
                   "statusCheckRollup,url")


class PrIndex:
    """Open PRs keyed by head branch — or a stated reason for not knowing.

    `by_branch is None` is the whole point of the class. `query_open_mr` below
    returns `None` for *both* "there is no PR" and "the lookup never ran", and
    a caller cannot tell those apart — which is fine for an advisory line under
    a push and wrong for a board somebody reads to decide where to work. Here
    the two are different objects:

    * `PrIndex({...})`      — GitHub answered; a branch absent from the map has
      no open PR, and that is a fact about the world.
    * `PrIndex(None, why)`  — GitHub did not answer; nothing at all is known
      about any branch, and `why` says what stopped it.

    `truncated` is the third case and the sneaky one: the page came back but
    hit its own limit, so it is authoritative for the branches it *names* and
    establishes nothing about the ones it does not.
    """

    __slots__ = ("by_branch", "reason", "truncated", "limit")

    def __init__(self, by_branch: Optional[dict], reason: str = "",
                 truncated: bool = False, limit: int = PR_INDEX_LIMIT) -> None:
        self.by_branch = by_branch
        self.reason = reason
        self.truncated = truncated
        self.limit = limit

    @property
    def answered(self) -> bool:
        return self.by_branch is not None

    def get(self, branch: str) -> Optional[dict]:
        """The open PR for `branch`, or None. Only meaningful when `answered`."""
        if not self.by_branch or not branch:
            return None
        return self.by_branch.get(branch)

    def __repr__(self) -> str:
        if self.by_branch is None:
            return f"PrIndex(None, {self.reason!r})"
        return f"PrIndex({len(self.by_branch)} branches, truncated={self.truncated})"


def _run_gh_pr_list(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True,
                          timeout=PR_INDEX_TIMEOUT, encoding="utf-8",
                          errors="replace")


#: Only the head ref is needed to answer "was this branch merged", plus the
#: number so the row can cite the PR it was decided by. Asking for the check
#: rollup here would multiply the payload by the number of legs for a column
#: nobody renders.
MERGED_PR_FIELDS = "number,headRefName"

#: How many branches go into one `--search` query. GitHub ORs repeated `head:`
#: qualifiers, so N branches cost one call rather than N — but an unbounded
#: query is an unbounded URL, and a fleet is a couple of dozen worktrees, so
#: this only ever chunks in an implausible case.
MERGED_PR_CHUNK = 30


def query_merged_prs_for_branches(branches, runner=None) -> PrIndex:
    """Merged PRs whose head is one of `branches` — keyed by head branch.

    This exists because `git for-each-ref --merged` is an ancestry test and a
    squash merge leaves no ancestry: the squashed commit has no parent link to
    the branch it came from, so a fully-merged branch is not an ancestor of
    the base and reads as unmerged forever (#1229).

    **Scoped by `--search head:…`, not paged.** The obvious implementation is
    `gh pr list --state merged --limit N` and it does not work here: merged
    PRs accumulate forever, so N is a cap the repo grows past and never comes
    back under. Measured 2026-08-10 — 632 merged PRs against a first attempt
    at `--limit 400`, which made every unmerged branch render `merge unknown`
    permanently. That is honest and useless: a third state that is always the
    answer has replaced the wrong answer with no answer.

    Scoping inverts the arithmetic. The query asks about the branches we hold
    — a couple of dozen — so the result set is bounded by the question rather
    than by the repository's history, and the cap stops growing. It is also
    faster: 0.7s against 3.9s for the 800-item page.

    A chunk that fails or hits its own limit declines for the **whole** board
    rather than returning what the other chunks found. A partial map is
    indistinguishable from a complete one at every call site, which is the
    defect this whole class of three-state contract exists to prevent.
    """
    names = [b for b in dict.fromkeys(branches) if b]
    if not names:
        return PrIndex({}, limit=0)
    found: dict = {}
    last_limit = 0
    for start in range(0, len(names), MERGED_PR_CHUNK):
        chunk = names[start:start + MERGED_PR_CHUNK]
        # Headroom over the branch count: one branch can carry several merged
        # PRs, and a result set that exactly fills the limit cannot be told
        # from one that was cut.
        limit = max(2 * len(chunk), 20)
        last_limit = limit
        idx = _pr_index(
            ["pr", "list", "--state", "merged", "--json", MERGED_PR_FIELDS,
             "--search", " ".join("head:" + b for b in chunk),
             "--limit", str(limit)], limit, runner)
        if not idx.answered:
            return idx
        if idx.truncated:
            return PrIndex(None, f"the merged-PR search hit its {limit}-item "
                           "cap, so its answer is incomplete", limit=limit)
        found.update(idx.by_branch or {})
    return PrIndex(found, limit=last_limit)


def query_open_prs_by_branch(limit: int = PR_INDEX_LIMIT, runner=None) -> PrIndex:
    """Every open PR of this repo, keyed by head branch, in **one** call.

    N worktrees must not mean N lookups: `gh pr list` already returns the head
    ref of every open PR, so the join is a dict build, not a fan-out. The
    caller passes a `runner` in tests; the default shells out to `gh`.

    Every failure route ends in `PrIndex(None, reason)` — a missing binary, a
    non-GitHub remote, an expired token, a rate limit, a timeout, output that
    is not the JSON array it should be. None of them return an empty map,
    because an empty map is a claim.
    """
    return _pr_index(["pr", "list", "--state", "open", "--json",
                      PR_INDEX_FIELDS, "--limit", str(limit)], limit, runner)


def _pr_index(args: list, limit: int, runner=None) -> PrIndex:
    """Run one `gh pr list` and key it by head ref — or decline, with a reason.

    Shared by the open and the merged lookups (#1229). Both need the identical
    seven failure routes and the identical `truncated` rule, and the second
    copy of them is where the two would drift: an added route on one side is
    silently a missing one on the other, and a missing route here returns an
    empty map, which is a claim.
    """
    run = runner or _run_gh_pr_list
    args = list(args) + _repo_target_args()
    try:
        res = run(args)
    except subprocess.TimeoutExpired:
        return PrIndex(None, f"gh pr list did not answer within {PR_INDEX_TIMEOUT}s",
                       limit=limit)
    except FileNotFoundError:
        return PrIndex(None, "gh is not installed — the tracker cannot be read here",
                       limit=limit)
    except OSError as exc:
        return PrIndex(None, f"gh could not be run ({exc})", limit=limit)

    if res.returncode != 0:
        blob = (res.stderr or "") + chr(10) + (res.stdout or "")
        why = _first_error_line(blob) or f"gh pr list exited {res.returncode}"
        return PrIndex(None, why, limit=limit)
    try:
        prs = json.loads(res.stdout or "")
    except (json.JSONDecodeError, ValueError):
        return PrIndex(None, "gh pr list returned output that is not JSON", limit=limit)
    if not isinstance(prs, list):
        return PrIndex(None, "gh pr list returned JSON that is not a list", limit=limit)

    by_branch: dict = {}
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        head = pr.get("headRefName")
        if isinstance(head, str) and head and head not in by_branch:
            by_branch[head] = pr
    return PrIndex(by_branch, truncated=len(prs) >= limit, limit=limit)


def _repo_target_args() -> list:
    """`--repo OWNER/NAME` when a repo target is set, else nothing (#673).

    Imported lazily: `_git_common` is loaded by presets that have no business
    depending on the gh family, and an import error there must not break a
    commit.
    """
    try:
        import _repo_target  # noqa: PLC0415  (deliberately lazy — see docstring)
    except ImportError:
        return []
    return _repo_target.gh_args()


#: Budget for one branch→MR/PR lookup. Advisory output hanging off an
#: otherwise local op, so it stays short — but since #948 a lookup that
#: outlasts it is `unknown`, never "no MR".
MR_LOOKUP_TIMEOUT = 5

#: What `glab` / `gh` say when they mean "there is no such request here". That
#: is an answer about the world, and the branch has no MR — rendered as
#: silence, because a branch with no MR yet is the most ordinary state a branch
#: can be in. Both CLIs exit non-zero for this *and* for an expired token, so
#: the exit code cannot tell them apart and the sentence has to.
NO_REQUEST_PHRASES = (
    "no open merge request",
    "no merge request",
    "no pull request",
)

#: The other host's CLI, in a repo that is not on its host: a GitLab repo runs
#: `gh` too, and a GitHub repo runs `glab`. Neither an answer about this branch
#: nor a failure worth recording — structural and permanent, so counting it as
#: "could not answer" would put a decline on every push of every GitLab repo
#: with `gh` installed, and a line that appears on every call stops being read.
NOT_THIS_HOST_PHRASES = (
    "none of the git remotes",
    "no git remotes found",
    "no remotes found",
)

#: What `glab` / `gh` say when they mean "nobody has logged me in". A failure,
#: not an answer — but the CLIs say it across several lines, and
#: `_first_error_line` finds no error keyword in any of them, so it falls
#: through to the *last* non-empty line. For `gh` inside GitHub Actions that
#: line is `GH_TOKEN: ${{ github.token }}` — a YAML fragment out of an example
#: block, offered to the reader as the reason their lookup did not run. The
#: disclosure was right and unreadable, which is only half a disclosure.
NOT_AUTHENTICATED_PHRASES = (
    "gh_token environment variable",
    "glab_token environment variable",
    "auth login",
    "not logged in",
    "no token provided",
)

#: The union, under the name `presets/git/status.py` gave it. That module had
#: the only copy of these phrases and now imports them (#948) — the same rule
#: about the same two CLIs, in one place, because a second copy beside the real
#: one is what this repo's issue tracker is largely made of.
ANSWERED_NONE = NO_REQUEST_PHRASES + NOT_THIS_HOST_PHRASES


class MrLookup:
    """The open MR/PR for a branch — or a stated reason for not knowing (#948).

    `query_open_mr` used to return `None` for *both* "there is no open MR/PR
    for this branch" and "the lookup did not happen", and swallowed every
    exception on the way. Its callers are `git-push` and `git-commit`, so the
    ambiguity was resolved — as an absence, every time — at the moment somebody
    reads the output to decide whether to open a request. `git-push` then told
    a caller who asked for `:watch` that the branch has no MR/PR *yet* and to
    open one, out of a lookup that never completed.

    Two objects, not one value:

    * `MrLookup(mr)` / `MrLookup(None)` — a CLI answered. `mr` is the request,
      or `None` meaning there genuinely is none.
    * `MrLookup(None, why)` — nothing answered, and `why` says what stopped it.
      Nothing at all is known about this branch.

    Deliberately *not* a blocking condition. A tracker that cannot be reached
    is not a reason to refuse to publish work — the receipt degrades to a
    stated unknown and the push proceeds (see `push.py::_mr_unknown_line`).
    """

    __slots__ = ("mr", "reason")

    def __init__(self, mr: Optional[dict] = None, reason: str = "") -> None:
        self.mr = mr
        self.reason = reason

    @property
    def answered(self) -> bool:
        return not self.reason

    def __repr__(self) -> str:
        if self.reason:
            return f"MrLookup(unanswered, {self.reason!r})"
        return f"MrLookup({self.mr!r})"


def _cli_verdict(res: subprocess.CompletedProcess) -> tuple:
    """(state, why) for a CLI that exited non-zero — the three-way read.

    `answered` — it said there is no such request. `n/a` — it said this repo is
    not on its host, which establishes nothing and never will. `failed` —
    anything else, including every authentication and network error, carried
    with the CLI's own first line so the reader is not sent to fix the wrong
    thing.
    """
    said = ((res.stderr or "") + (res.stdout or "")).lower()
    if any(p in said for p in NOT_THIS_HOST_PHRASES):
        return "n/a", ""
    if any(p in said for p in NO_REQUEST_PHRASES):
        return "answered", ""
    if any(p in said for p in NOT_AUTHENTICATED_PHRASES):
        # Checked after the two above deliberately: glab's not-this-host text
        # also says "please use `glab auth login`", and that case is an answer.
        return "failed", ("is not authenticated here — nothing asked the "
                          "tracker anything (`auth login`, or a token in the "
                          "environment)")
    blob = (res.stderr or "") + chr(10) + (res.stdout or "")
    return "failed", _first_error_line(blob) or f"exited {res.returncode}"


def _probe_open_request(argv: list, parse) -> tuple:
    """One CLI list call. Returns (mr, state, why) — never raises.

    Every route that used to fall into a bare `except … : pass` now names
    itself. The exceptions are still caught — this is advisory output and must
    not take a push receipt with it — but catching is not the same as
    discarding what was caught.
    """
    tool = argv[0]
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=MR_LOOKUP_TIMEOUT, encoding="utf-8",
                             errors="replace")
    except subprocess.TimeoutExpired:
        return None, "failed", f"`{tool}` timed out after {MR_LOOKUP_TIMEOUT}s"
    except FileNotFoundError:
        # Vanished between `which` and here. Nothing on this machine was going
        # to answer through it, so it is the not-installed case, not a failure.
        return None, "n/a", ""
    except OSError as exc:
        return None, "failed", f"`{tool}` could not be run ({exc})"
    if res.returncode != 0:
        state, why = _cli_verdict(res)
        return None, state, (f"`{tool}` {why}" if why else "")
    body = (res.stdout or "").strip()
    if not body.startswith("["):
        return None, "failed", f"`{tool}` answered with output that is not JSON"
    try:
        rows = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, "failed", f"`{tool}` answered with output that is not JSON"
    if not isinstance(rows, list):
        return None, "failed", f"`{tool}` answered with JSON that is not a list"
    if not rows:
        return None, "answered", ""
    if not isinstance(rows[0], dict):
        return None, "failed", f"`{tool}` answered with an entry that is not an object"
    return parse(rows[0]), "answered", ""


def _glab_fields(row: dict) -> dict:
    pipeline = row.get("pipeline") or row.get("head_pipeline") or {}
    if not isinstance(pipeline, dict):
        pipeline = {}
    return {
        "source": "gitlab",
        "iid": row.get("iid") or row.get("number") or "?",
        "target": row.get("target_branch", "?"),
        "pipeline": pipeline.get("status"),
        "pipeline_id": pipeline.get("id"),
        "pipeline_url": pipeline.get("web_url"),
        "merge_status": row.get("detailed_merge_status")
        or row.get("merge_status"),
    }


def _gh_fields(row: dict) -> dict:
    # gh: mergeable is CONFLICTING / MERGEABLE / UNKNOWN
    gh_merge = row.get("mergeable")
    return {
        "source": "github",
        "iid": row.get("number", "?"),
        "target": row.get("baseRefName", "?"),
        "pipeline": None,
        "pipeline_id": None,
        "pipeline_url": None,
        "merge_status": "cannot_be_merged" if gh_merge == "CONFLICTING" else None,
    }


def _is_local_remote(url: str) -> bool:
    """True when this remote URL cannot possibly be a forge.

    Conservative on purpose, and in one direction only: it returns True only
    for shapes that are positively local, and False for anything it does not
    recognise. A wrong True is the expensive mistake — it would let the
    function below state an absence about a repo that does have a tracker.
    """
    u = url.strip()
    if not u:
        return False
    if u.startswith("file://"):
        return True
    if u.startswith(("/", "./", "../", "~")) or u[:1] == chr(92):
        return True
    if len(u) > 1 and u[1] == ":" and u[0].isalpha() and u[0].isascii():
        # A Windows drive letter, and the reason this function has a test per
        # shape. `C:/Users/…/remote.git` has a colon before the first slash, so
        # the scp-style rule below reads `C` as a hostname and calls a temp
        # directory a forge — which is exactly what happened, on the Windows
        # leg only, after the POSIX legs went green. One character before the
        # colon is the discriminator: a real host name is never one letter.
        return True
    scheme, sep, rest = u.partition("://")
    if sep and rest and scheme and scheme.replace(
            "+", "").replace(".", "").replace("-", "").isalnum():
        return False  # ssh:// git:// https:// … — a host is named
    if ":" in u.split("/", 1)[0]:
        return False  # scp-style `git@host:path`
    return True  # a bare relative path, e.g. `../sibling.git`


def _remotes_could_host_a_request() -> tuple[Optional[bool], str]:
    """Can any configured remote hold an MR/PR? Three states, not two (#948).

    `(True, "")` at least one remote names a host; `(False, "")` git answered
    and every remote is a local path (or there are none), so there is no
    tracker for a request to be open on; `(None, why)` git did not answer, and
    nothing has been established.

    This exists because the CLIs check their own credentials *before* they look
    at the repository. `gh` with no token exits 4 saying so and never reaches
    "none of the git remotes ... point to a known GitHub host", which is the
    sentence `NOT_THIS_HOST_PHRASES` reads to turn that case into an answer. On
    a CI runner — `gh` installed, no token, sandbox repo whose only remote is a
    path under /tmp — the #948 disclosure therefore fired on a push where every
    check had in fact run, which is the one thing it promised not to do.

    The fact was local the whole time. Asking git for it costs one call and is
    not a guess: a remote at `/tmp/x/remote.git` has no tracker, in the same
    way and for the same reason that a GitHub repo has no GitLab MR.
    """
    try:
        res = _git(["remote", "-v"])
    except OSError as exc:
        # `_git` lets an OSError escape by design — `push.py` guards each of its
        # own calls and #675 pins that. This call site is inside an advisory
        # lookup that must never take a receipt with it, and a machine with no
        # `git` on PATH is precisely the "git did not answer" state below, not
        # a traceback out of a push that already succeeded.
        return None, f"`git remote` could not be run ({exc})"
    if res.returncode != 0:
        return None, (res.stderr.strip() or f"git exited {res.returncode}")
    urls = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.append(parts[1])
    if not urls:
        return False, ""
    return any(not _is_local_remote(u) for u in urls), ""


def query_open_mr_result(branch: str) -> MrLookup:
    """Open MR/PR for `branch` as an `MrLookup` — three states, not two (#948).

    Returns {source, iid, target, pipeline, pipeline_id, pipeline_url,
    merge_status} in `.mr` when a request was found. `pipeline` is the GitLab
    pipeline status when known, else None (gh list carries no cheap check
    state). `merge_status` is the server's view of mergeability
    ('can_be_merged' / 'cannot_be_merged' / None). The extra fields ride the
    same call — no added round-trip — and are best-effort: absent on a glab
    version that doesn't emit them. Tries glab (GitLab) first, falls back to
    gh (GitHub).

    **An answer already given is never downgraded by the fallback.** glab
    saying "no MR" on a GitLab repo is a fact; `gh` failing a moment later
    because the repo is not on GitHub says nothing about it. Getting that
    backwards would decline on every push of every GitLab repo with `gh`
    installed — the loud bug traded for the quiet one, in the direction that
    makes the disclosure worthless.
    """
    if not branch or branch == "HEAD":
        # A detached HEAD has no branch for a request to be open against. That
        # is a fact about the repository, not a lookup that failed.
        return MrLookup(None)
    could_host, repo_why = _remotes_could_host_a_request()
    if could_host is False:
        # git answered and no remote names a host. There is no tracker here, so
        # there is no open request — established locally, without needing a CLI
        # to survive long enough to say it. `None` (git itself did not answer)
        # deliberately falls through to the probes rather than claiming this.
        return MrLookup(None)
    probes = []
    if shutil.which("glab"):
        # No `--state opened`: glab has no such flag (1.86 exits 1 with
        # `Unknown flag: --state.`) and open is `mr list`'s default anyway —
        # `--closed` is the opt-out. This arm therefore failed at argument
        # parsing on every call, and the swallowed exit code meant nobody
        # found out for as long as the fallback to `gh` also said nothing
        # (#948). Found by the disclosure above, on its first run.
        probes.append((
            ["glab", "mr", "list", "--source-branch", branch,
             "--output", "json"], _glab_fields))
    if shutil.which("gh"):
        probes.append((
            ["gh", "pr", "list", "--head", branch, "--state", "open",
             "--json", "number,baseRefName,mergeable", "--limit", "1"],
            _gh_fields))
    if not probes:
        why = ("neither `glab` nor `gh` is installed, so no tracker can be "
               "read from here")
        return MrLookup(None, f"{repo_why}; {why}" if repo_why else why)
    answered = False
    # A git that did not answer is carried, not dropped: it is the first thing
    # that failed and the most likely thing to explain the rest, and naming
    # only the CLI would send the reader to fix the wrong tool. It does not
    # outrank a CLI that *did* answer — `answered` still wins below.
    reasons: list = [repo_why] if repo_why else []
    for argv, parse in probes:
        mr, state, why = _probe_open_request(argv, parse)
        if mr is not None:
            return MrLookup(mr)
        if state == "answered":
            answered = True
        elif state == "failed" and why:
            reasons.append(why)
    if answered or not reasons:
        # `not reasons` is the all-`n/a` case: every CLI present said this repo
        # is not on its host, so there is no tracker on which a request could
        # exist. That is an answer, and the same one status.py gives it.
        return MrLookup(None)
    return MrLookup(None, "; ".join(reasons))


def query_open_mr(branch: str) -> Optional[dict]:
    """The request itself, discarding why it might be unknown.

    Kept for `git-commit`'s post-commit hint, which appends `(!42)` to a line
    when there is a request and says nothing when there is not — a caller with
    no third thing to render. Anything that shows the reader a verdict should
    call `query_open_mr_result` and disclose the unknown.
    """
    return query_open_mr_result(branch).mr
