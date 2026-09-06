"""#1397 -- `_path_meta_suffix` must not spawn `git status` when the repo-root
walk already found no `.git` anywhere up the tree.

`_path_meta_repo_root` answering `""` means the walk climbed from the path's
own directory to the filesystem root without finding a `.git`. In that state
the per-path `git status` spawn that used to follow could only ever answer
"not a git repository" (silently swallowed) or fail (rendering the existing
`git?` decline) -- it can never produce a working-tree marker. That is a
spawn that cannot produce information: ~10ms locally, 150-800ms under an AV
filter driver or in CI's Windows/QEMU runners, paid on every single-file read
outside a repository.

Skipping the spawn outright risks this repo's own defect class -- an absence
produced by the tool, read as an absence in the world -- if it were rendered
identically to "asked git, and it's clean". So the walk's `""` must render as
its own distinct token (`supertool.PATH_META_NOT_CONSULTED`), never conflated
with a clean file (no marker) or with the existing decline
(`supertool.PATH_META_UNKNOWN`, which means git *was* asked and failed to
answer).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import supertool


class _StatusCounter:
    """Counts `git status` spawns while delegating to the real subprocess."""

    def __init__(self) -> None:
        self.status = 0
        self._real = subprocess.run

    def __call__(self, cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git" and "status" in cmd:
            self.status += 1
        return self._real(cmd, *args, **kwargs)


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> _StatusCounter:
    c = _StatusCounter()
    monkeypatch.setattr(supertool.subprocess, "run", c)
    return c


def _reset_bulk() -> None:
    getattr(supertool, "_PATH_META_BULK", {}).clear()
    getattr(supertool, "_PATH_META_ROOT_CACHE", {}).clear()


def _init_repo(root: Path) -> None:
    def run(*a):
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

    run("init", "-q", "-b", "master")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / "clean.txt").write_bytes(b"orig\n")
    run("add", "-A")
    run("commit", "-qm", "seed")


def test_a_path_outside_any_repo_spawns_no_git_status(
    tmp_path: Path, counter: _StatusCounter
) -> None:
    """The wasted spawn this issue is about: zero `git status` calls."""
    _reset_bulk()
    loose = tmp_path / "loose.txt"
    loose.write_bytes(b"hello\n")

    supertool._path_meta_suffix(str(loose))

    assert counter.status == 0, (
        f"{counter.status} `git status` spawn(s) for a path the repo-root "
        f"walk already said has no repository"
    )


def test_a_path_outside_any_repo_renders_the_not_consulted_token(
    tmp_path: Path,
) -> None:
    """The token must exist, be distinct, and actually render."""
    _reset_bulk()
    loose = tmp_path / "loose.txt"
    loose.write_bytes(b"hello\n")

    out = supertool._path_meta_suffix(str(loose))

    assert supertool.PATH_META_NOT_CONSULTED in out.split()


def test_the_not_consulted_token_is_not_the_existing_decline_token() -> None:
    """Distinct from `git?` (asked, and it failed to answer)."""
    assert supertool.PATH_META_NOT_CONSULTED != supertool.PATH_META_UNKNOWN


def test_the_not_consulted_token_is_not_one_of_the_answer_tokens(
    tmp_path: Path,
) -> None:
    """Distinct from the three real answers -- a decline must not be
    confusable with an answer, the same rule #705 already holds for `git?`."""
    _reset_bulk()
    loose = tmp_path / "loose.txt"
    loose.write_bytes(b"hello\n")

    tokens = supertool._path_meta_suffix(str(loose)).split()

    assert "?" not in tokens
    assert "!" not in tokens
    assert "m" not in tokens


# ---------------------------------------------------------------------------
# Positive control: a path INSIDE a real repository is unaffected.
# ---------------------------------------------------------------------------

def test_a_path_inside_a_real_repo_still_spawns_git_and_answers_clean(
    tmp_path: Path, counter: _StatusCounter
) -> None:
    """The walk finding a repo must still trigger the existing lookup path --
    this fix must not regress in-repo behavior."""
    _init_repo(tmp_path)
    _reset_bulk()

    out = supertool._path_meta_suffix(str(tmp_path / "clean.txt"), b"orig\n")

    assert counter.status >= 1, "no git status spawn for a path inside a real repo"
    assert supertool.PATH_META_NOT_CONSULTED not in out.split()
    assert out == "", "a clean tracked file inside a repo must render nothing"


def test_a_path_inside_a_real_repo_still_renders_the_existing_decline_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #1596's own gate, restated here: a real in-repo git failure must
    still render the existing `git?` decline, unchanged by this fix."""
    _init_repo(tmp_path)
    _reset_bulk()
    target = tmp_path / "clean.txt"

    real_run = subprocess.run

    def _timeout(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git" and "status" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 2))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(supertool.subprocess, "run", _timeout)

    out = supertool._path_meta_suffix(str(target), b"orig\n")

    assert supertool.PATH_META_UNKNOWN in out.split(), out
    assert supertool.PATH_META_NOT_CONSULTED not in out.split()
