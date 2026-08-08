"""A branch checked out in a sibling worktree is not a MISMATCH (#850).

`gh-pr`, `gh-job`, `gh-run`, `gl-mr` and `gl-job` each built the same string by
hand:

    You are on: master ⚠ MISMATCH — switch with: ./supertool 'git-checkout:fix/900'

`⚠ MISMATCH` is true of the current directory and reads as a statement about the
repository, so a reader concludes the branch is checked out nowhere when it is
checked out one directory over. Worse, the prescribed command is one
`git-checkout` itself refuses — it already prints `ref <ref> is checked out in
another worktree` and names `cd <path>`. The advice line recommends the action
its own implementation rejects.

These tests use a *real* git repo with a *real* linked worktree, because the
defect is entirely in what git knows and the string did not ask. A test that
stubbed the worktree lookup would pass against the broken code.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Every site the issue names. A fix that lands on one and not the others leaves
# the surface everyone else reads.
#
# `gh-run` was one of the five and is no longer here: #1056 removed the
# prescription from that op entirely, on the ground that reading a run is not a
# claim about wanting its branch, so there is no checkout advice left for these
# tests to be about. Its replacement line is pinned by
# `tests/test_gh_run_read_only_branch_line_1056.py`, which also asserts these
# four still carry the imperative — dropping it here does not drop the
# coverage, it moves the four-vs-one boundary somewhere a reader can see it.
SITES = {
    "gh-pr": "presets/github/pr.py",
    "gh-job": "presets/github/job.py",
    "gl-mr": "presets/gitlab/mr.py",
    "gl-job": "presets/gitlab/job.py",
}

MODULES = {op: _load(rel, f"site_850_{op.replace('-', '_')}")
           for op, rel in SITES.items()}

BRANCH = "fix/900"


def _as_git_path(p: Path) -> str:
    """The path as *git* spells it, which is not how the OS spells it.

    `git worktree list --porcelain` emits POSIX separators on every platform —
    `C:/Users/runneradmin/...` on the Windows runners, where `str(WindowsPath)`
    gives `C:\\Users\\runneradmin\\...`. The render prints git's form verbatim
    and should: `presets/git/checkout.py` answers the very same situation with
    `Switch with: cd <path>` straight out of git's stderr, so normalising to
    `os.sep` in one of the two would make the pair disagree about one path —
    and Git Bash, the shell on those runners, takes the forward-slash form.

    So the assertions below compare in git's terms. Using `str()` made the
    location tests fail on Windows for a spelling difference, and — worse —
    made the detached-worktree test pass there **vacuously**: a backslash
    string can never appear in a forward-slash render, so the assertion it was
    named for could not fail.
    """
    return p.as_posix()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r


@pytest.fixture
def repo_with_sibling_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A repo on `master` with `fix/900` held by a linked worktree.

    Returns `(main_checkout, sibling_worktree)`.
    """
    main = tmp_path / "clone"
    main.mkdir()
    _git("init", "-q", "-b", "master", cwd=main)
    _git("config", "user.email", "t@example.invalid", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "f.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "f.txt", cwd=main)
    _git("commit", "-qm", "init", cwd=main)

    sibling = tmp_path / "st-wt" / "900"
    _git("worktree", "add", "-q", "-b", BRANCH, str(sibling), cwd=main)
    return main, sibling


# ---------------------------------------------------------------------------
# the claim git itself makes — the premise these tests rest on
# ---------------------------------------------------------------------------

def test_git_checkout_refuses_the_command_the_line_prescribes(
    repo_with_sibling_worktree: tuple[Path, Path]
) -> None:
    """The advice is not merely imprecise, it is an action git rejects."""
    main, _sibling = repo_with_sibling_worktree
    # Named explicitly, like every other child process in this repo (#856):
    # `text=True` alone decodes by the locale codec, which is cp1252 on the
    # Windows runners and raises inside subprocess's reader thread. The module
    # under test gets this right; this helper did not, and the meta-test that
    # guards the seam caught it.
    r = subprocess.run(["git", "checkout", BRANCH], cwd=str(main),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode != 0, "premise broken: git allowed the checkout"
    said = r.stderr + r.stdout
    assert "worktree" in said, f"premise broken: git refused for another reason: {said!r}"


# ---------------------------------------------------------------------------
# all five sites — the branch is checked out one directory over
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(SITES))
def test_a_sibling_worktree_is_not_rendered_as_a_mismatch(
    monkeypatch, repo_with_sibling_worktree: tuple[Path, Path], op: str
) -> None:
    main, sibling = repo_with_sibling_worktree
    monkeypatch.chdir(main)
    line = MODULES[op]._local_branch_check(BRANCH)

    assert "MISMATCH" not in line, (
        f"{op}: the branch IS checked out — MISMATCH reads as a claim about "
        f"the repository and is false there"
    )
    assert BRANCH in line, f"{op}: the branch must still be named"
    assert _as_git_path(sibling) in line, (
        f"{op}: the worktree holding the branch is the whole answer and is "
        f"absent from: {line!r}"
    )


@pytest.mark.parametrize("op", sorted(SITES))
def test_the_rendered_path_is_the_one_git_itself_printed(
    monkeypatch, repo_with_sibling_worktree: tuple[Path, Path], op: str
) -> None:
    """Character-identical to git's porcelain — the one platform-free check.

    Everything else here compares against a path this test built, which is a
    claim about `pathlib` as much as about the render. This compares the render
    against the bytes `git worktree list --porcelain` actually emitted, so it
    holds the same on POSIX and on Windows, where the two spellings diverge.
    It is the assertion that would have caught the `str()`/`as_posix()` slip
    without a Windows runner to find it.
    """
    main, _sibling = repo_with_sibling_worktree
    monkeypatch.chdir(main)
    porcelain = _git("worktree", "list", "--porcelain", cwd=main).stdout
    holder = ""
    for block in porcelain.split("\n\n"):
        fields = dict(ln.partition(" ")[::2] for ln in block.splitlines() if ln)
        if fields.get("branch", "").replace("refs/heads/", "", 1) == BRANCH:
            holder = fields.get("worktree", "")
    assert holder, f"premise broken: git named no worktree for {BRANCH}"

    line = MODULES[op]._local_branch_check(BRANCH)
    assert holder in line, (
        f"{op}: the render spells the path differently from git, which wrote "
        f"it. git said {holder!r}; the line is {line!r}"
    )


@pytest.mark.parametrize("op", sorted(SITES))
def test_no_site_prescribes_a_checkout_git_would_refuse(
    monkeypatch, repo_with_sibling_worktree: tuple[Path, Path], op: str
) -> None:
    main, _sibling = repo_with_sibling_worktree
    monkeypatch.chdir(main)
    line = MODULES[op]._local_branch_check(BRANCH)
    assert f"git-checkout:{BRANCH}" not in line, (
        f"{op}: prescribes the command git-checkout itself rejects"
    )


@pytest.mark.parametrize("op", sorted(SITES))
def test_the_state_is_still_stated_when_you_are_not_on_the_branch(
    monkeypatch, repo_with_sibling_worktree: tuple[Path, Path], op: str
) -> None:
    """Dropping the warning entirely would be the quiet failure. Not that."""
    main, _sibling = repo_with_sibling_worktree
    monkeypatch.chdir(main)
    line = MODULES[op]._local_branch_check(BRANCH)
    assert line, f"{op}: rendered nothing — silence is not an answer"
    assert "master" in line, f"{op}: must still say which branch you are on"


# ---------------------------------------------------------------------------
# the ordinary single-worktree case must keep its warning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(SITES))
def test_a_branch_checked_out_nowhere_still_warns_and_still_advises(
    monkeypatch, repo_with_sibling_worktree: tuple[Path, Path], op: str
) -> None:
    main, sibling = repo_with_sibling_worktree
    _git("worktree", "remove", str(sibling), cwd=main)
    monkeypatch.chdir(main)
    line = MODULES[op]._local_branch_check(BRANCH)
    assert "MISMATCH" in line, f"{op}: lost a genuine warning"
    assert f"git-checkout:{BRANCH}" in line, (
        f"{op}: the advice is correct here and must survive"
    )


@pytest.mark.parametrize("op", sorted(SITES))
def test_standing_on_the_branch_still_reads_as_a_match(
    monkeypatch, repo_with_sibling_worktree: tuple[Path, Path], op: str
) -> None:
    _main, sibling = repo_with_sibling_worktree
    monkeypatch.chdir(sibling)
    line = MODULES[op]._local_branch_check(BRANCH)
    assert line == f"You are on: {BRANCH} ✓", f"{op}: {line!r}"


# ---------------------------------------------------------------------------
# a detached worktree on the same commit is not the branch's home
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(SITES))
def test_a_detached_worktree_is_not_mistaken_for_holding_the_branch(
    monkeypatch, tmp_path: Path, op: str
) -> None:
    main = tmp_path / "clone2"
    main.mkdir()
    _git("init", "-q", "-b", "master", cwd=main)
    _git("config", "user.email", "t@example.invalid", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "f.txt").write_text("hi\n", encoding="utf-8")
    _git("add", "f.txt", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("branch", BRANCH, cwd=main)
    detached = tmp_path / "detached"
    _git("worktree", "add", "-q", "--detach", str(detached), BRANCH, cwd=main)

    monkeypatch.chdir(main)
    line = MODULES[op]._local_branch_check(BRANCH)
    # Both spellings, so this cannot go vacuous on a platform where only one
    # of them was ever renderable.
    for spelling in (_as_git_path(detached), str(detached)):
        assert spelling not in line, (
            f"{op}: a worktree sitting on the same commit does not hold the "
            f"branch — conflating them reintroduces the confusion: {line!r}"
        )
    assert "MISMATCH" in line, f"{op}: the branch is held nowhere; warn"


# ---------------------------------------------------------------------------
# three states: when the lookup cannot answer, say so
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(SITES))
def test_an_unanswerable_worktree_lookup_is_not_rendered_as_nowhere(
    monkeypatch, repo_with_sibling_worktree: tuple[Path, Path], op: str
) -> None:
    """`git worktree list` failing must not become 'checked out nowhere'."""
    main, _sibling = repo_with_sibling_worktree
    monkeypatch.chdir(main)
    mod = MODULES[op]
    real = subprocess.run

    def fake_run(args, **kw: Any):
        if list(args[:3]) == ["git", "worktree", "list"]:
            return subprocess.CompletedProcess(args=args, returncode=128,
                                               stdout="", stderr="boom")
        return real(args, **kw)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    line = mod._local_branch_check(BRANCH)
    assert f"git-checkout:{BRANCH}" not in line, (
        f"{op}: prescribed a checkout on an unestablished answer"
    )
    assert "UNKNOWN" in line, (
        f"{op}: an unanswered lookup must read as unestablished, not as "
        f"'checked out nowhere': {line!r}"
    )


# ---------------------------------------------------------------------------
# end to end — the line has to reach stdout, not just the helper
# ---------------------------------------------------------------------------

def test_gl_job_prints_the_worktree_location_in_its_output(
    monkeypatch, capsys, repo_with_sibling_worktree: tuple[Path, Path]
) -> None:
    import json
    main, sibling = repo_with_sibling_worktree
    monkeypatch.chdir(main)
    job = MODULES["gl-job"]
    meta = json.dumps({
        "name": "test-job", "status": "failed", "stage": "test", "duration": 3.0,
        "web_url": "https://gitlab.example/job/1", "ref": BRANCH,
        "pipeline": {"id": 999},
    })
    real = subprocess.run

    def fake_run(args, **kw: Any):
        if args and args[0] == "git":
            return real(args, **kw)
        url = args[2] if len(args) > 2 else ""
        stdout = "boom\nERROR: it broke\n" if url.endswith("/trace") else meta
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout=stdout, stderr="")

    monkeypatch.setattr(job.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["job.py", "123"])
    assert job.main() == 0
    out = capsys.readouterr().out
    assert _as_git_path(sibling) in out, (
        f"the location never reached stdout:\n{out}")
    assert "MISMATCH" not in out
