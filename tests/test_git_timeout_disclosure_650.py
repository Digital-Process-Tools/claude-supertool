"""#650 — a git call that did not answer must not be spoken as an answer.

Filed as "two tests flake under xdist load". The load hypothesis did not
survive measurement: on the machine that reported it, the worst git latency
observed over 300 samples while the real 11-worker suite ran was 0.30s, and
0.76s over 120 samples under 96 CPU burners on 11 cores — 6x inside the 5s
budget, at a load no runner will see. So "the machine was slow, raise the
budget" would be a fix aimed at an unmeasured cause, and the cause of the one
stall that was actually observed is still unknown.

What *is* first-hand, from the traceback in the issue, is what this code does
when a git call does not return inside its budget, and that is a defect
regardless of why it stalled:

  * `presets/git/status.py::_git` let `TimeoutExpired` escape. One stalled
    `rev-list` killed the entire report with a stack trace — the ahead/behind,
    the commits, the working tree, the PR section, all lost to a courtesy line
    about divergence from master.
  * `supertool._current_branch()` folded a timeout into `""`, the same value
    that means "there is no branch here". `_branch_line()` then printed
    nothing, so a mutating op's receipt silently dropped the branch on exactly
    the run where the caller was least sure what state the repo was in.

Both are the house defect (docs/validators.md, "Declining instead of
guessing"): three states, not two — answered, answered-with-a-finding, and
*could not answer*. The tests below drive real `git` binaries to real
timeouts through a PATH shim; nothing here mocks `_git` (#649).
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).parent.parent
_STATUS_PATH = _ROOT / "presets" / "git" / "status.py"
_spec = importlib.util.spec_from_file_location("git_status_650", _STATUS_PATH)
assert _spec is not None and _spec.loader is not None
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)

_CONFLICTS_PATH = _ROOT / "presets" / "git" / "conflicts.py"
_c_spec = importlib.util.spec_from_file_location("git_conflicts_650", _CONFLICTS_PATH)
assert _c_spec is not None and _c_spec.loader is not None
conflicts = importlib.util.module_from_spec(_c_spec)
_c_spec.loader.exec_module(conflicts)


def _load_preset(name: str):
    path = _ROOT / "presets" / "git" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"git_{name}_650", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


resolve = _load_preset("resolve")
merge = _load_preset("merge")

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")


# ---------------------------------------------------------------------------
# A real git that really hangs, for one subcommand only
# ---------------------------------------------------------------------------

def _slow_git_path(tmp_path: Path, subcommand: str) -> str:
    """PATH containing a `git` that sleeps forever on `subcommand`.

    Everything else execs the real binary, so the repo under test is built and
    read by real git and only the one call under examination stalls. The
    returned PATH holds nothing else — `gh` and `glab` are unfindable, which
    is how the PR section stays off the network.
    """
    real = _require_git()
    # Absolute, because the PATH handed to the shim contains only the shim:
    # a bare `sleep` is not found there, the script falls through to real git,
    # and the test passes having stalled nothing at all. It did, once.
    sleep = shutil.which("sleep")
    assert sleep, "sleep must be on PATH for this suite"
    bindir = tmp_path / "shimbin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "{subcommand}" ]; then {sleep} 300; fi\n'
        f'exec {real} "$@"\n'
    )
    shim.chmod(0o755)
    return str(bindir)


def _git_only_path(tmp_path: Path) -> str:
    """A PATH holding a real, unshimmed `git` — and nothing else at all.

    `os.path.dirname(shutil.which("git"))` was the obvious way to write this
    and is not the same thing: it hands over whatever else happens to live in
    git's directory, which on a GitHub runner is `gh`. An unauthenticated `gh`
    then declines the MR lookup, `git-status` discloses it in the INCOMPLETE
    footer — correctly — and a test asserting the footer is absent fails on
    a machine, not on a defect (#705).

    So the premise is built rather than assumed. `gh` and `glab` being
    unfindable is the documented middle state: a CLI that is not installed is
    silent, because nothing on that machine was ever going to answer.
    """
    bindir = tmp_path / "gitonlybin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "git"
    shim.symlink_to(_require_git())
    return str(bindir)


def _require_git() -> str:
    real = shutil.which("git")
    assert real, "git must be on PATH for this suite"
    return real


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)
    return r.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "fix/650")
    (repo / "g").write_text("y\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-m", "the work")
    return repo


def _run_status(repo: Path, monkeypatch) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status.sys, "argv", ["status.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert status.main() == 0
    return buf.getvalue()


# ---------------------------------------------------------------------------
# git-status: a stalled call costs its own line, not the report
# ---------------------------------------------------------------------------

def test_a_stalled_git_call_does_not_kill_the_whole_report(
    tmp_path: Path, monkeypatch
) -> None:
    """The defect in the issue's traceback: one slow `rev-list`, no report."""
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "rev-list"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")

    started = time.monotonic()
    out = _run_status(repo, monkeypatch)
    elapsed = time.monotonic() - started

    assert "# git-status" in out
    assert "Branch: fix/650" in out
    assert "## Last 5 commits" in out
    assert "the work" in out
    # The stall was bounded by the budget, not waited out. `sleep 300` means a
    # budget that did not apply cannot finish inside this assertion at all.
    assert elapsed < 60, elapsed


def test_the_report_says_which_git_call_went_unanswered(
    tmp_path: Path, monkeypatch
) -> None:
    """Missing sections must not read as 'there was nothing to report'.

    The divergence-from-master line is absent either way. The reader can only
    tell the two apart if the report says so, names the call, and states the
    budget it was cut off at.
    """
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "rev-list"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER in out
    note = next(l for l in out.splitlines() if status.INCOMPLETE_MARKER in l)
    # The literal word, not just the constant: a marker renamed to something
    # that does not say "incomplete" would satisfy the constant and nothing else.
    assert "INCOMPLETE" in note, note
    assert "rev-list" in note, note
    assert "1s" in note, note


def test_the_incomplete_note_is_absent_when_every_call_answered(
    tmp_path: Path, monkeypatch
) -> None:
    """A permanent disclaimer discloses nothing. Real git, no shim.

    The PATH is built to hold git alone rather than taken as git's directory:
    on a GitHub runner that directory also holds `gh`, which is unauthenticated
    inside a workflow and declines. That decline is a true one and is disclosed
    on purpose — an unauthenticated CLI cannot tell you whether a branch has a
    PR, and unlike a missing binary it can be resolved (`gh auth login`), so it
    is not the never-resolving noise the doctrine keeps silent. Silencing it to
    keep this fixture quiet would re-open #705 exactly where it was closed.
    """
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _git_only_path(tmp_path))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER not in out
    assert "vs master: 1 ahead" in out


def test_a_stalled_call_is_never_rendered_as_a_git_success(
    tmp_path: Path, monkeypatch
) -> None:
    """The cheap fix — swallow the timeout, return an empty CompletedProcess —
    would make `master...HEAD` render as `0 ahead ... branch has no own
    commits!`, which is a false alarm about the branch rather than a true one
    about git."""
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "rev-list"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    out = _run_status(repo, monkeypatch)

    assert "no own commits" not in out
    assert "vs master:" not in out


def test_a_stalled_call_carries_a_failure_returncode(tmp_path: Path, monkeypatch) -> None:
    """The mechanism the docstring promises, pinned as behaviour.

    Returning `returncode=0` for a call that never answered makes the timeout
    indistinguishable from a git that succeeded and printed nothing. Two of the
    call sites happen to survive that anyway, because they also check the shape
    of stdout — but that is luck at the call site, not a property of `_git`, and
    it does not hold for the ones that only ask `returncode == 0`.
    """
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "rev-list"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    monkeypatch.chdir(tmp_path)
    r = status._git(["rev-list", "--count", "HEAD"])
    assert r.returncode == status.TIMEOUT_RC
    assert r.returncode != 0, "a call that never answered is not a success"


def test_a_stalled_branch_name_reads_unknown_not_blank(
    tmp_path: Path, monkeypatch
) -> None:
    """`Branch: ` with nothing after it is the false answer in miniature.

    `rev-parse --abbrev-ref HEAD` has an `else "?"` leg for exactly this, and a
    timeout that reports success walks straight past it. The report then states
    a branch — the empty one — on the run where it knows least. `?` is the
    honest reading: git was asked and did not say.
    """
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "rev-parse"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    out = _run_status(repo, monkeypatch)

    branch_line = next(l for l in out.splitlines() if l.startswith("Branch:"))
    assert branch_line.strip() != "Branch:", "a stall rendered as an empty branch name"
    assert "?" in branch_line, branch_line
    assert status.INCOMPLETE_MARKER in out


# ---------------------------------------------------------------------------
# The budget itself — configurable per environment, unchanged as shipped
# ---------------------------------------------------------------------------

def test_the_git_budget_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "30")
    assert status._git_timeout() == 30
    assert supertool._git_timeout() == 30


def test_an_explicit_budget_is_honoured(monkeypatch) -> None:
    """Two call sites ask for 3s on purpose — a cheap local lookup should not
    hold the report for the full budget. The parameter has to reach `run`."""
    monkeypatch.delenv("SUPERTOOL_GIT_TIMEOUT", raising=False)
    assert status._git_timeout(3) == 3
    assert status._git_timeout() == 5


def test_a_bad_budget_env_falls_back_to_the_default(monkeypatch) -> None:
    for bad in ("not-a-number", "", "0", "-3"):
        monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", bad)
        assert status._git_timeout() == status._GIT_TIMEOUT_DEFAULT, bad
        assert supertool._git_timeout() == supertool._GIT_TIMEOUT_DEFAULT, bad


def test_the_suite_budget_does_not_move_the_product_default(monkeypatch) -> None:
    """conftest raises SUPERTOOL_GIT_TIMEOUT for this suite because a loaded
    runner occasionally needs the room (#553 made the same call for the lint
    budget). That is a fact about the runner. What supertool ships with is
    unchanged, and reading the suite's value as the product's would be reading
    a workaround as a decision.
    """
    monkeypatch.delenv("SUPERTOOL_GIT_TIMEOUT", raising=False)
    assert status._GIT_TIMEOUT_DEFAULT == 5
    assert status._git_timeout() == 5
    assert supertool._GIT_TIMEOUT_DEFAULT == 5
    assert supertool._git_timeout() == 5


# ---------------------------------------------------------------------------
# _current_branch: absent, or unanswered — never the same value
# ---------------------------------------------------------------------------

def _branch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "b"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "checkout", "-q", "-b", "my-feature")
    return repo


def test_a_healthy_read_reports_no_reason(tmp_path: Path, monkeypatch) -> None:
    """Kills the mutant that declines unconditionally — which would otherwise
    make every branch assertion in the suite skip itself into a green."""
    repo = _branch_repo(tmp_path)
    monkeypatch.chdir(repo)
    supertool._BRANCH_CACHE[0] = None
    assert supertool._branch_reading() == ("my-feature", "")
    assert supertool._branch_line() == "[branch: my-feature]\n"


def test_outside_a_repo_the_branch_is_absent_not_declined(
    tmp_path: Path, monkeypatch
) -> None:
    """The one honest silence: git answered, and the answer is 'no branch'."""
    monkeypatch.chdir(tmp_path)
    supertool._BRANCH_CACHE[0] = None
    branch, why = supertool._branch_reading()
    assert branch == ""
    assert why == ""
    assert supertool._branch_line() == ""


def test_no_git_at_all_is_an_absence_not_a_decline(
    tmp_path: Path, monkeypatch
) -> None:
    """The one silence worth keeping (docs/validators.md): nothing on this
    machine was ever going to name a branch. Declining on every receipt of
    every op would be noise that never resolves."""
    empty = tmp_path / "nobin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(tmp_path)
    supertool._BRANCH_CACHE[0] = None
    assert supertool._branch_reading() == ("", "")
    assert supertool._branch_line() == ""


def test_a_stalled_branch_read_declines_instead_of_reading_as_no_branch(
    tmp_path: Path, monkeypatch
) -> None:
    """The #650 half Florian grouped in: `''` from a failed call is byte
    identical to `''` from a repo with no branch, and the receipt renders the
    second one as silence."""
    repo = _branch_repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "symbolic-ref"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    monkeypatch.chdir(repo)
    supertool._BRANCH_CACHE[0] = None

    branch, why = supertool._branch_reading()
    assert branch == ""
    assert why, "a timed-out read must state why it has no branch"
    assert "symbolic-ref" in why, why
    assert "1s" in why, why

    line = supertool._branch_line()
    assert line != "", "silence here is the defect — it reads as 'no branch'"
    assert "UNKNOWN" in line, line
    assert "my-feature" not in line, line


def test_the_declined_reading_is_cached_like_any_other(
    tmp_path: Path, monkeypatch
) -> None:
    """A batch of edits must not pay a stalled subprocess per op — the timeout
    is the expensive case, so it is the one that most needs the cache."""
    repo = _branch_repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "symbolic-ref"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    monkeypatch.chdir(repo)
    supertool._BRANCH_CACHE[0] = None

    started = time.monotonic()
    for _ in range(5):
        supertool._branch_reading()
    assert time.monotonic() - started < 4, "the decline was re-read every call"


# ---------------------------------------------------------------------------
# git-conflicts: "I could not look" must never print as "there are none"
# ---------------------------------------------------------------------------

def _failing_git_path(tmp_path: Path, subcommand: str) -> str:
    """PATH containing a `git` that exits 1 on `subcommand`, real git otherwise.

    The hang shim above covers the timeout leg. This covers the other half of
    the same conflation, and the one that needs no load at all to happen: a
    git that answers, unsuccessfully. An index lock held by a concurrent
    process is the everyday cause.
    """
    real = shutil.which("git")
    assert real, "git must be on PATH for this suite"
    bindir = tmp_path / "failbin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "{subcommand}" ]; then echo "fatal: shim" >&2; exit 1; fi\n'
        f'exec {real} "$@"\n'
    )
    shim.chmod(0o755)
    return str(bindir)


def _conflicted_repo(tmp_path: Path) -> Path:
    """A repo genuinely stopped mid-merge with one unresolved file."""
    repo = tmp_path / "c"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f").write_text("base\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "theirs")
    (repo / "f").write_text("theirs\n")
    _git(repo, "commit", "-am", "theirs")
    _git(repo, "checkout", "master")
    (repo / "f").write_text("ours\n")
    _git(repo, "commit", "-am", "ours")
    subprocess.run(["git", "-C", str(repo), "merge", "theirs"],
                   capture_output=True, text=True)
    return repo


def _run_conflicts(repo: Path, monkeypatch) -> tuple[str, int]:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(conflicts.sys, "argv", ["conflicts.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = conflicts.main()
    return buf.getvalue(), rc


def test_a_failed_conflict_list_does_not_read_as_no_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    """The most dangerous instance of the house defect in the tool.

    `_list_conflicts` returned `[]` for both "git says the tree is clean" and
    "git did not answer", and `main` renders `[]` as `No conflicted files.`
    You reach for `git-conflicts` precisely when you are stopped mid-merge, so
    the sentence it prints on a failed lookup is an invitation to `git commit`
    over live `<<<<<<<` markers. The repo here really is mid-merge with a real
    unresolved file — only the lookup fails.
    """
    repo = _conflicted_repo(tmp_path)
    monkeypatch.setenv("PATH", _failing_git_path(tmp_path, "diff"))

    out, rc = _run_conflicts(repo, monkeypatch)

    assert "No conflicted files." not in out, out
    assert "UNKNOWN" in out, out
    assert rc != 0, "a report that could not be produced must not exit clean"


def test_a_stalled_conflict_list_declines_instead_of_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    """The timeout leg of the same call — a stack trace is not a conflict list."""
    repo = _conflicted_repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "diff"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")

    started = time.monotonic()
    out, rc = _run_conflicts(repo, monkeypatch)
    elapsed = time.monotonic() - started

    assert "No conflicted files." not in out, out
    assert "UNKNOWN" in out, out
    assert rc != 0
    assert elapsed < 60, elapsed


def test_conflicts_still_reports_a_genuinely_clean_tree(
    tmp_path: Path, monkeypatch
) -> None:
    """Kills the mutant that declines unconditionally.

    Real git, no shim, no merge in progress: the honest `No conflicted files.`
    must survive, or the fix above would have bought silence for noise.
    """
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _git_only_path(tmp_path))

    out, rc = _run_conflicts(repo, monkeypatch)

    assert "No conflicted files." in out, out
    assert "UNKNOWN" not in out, out
    assert rc == 0


def test_every_conflict_list_in_the_tool_declines(tmp_path, monkeypatch) -> None:
    """`_list_conflicts` exists in triplicate — all three must say "unknown".

    `git-resolve` is the one that mattered enough to fix alongside
    `git-conflicts`: its closing `Remaining: N` line is followed by
    `Next: git-commit ...` when N is 0, so a failed lookup there ends in a
    commit over live markers. `git-merge`'s copy is fixed for consistency
    rather than danger — by the time it runs, the merge has already failed.
    """
    repo = _conflicted_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", _failing_git_path(tmp_path, "diff"))

    for mod in (conflicts, resolve, merge):
        paths, why = mod._list_conflicts()
        assert paths == [], mod.__name__
        assert why, f"{mod.__name__} folded a failed lookup into 'no conflicts'"


def test_a_conflict_list_that_answers_reports_no_reason(tmp_path, monkeypatch) -> None:
    """Kills the mutant that declines unconditionally in all three copies."""
    repo = _conflicted_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", _git_only_path(tmp_path))

    for mod in (conflicts, resolve, merge):
        paths, why = mod._list_conflicts()
        assert why == "", mod.__name__
        assert paths == ["f"], mod.__name__


def test_current_branch_still_returns_a_plain_string(
    tmp_path: Path, monkeypatch
) -> None:
    """The receipt is the only place the reason belongs. Every other caller
    keeps the old contract: a branch name, or '' when there is none to name."""
    repo = _branch_repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "symbolic-ref"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    monkeypatch.chdir(repo)
    supertool._BRANCH_CACHE[0] = None
    assert supertool._current_branch() == ""
