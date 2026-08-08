"""#1028: `ahead 5, behind 1` after a clean rebase reads as lost work.

Straight after a successful `git rebase origin/master`, `git-status` printed

    Branch: fix/x (ahead 5, behind 1)

which is arithmetically true and semantically the opposite of what happened.
`ahead N, behind M` is the render for "the two histories have genuinely
diverged and you must reconcile them"; here nothing diverged and nothing is
lost — the remote simply holds the pre-rebase originals of commits that are
already in this branch, and the move is a force-with-lease push.

The two are distinguishable from data git already has. `git rev-list --count
--right-only --cherry-pick HEAD...@{upstream}` counts upstream commits with
**no patch-equivalent** on this side:

    pure rebase (ahead 4, behind 3)                 -> 0
    rebase + one genuine upstream commit (4, 4)     -> 1

Measured on a real repository, not asserted from the manual.

Suppressing the count instead would trade a confusing render for a quiet one,
so the count stays and gains a line that says which of the two it is — with a
third state for the run where the extra call did not answer, in the vocabulary
#1034 established for this file (`UNKNOWN — ... did not answer`).

Part two of the issue is the ordering complaint: the branch inventory and the
commit log sit above the working tree, which is what makes people pipe this op
through `tail` — a documented mistake here, because these ops put the meaning
at the top. `git-status:brief` removes the incentive. The default is
deliberately unchanged; the issue says so itself.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "git_status_1028", _ROOT / "presets" / "git" / "status.py")
assert _spec is not None and _spec.loader is not None
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)


def _ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=0,
                                       stdout=stdout, stderr="")


def _dead(rc: int, stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=rc,
                                       stdout="", stderr=stderr)


_OTHERS = "".join(
    "other-%d\t[ahead %d, behind 1]\n" % (i, i) for i in range(1, 13)
)


def _render(monkeypatch, *, left_right="5\t1", cherry=_ok("0\n"),
            argv=("status.py",)) -> str:
    """`git-status` against a scripted git. Only the subject varies."""

    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "rev-list":
            if "--cherry-pick" in args:
                return cherry
            if args[-1].endswith("@{upstream}"):
                return _ok(left_right + "\n")
            return _ok("0\t3\n")
        if head == "branch":
            return _ok("* fix/1028 abc1234 [origin/fix/1028] subject\n")
        if head == "rev-parse":
            if "--abbrev-ref" in args:
                return _ok("fix/1028\n")
            return _dead(1, "")
        if head == "log":
            return _ok("abc1234 2026-08-08 t | subject\n")
        if head == "for-each-ref":
            return _ok(_OTHERS)
        if head == "status":
            return _ok(" M keep.txt\n")
        if head == "stash":
            return _ok("")
        return _dead(1, "")

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(status, "_hosted_request", lambda cmd: None)
    monkeypatch.setattr(sys, "argv", list(argv))
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


# --- part 1: telling a rebase apart from lost work ---


def test_the_count_is_still_printed(monkeypatch) -> None:
    """The control. Suppressing it would be the quiet bug for the loud one."""
    out = _render(monkeypatch)
    assert "ahead 5, behind 1" in out, out


def test_a_rebased_branch_says_nothing_is_lost(monkeypatch) -> None:
    out = _render(monkeypatch, cherry=_ok("0\n"))
    line = next((l for l in out.splitlines() if l.startswith("Diverged:")), "")
    assert line, (
        "`ahead 5, behind 1` stands alone, and its ordinary meaning is the "
        f"opposite of what happened:\n{out}"
    )
    assert "REBASED" in line, line
    assert "force-with-lease" in line.lower() or "force-with-lease" in out, out


def test_a_genuinely_diverged_branch_is_not_called_a_rebase(monkeypatch) -> None:
    """The discriminator has to be able to say no, or it says nothing."""
    out = _render(monkeypatch, cherry=_ok("1\n"))
    line = next((l for l in out.splitlines() if l.startswith("Diverged:")), "")
    assert line, out
    assert "REBASED" not in line, (
        "a real upstream commit that is not in this history was reported as a "
        f"replay of our own:\n{line}"
    )
    assert "force-with-lease" not in out.lower(), (
        "a force push here discards a commit nobody has:\n" + out
    )


def test_an_unanswered_cherry_check_is_unknown_not_a_verdict(monkeypatch) -> None:
    """Three states, and the same vocabulary the rest of this file uses."""
    out = _render(monkeypatch, cherry=_dead(status.TIMEOUT_RC, "timed out after 5s"))
    line = next((l for l in out.splitlines() if l.startswith("Diverged:")), "")
    assert line, out
    assert "UNKNOWN" in line, line
    assert "REBASED" not in line, line
    assert "ahead 5, behind 1" in out, "the count must survive the failed check"


def test_no_divergence_line_when_there_is_no_divergence(monkeypatch) -> None:
    """A line on every run is a line nobody reads on the run that needs it."""
    out = _render(monkeypatch, left_right="5\t0")
    assert "Diverged:" not in out, out
    out = _render(monkeypatch, left_right="0\t0")
    assert "Diverged:" not in out, out


def test_the_cherry_check_is_not_made_when_it_cannot_matter(monkeypatch) -> None:
    """No extra spawn on the overwhelmingly common non-diverged call."""
    seen = []

    def fake(args, timeout=None):
        seen.append(args)
        if args and args[0] == "rev-list":
            if args[-1].endswith("@{upstream}"):
                return _ok("0\t0\n")
            return _ok("0\t0\n")
        if args and args[0] == "branch":
            return _ok("* fix/1028 abc1234 [origin/fix/1028] s\n")
        if args and args[0] == "rev-parse" and "--abbrev-ref" in args:
            return _ok("fix/1028\n")
        return _ok("")

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(status, "_hosted_request", lambda cmd: None)
    monkeypatch.setattr(sys, "argv", ["status.py"])
    with redirect_stdout(io.StringIO()):
        status.main()
    assert not any("--cherry-pick" in a for a in seen), seen


# --- part 2: the ordering complaint, settled with a flag ---


def test_brief_drops_the_branch_inventory(monkeypatch) -> None:
    out = _render(monkeypatch, argv=("status.py", "brief"))
    assert "Other branches" not in out, (
        "the twelve lines the caller did not ask for are still above the "
        f"section they did:\n{out}"
    )
    assert "## Last 5 commits" not in out, out


def test_brief_keeps_what_the_op_is_called_for(monkeypatch) -> None:
    out = _render(monkeypatch, argv=("status.py", "brief"))
    assert "Branch: fix/1028 (ahead 5, behind 1)" in out, out
    assert "Diverged:" in out, out
    assert "## Working tree (1 changes)" in out, out
    assert "keep.txt" in out, out


def test_brief_does_not_hide_a_section_that_could_not_be_read(monkeypatch) -> None:
    """Brevity may drop what was answered; never what was not (#1034)."""

    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "status":
            return _dead(128, "fatal: unable to read index")
        if head == "rev-list":
            return _ok("0\t0\n")
        if head == "branch":
            return _ok("* fix/1028 abc1234 [origin/fix/1028] s\n")
        if head == "rev-parse" and "--abbrev-ref" in args:
            return _ok("fix/1028\n")
        return _ok("")

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(status, "_hosted_request", lambda cmd: None)
    monkeypatch.setattr(sys, "argv", ["status.py", "brief"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    out = buf.getvalue()
    assert "## Working tree: UNKNOWN" in out, out


def test_the_default_render_is_unchanged(monkeypatch) -> None:
    """The issue explicitly does not ask for a shorter default."""
    out = _render(monkeypatch)
    assert "## Other branches with unpushed/unpulled work" in out, out
    assert "## Last 5 commits" in out, out


def test_an_unrecognised_mode_is_reported_not_discarded(monkeypatch) -> None:
    """`git-status:breif` used to render the default and say nothing (#647)."""
    out = _render(monkeypatch, argv=("status.py", "breif"))
    assert "breif" in out, (
        "a mode this op cannot honour was silently dropped, so the caller "
        f"believes they got the render they asked for:\n{out}"
    )
