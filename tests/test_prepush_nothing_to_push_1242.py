"""Zero ref lines from git means nothing to push, not "I could not read" (#1242).

`.githooks/pre-push` reads one line per ref off stdin and, since #894, runs the
suite only when the destination is `master`/`main`. Its third state — no lines
at all — was written for "some callers invoke the hook with stdin closed" and
resolves to *run the suite*, on the reasoning that being wrong that way costs
three minutes and the other way costs master.

That fallback fires on a case it was not written for and gets it backwards.
**`git push` with nothing to update still invokes the hook, and legitimately
sends zero ref lines.** Measured 2026-08-12 in a disposable clone, with the
hook's branches marked: a second `git-push` on an already-pushed feature branch
reached `could not read the push refs from stdin` and went on to the full
~296s suite — to decide whether to permit a push that transfers nothing. Under
`_PUSH_TIMEOUT = 300` in `presets/git/push.py` that is the coin flip #1242 was
filed about, and it is this repo's own defect class: an absence produced by the
tool (git had no refs to send) read as an absence in the world (the refs could
not be read).

The two are distinguishable, and not by counting lines. **Git always invokes
`pre-push` with two arguments** — the remote name and its URL. So:

* two args and no ref lines — git ran us and there is nothing to update. There
  is nothing to gate. Skip, and say so.
* no args and no ref lines — something that is not git ran the hook, and the
  question of what is being pushed really is unanswered.

Three states, and the middle one stops being answered by the wrong arm.

#1802 changed what the third one resolves to. It used to run the suite, on
"being wrong that way costs three minutes and the other way costs master".
That reasoning does not survive being looked at: the arm fires precisely when
the destination is **unknown**, and the overwhelming majority of pushes in this
repo are to feature branches, which this hook has deliberately not gated since
#893. So it paid ~5 minutes to gate a push it had no reason to believe was
going anywhere that matters, on one platform of CI's three, in a suite that
rewrites the index of the live worktree it runs in.

It now skips and says which of the three states it is in. What did *not*
change is the `master`/`main` arm — measured 2026-08-20 over the last 400
commits on `origin/master`, 30 arrived by direct push rather than by squash
merge (committer `GitHub <noreply@github.com>` marks the merge path), 15 of
them touching `.py` or `presets/`, the most recent two days old. That arm is
live, not vestigial, and is the only local gate on the one path that never
goes through PR review.

The controls below are what keep this file honest about a deletion: every
"does not run" assertion sits in the same fixture as a "still runs" one, and
`ran_the_suite` reads a stub interpreter's log rather than the hook's prose,
so a hook that failed to execute at all fails the positive tests instead of
passing the negative ones.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HOOK = REPO / ".githooks" / "pre-push"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason=".githooks/pre-push is a bash script"
)

# Logs every invocation, so "did the suite run" is answered by what the
# interpreter was asked to do, never by parsing the hook's prose.
_STUB = """#!/bin/bash
echo "$*" >> "$STUB_LOG"
exit 0
"""

# What git actually passes: `pre-push <remote-name> <remote-url>`.
GIT_ARGV = ("origin", "https://example.invalid/repo.git")

MASTER_REF = "refs/heads/master abc123 refs/heads/master def456\n"
FEATURE_REF = "refs/heads/fix/1 abc123 refs/heads/fix/1 def456\n"


class _Box:
    """A git repo, a stub interpreter, and a way to feed the hook stdin."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.log = tmp_path / "invocations.log"
        self.log.write_text("", encoding="utf-8")
        git = shutil.which("git")
        assert git is not None, "git is required to run the hook at all"
        # A shim rather than a symlink. The hook only needs *a* `git` on this
        # PATH, and a symlink would add a call site to the register in
        # tests/test_symlink_gating_register_1232.py — a capability this file
        # does not otherwise need, gated for a platform it already skips.
        shim = self.bin / "git"
        shim.write_text(
            '#!/bin/bash\nexec ' + shlex.quote(git) + ' "$@"\n',
            encoding="utf-8")
        shim.chmod(0o755)
        python = self.bin / "python3.13"
        python.write_text(_STUB, encoding="utf-8")
        python.chmod(0o755)
        self.python = python
        subprocess.run(["git", "init", "-q", str(self.work)], check=True,
                       capture_output=True)

    def run(self, stdin: str = "",
            argv: tuple[str, ...] = GIT_ARGV,
            prepush_full: bool = False) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.work),
            "STUB_LOG": str(self.log),
            # Named explicitly: the interpreter ladder is #572's subject, not
            # this file's, and resolving it here would couple the two.
            "PYTHON": str(self.python),
        }
        if prepush_full:
            env["PREPUSH_FULL"] = "1"
        return subprocess.run(
            [BASH, str(HOOK), *argv], cwd=str(self.work), env=env,
            input=stdin, capture_output=True, encoding="utf-8",
            errors="replace", timeout=60,
        )

    def ran_the_suite(self) -> bool:
        return "-m pytest" in self.log.read_text(encoding="utf-8")


@pytest.fixture
def box(tmp_path: Path) -> _Box:
    return _Box(tmp_path)


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------

def test_git_with_nothing_to_push_does_not_run_the_suite(box: _Box) -> None:
    """The whole issue. An up-to-date `git push` still runs the hook and sends
    no ref lines; ~296s of pytest to authorise moving nothing."""
    r = box.run(stdin="", argv=GIT_ARGV)
    assert not box.ran_the_suite(), (
        "the suite ran for a push that updates no ref: " + r.stdout + r.stderr)
    assert r.returncode == 0


def test_nothing_to_push_is_disclosed_not_silent(box: _Box) -> None:
    """A gate that stops gating has to say so — same rule as #894's skip."""
    r = box.run(stdin="", argv=GIT_ARGV)
    out = r.stdout + r.stderr
    assert "pre-push" in out
    assert "no refs" in out or "nothing to push" in out, out


# ---------------------------------------------------------------------------
# what must not weaken
# ---------------------------------------------------------------------------

def test_no_argv_and_no_refs_no_longer_runs_the_suite(box: _Box) -> None:
    """#1802: the not-git fallback is gone. An unknown destination is not a
    reason to spend the suite — it is a reason to say the destination is
    unknown. CI gates the commit on all three platforms either way."""
    r = box.run(stdin="", argv=())
    assert not box.ran_the_suite(), (
        "the suite ran for a caller whose destination was never established: "
        + r.stdout + r.stderr)
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_argv_and_no_refs_says_the_destination_was_unknown(
        box: _Box) -> None:
    """The skip is disclosed, and it must not render as either of the other
    two states. "could not read the refs" and "there are no refs" are the
    conflation #1242 split apart; #1802 changed what one of them *does*, not
    whether they are still told apart."""
    r = box.run(stdin="", argv=())
    out = r.stdout + r.stderr
    assert "could not read" in out, out
    assert "NOT run" in out, out
    assert "nothing to push" not in out, out
    assert "feature branch" not in out, out


def test_prepush_full_still_forces_the_suite_with_no_argv(box: _Box) -> None:
    """The override keeps meaning "run it anyway" in every state, including
    the one #1802 stopped running by default. This is also the positive
    control for the two tests above: same fixture, same argv, and the suite
    *does* run — so a hook that failed to execute, or a stub log that was
    never written, fails here instead of passing there."""
    r = box.run(stdin="", argv=(), prepush_full=True)
    assert box.ran_the_suite(), r.stdout + r.stderr


def test_a_push_to_master_still_runs_the_suite(box: _Box) -> None:
    r = box.run(stdin=MASTER_REF)
    assert box.ran_the_suite(), r.stdout + r.stderr


def test_a_feature_branch_still_skips(box: _Box) -> None:
    r = box.run(stdin=FEATURE_REF)
    assert not box.ran_the_suite(), r.stdout + r.stderr
    assert r.returncode == 0


def test_prepush_full_still_forces_the_suite_on_a_feature_branch(
        box: _Box) -> None:
    r = box.run(stdin=FEATURE_REF, prepush_full=True)
    assert box.ran_the_suite(), r.stdout + r.stderr


def test_prepush_full_forces_the_suite_even_with_nothing_to_push(
        box: _Box) -> None:
    """The override keeps meaning "run it anyway" in every state, and the
    banner has to say which state it overrode — a message about refs that
    could not be read, printed for refs that were read and were empty, is the
    same conflation one layer up."""
    r = box.run(stdin="", argv=GIT_ARGV, prepush_full=True)
    out = r.stdout + r.stderr
    assert box.ran_the_suite(), out
    assert "could not read" not in out, out


def test_master_among_several_refs_still_runs_the_suite(box: _Box) -> None:
    """Zero lines is the new case; more than one line was always handled."""
    r = box.run(stdin=FEATURE_REF + MASTER_REF)
    assert box.ran_the_suite(), r.stdout + r.stderr
