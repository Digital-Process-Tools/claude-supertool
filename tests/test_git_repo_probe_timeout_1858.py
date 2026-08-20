"""#1858 — a git call that did not answer is not a git repository that is absent.

Seven ops open with the same question — *am I inside a git repository?* — asked as
`git rev-parse --git-dir` (`git branch -vv` in `git-status`, which runs it for
its return code only). Every one of them read a non-zero return as **no**, and
`_git` hands back `TIMEOUT_RC` (124) for a call that never answered. So a stalled
probe printed:

    ERROR: not inside a git repository.

about a repository that is mid-merge with live conflict markers on disk. That is
this repository's named defect in its sharpest form: not an absence rendered as
an absence, but a positive false claim about the world. A caller told *not inside
a git repository* has no reason to retry; a caller told *the call did not answer*
does.

**The population, and how it was derived.** `presets/git/` holds 187 reads of a
`returncode`. Nearly all of them are honest under a timeout: they skip an
optional section, or relay `exit N: <stderr>` verbatim, and a timeout's stderr
says `timed out after Ns`. The sub-population fixed here is the one whose
non-zero arm makes a **positive claim about the repository** that a timeout does
not license — the repo probe in `conflicts.py`, `resolve.py`, `push.py`,
`diff.py`, `commit.py`, `status.py` and `blame.py`, plus
`conflicts.py::_detect_state`, whose empty return prints as `State: no
merge/rebase/cherry-pick in progress` over a repository that is mid-merge.

`blame.py` is here because the sweep was re-run rather than trusted: the first
pass named six sites, and grepping `presets/` for the printed sentence found
ten. Three of the four extras (`trail.py`, `investigate.py`, `checkout.py`) are
honest — they gate on git's own stderr saying `not a git repository`, which a
timeout's `timed out after Ns` does not — and the fourth was the same defect,
under a comment that named `TIMEOUT_RC` two lines above the arm ignoring it.

**The control that matters, and it is half of every test here.** A fix that
treats every non-zero return as *could not tell* trades the loud bug for the
quiet one. So every timeout case below is paired with a genuine-failure case in
the same fixture: a directory that really is not a repository must still get
`ERROR: not inside a git repository.` and a non-zero exit. Both halves run
against real `git` through a PATH shim; nothing here mocks the return code into
existence.

**The harness has its own third state.** `_run` wraps the module's `_git` and
records every `TIMEOUT_RC` it sees with its own eyes. A stall case that recorded
none means the shim did not fire, which is a vacuous test rather than a passing
one, so it fails loudly. A control case that recorded one is `contended-git
(#1845)`: the machine could not meet the premise, so nothing is claimed.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _changelog_findable import assert_change_is_findable  # noqa: E402
from _gitshim import dispatch_on_subcommand  # noqa: E402

REPO = Path(__file__).parent.parent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")

#: The false sentence. Absent from a stalled run, present in a real refusal.
FALSE_CLAIM = "not inside a git repository"
#: The third state. Deliberately does not contain FALSE_CLAIM as a substring,
#: so the two assertions below cannot both be satisfied by one line.
THIRD_STATE = "could not tell whether this is a git repository"


def _load(name: str):
    path = REPO / "presets" / "git" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"git_{name}_1858", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _require_git() -> str:
    real = shutil.which("git")
    assert real, "git must be on PATH for this suite"
    return real


def _slow_git_path(tmp_path: Path, subcommand: str) -> str:
    """PATH holding a `git` that sleeps forever on SUBCOMMAND and nothing else.

    Absolute `sleep`: the shim's own PATH holds only the shim, so a bare `sleep`
    is not found there, the script falls through to real git, and the test
    passes having stalled nothing at all.
    """
    real = _require_git()
    sleep = shutil.which("sleep")
    assert sleep, "sleep must be on PATH for this suite"
    bindir = tmp_path / "shimbin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "git"
    shim.write_text("#!/bin/sh" + chr(10)
                    + dispatch_on_subcommand(subcommand, f"{sleep} 300", real))
    shim.chmod(0o755)
    return str(bindir)


def _slow_git_path_after_first_call(tmp_path: Path, subcommand: str) -> str:
    """PATH holding a `git` that answers SUBCOMMAND once, then hangs on every later one.

    Two probes in `conflicts.py` issue the identical `rev-parse --git-dir`, and
    the second is unreachable in a fixture that stalls the first. Remembering
    that one has already gone through is the only way to put a real timeout on
    the second without mocking a return code into existence.

    **Builtins only, and that is not a style choice.** The PATH handed to this
    shim contains the shim and nothing else, so `cat` is not findable — the
    first cut counted with `n=$(cat file || echo 0)`, which therefore read 0
    forever, never reached its threshold, and passed the whole report through
    real git having stalled nothing at all. `test -f` and `>` are builtins; the
    absolute `sleep` is resolved here, in Python, for the same reason.
    """
    real = _require_git()
    sleep = shutil.which("sleep")
    assert sleep, "sleep must be on PATH for this suite"
    bindir = tmp_path / "afterfirstbin"
    bindir.mkdir(exist_ok=True)
    seen = bindir / "seen"
    action = f'if [ -f {seen} ]; then exec {sleep} 300; fi; : > {seen}'
    shim = bindir / "git"
    shim.write_text("#!/bin/sh" + chr(10)
                    + dispatch_on_subcommand(subcommand, action, real))
    shim.chmod(0o755)
    return str(bindir)


def _git_only_path(tmp_path: Path) -> str:
    """A real, unshimmed `git` — and nothing else, so no `gh` reaches a network."""
    bindir = tmp_path / "gitonlybin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "git"
    if not shim.exists():
        shim.symlink_to(_require_git())
    return str(bindir)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo.parent, "init", "-q", "-b", "master", str(repo))
    for k, v in (("user.email", "t@test.com"), ("user.name", "Test"),
                 ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    (repo / "f").write_text("base" + chr(10), encoding="utf-8")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "base")
    return repo


def _conflicted_repo(tmp_path: Path) -> Path:
    """A repo genuinely stopped mid-merge with one unresolved file."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "theirs")
    (repo / "f").write_text("theirs" + chr(10), encoding="utf-8")
    _git(repo, "commit", "-qam", "theirs")
    _git(repo, "checkout", "-q", "master")
    (repo / "f").write_text("ours" + chr(10), encoding="utf-8")
    _git(repo, "commit", "-qam", "ours")
    subprocess.run(["git", "-C", str(repo), "merge", "theirs"],
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
    return repo


def _not_a_repo(tmp_path: Path) -> Path:
    """A directory with no repository at or above it, established rather than assumed.

    Holds an `f`, because `blame.py` checks that its PATH is a file BEFORE it
    probes for a repository. Without the file it would refuse with `file not
    found` and never reach the arm this control exists to pin — a green that
    proves nothing, in the exact shape this repository is named for.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x" + chr(10), encoding="utf-8")
    probe = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(outside),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if probe.returncode == 0:
        pytest.skip(f"tmp_path is inside a repository ({probe.stdout.strip()})")
    return outside


def _run(mod, argv, cwd: Path, monkeypatch, *, expect_stall: bool) -> tuple[str, int]:
    """Call MOD.main() in CWD, and account for every git call that stalled.

    The accounting is the point. A stall case whose shim silently failed to fire
    asserts nothing at all — real git answers, the product takes the ordinary
    path, and the absent false claim is absent for the wrong reason. So the
    stalls are seen here rather than inferred from what the product printed.
    """
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(mod.sys, "argv", argv)

    stalls: list[str] = []

    def recording(real):
        def recording_git(args, timeout=None):
            res = real(args, timeout)
            if res.returncode == 124:
                stalls.append("git " + " ".join(args))
            return res
        return recording_git

    # ONE seam, and that it is enough is a claim about the product. `probe_repo`
    # lives in `_git_common`; the first cut of the fix let it resolve `_git` in
    # that module's own globals, and this wrapper then saw nothing at all — five
    # probes stalled correctly and were reported here as "the shim did not
    # fire". The same escape silently broke three existing tests that mock
    # `mod._git`, and would have kept `status.py`'s `_UNANSWERED` footer from
    # recording the one call it exists to record. So the helper takes the
    # caller's `_git`, and wrapping it here is sufficient by construction.
    monkeypatch.setattr(mod, "_git", recording(mod._git))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main()
    out = buf.getvalue()

    if expect_stall:
        assert stalls, (
            "the PATH shim did not fire: no git call returned TIMEOUT_RC, so "
            "this asserts nothing about a stalled probe" + chr(10) + out)
    elif stalls:
        pytest.skip(
            "contended-git(#1845): a git call this fixture needs to ANSWER did "
            "not -- " + "; ".join(f"`{c}`" for c in stalls)
            + ", so nothing is claimed about the product here")
    return out, rc


#: (module name, argv, which subcommand the repo probe issues).
#: `git-status` probes with `branch -vv`, run for its return code only.
#:
#: `blame` was missed by the first sweep of this fix and found by re-running the
#: issue's own work item — `grep` for the printed sentence across `presets/`
#: rather than for the sites already known. It has the identical shape, and its
#: own comment named `TIMEOUT_RC` two lines above the arm that ignored it. The
#: other three sites that print the sentence — `trail.py`, `investigate.py`,
#: `checkout.py` — are deliberately absent: each gates it on `not a git
#: repository` appearing in git's stderr, which a timeout's `timed out after Ns`
#: does not satisfy, so they already relay the timeout verbatim instead.
PROBES = [
    ("conflicts", ["conflicts.py"], "rev-parse"),
    ("resolve", ["resolve.py", "ours", "f"], "rev-parse"),
    ("diff", ["diff.py"], "rev-parse"),
    ("commit", ["commit.py", "a message", "f"], "rev-parse"),
    ("push", ["push.py"], "rev-parse"),
    ("status", ["status.py"], "branch"),
    ("blame", ["blame.py", "f", "1"], "rev-parse"),
]


@pytest.mark.parametrize("name,argv,sub", PROBES)
def test_a_stalled_repo_probe_is_not_rendered_as_not_a_repository(
    name, argv, sub, tmp_path: Path, monkeypatch
) -> None:
    """The defect: `TIMEOUT_RC` collapsed into git's `no`.

    The repository under test is genuinely mid-merge with a live conflict, so
    every word of `not inside a git repository` is false about it. Only the
    probe stalls; the tree is built and would be read by real git.
    """
    repo = _conflicted_repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, sub))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")

    out, rc = _run(_load(name), argv, repo, monkeypatch, expect_stall=True)

    assert FALSE_CLAIM not in out, out
    assert THIRD_STATE in out, out
    assert "did not answer" in out, out
    assert rc != 0, "a probe that did not answer must not exit clean"


@pytest.mark.parametrize("name,argv,sub", PROBES)
def test_a_directory_that_really_is_not_a_repository_still_says_so(
    name, argv, sub, tmp_path: Path, monkeypatch
) -> None:
    """The positive control, and the whole reason the fix is not worse than the bug.

    A fix that reads every non-zero return as *could not tell* buys the false
    claim's silence with a real refusal's silence. Real git, no shim: the loud
    failure path must stay exactly as loud as it was.
    """
    outside = _not_a_repo(tmp_path)
    monkeypatch.setenv("PATH", _git_only_path(tmp_path))

    out, rc = _run(_load(name), argv, outside, monkeypatch, expect_stall=False)

    assert FALSE_CLAIM in out, out
    assert THIRD_STATE not in out, out
    assert rc != 0, out


def test_a_stalled_state_probe_does_not_read_as_no_merge_in_progress(
    tmp_path: Path, monkeypatch
) -> None:
    """`conflicts.py::_detect_state` — the second false sentence in the same file.

    Its non-zero arm returned `""`, which `main` renders as `State: no
    merge/rebase/cherry-pick in progress`. The repository here is stopped
    mid-merge, so that sentence is as false as the repository claim above, and
    it lands in the one report a caller reaches for while stopped mid-merge.

    Asserted against the function rather than the render, deliberately. Both
    probes issue `rev-parse --git-dir`, so a fixture that stalls this one stalls
    the repository check first and `main` returns before ever reaching here —
    which means an end-to-end assertion that the false sentence is *absent*
    would pass for a reason that has nothing to do with this function.
    """
    repo = _conflicted_repo(tmp_path)
    mod = _load("conflicts")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "rev-parse"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")

    assert mod._detect_state() == mod.STATE_UNKNOWN


def test_the_state_probe_still_reads_a_real_merge_and_a_real_absence(
    tmp_path: Path, monkeypatch
) -> None:
    """The control, in both directions.

    A `_detect_state` that answered `unknown` unconditionally would satisfy the
    test above and destroy the function. So: a repository genuinely mid-merge
    still reads `merge`, and one with nothing in progress still reads `""` —
    the value `main` renders as the honest `no merge/rebase/cherry-pick in
    progress`.
    """
    merging = _conflicted_repo(tmp_path / "m")
    clean = _repo(tmp_path / "c")
    mod = _load("conflicts")
    monkeypatch.setenv("PATH", _git_only_path(tmp_path))

    monkeypatch.chdir(merging)
    assert mod._detect_state() == "merge"
    monkeypatch.chdir(clean)
    assert mod._detect_state() == ""


def test_the_report_renders_the_unknown_state_rather_than_asserting_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The render half, reached by stalling the SECOND `rev-parse` only.

    The repository check answers, so `main` runs; the state probe then loses its
    budget. That is the transient-contention shape the two-probe sequence makes
    reachable, and the only fixture in which the false `no merge/rebase/
    cherry-pick in progress` can actually be printed over a live merge.
    """
    repo = _conflicted_repo(tmp_path)
    monkeypatch.setenv("PATH",
                       _slow_git_path_after_first_call(tmp_path, "rev-parse"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")

    out, rc = _run(_load("conflicts"), ["conflicts.py"], repo, monkeypatch,
                   expect_stall=True)

    assert "no merge/rebase/cherry-pick in progress" not in out, out
    assert "State: UNKNOWN" in out, out
    assert FALSE_CLAIM not in out, out
    assert rc != 0, out


def test_a_repo_with_no_merge_in_progress_still_says_so(
    tmp_path: Path, monkeypatch
) -> None:
    """The control for the render: the honest sentence must survive."""
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _git_only_path(tmp_path))

    out, rc = _run(_load("conflicts"), ["conflicts.py"], repo, monkeypatch,
                   expect_stall=False)

    assert "no merge/rebase/cherry-pick in progress" in out, out
    assert "No conflicted files." in out, out
    assert "UNKNOWN" not in out, out
    assert rc == 0, out


def test_a_stalled_probe_does_not_cost_git_status_its_whole_report(
    tmp_path: Path, monkeypatch
) -> None:
    """`git-status` returned 1 and printed no report at all.

    `branch -vv` is a courtesy probe run for its return code. A stall there is
    not evidence about the repository, and the report the caller asked for was
    dropped on the strength of it. What replaces the drop is a named third
    state, not a report manufactured from a machine that is not answering:
    exiting 1 with a sentence a caller can retry on is the loud half kept loud.
    """
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", _slow_git_path(tmp_path, "branch"))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")

    out, rc = _run(_load("status"), ["status.py"], repo, monkeypatch,
                   expect_stall=True)

    assert "git failed" not in out, out
    assert "Nothing was inspected" in out, out
    assert rc != 0, out


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1858, REPO)
