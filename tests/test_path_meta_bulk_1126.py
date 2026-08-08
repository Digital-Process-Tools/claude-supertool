"""#1126 - `read` pays a `git status` spawn per rendered path.

The filed shape was "memoise `_path_meta_suffix` per invocation". A memo keyed
by path cannot help the case the issue exists for: a batched `read` of seven
files asks about seven *different* paths, so every lookup is a miss. Measured on
this repo, macOS, 2026-08-08:

    supertool read:README.md              165 ms   (48 ms of it `git status`)
    supertool read x7 (one call)          504 ms   (355 ms of it, 70%)
    7x git status, one per path           355 ms
    1x git status covering all 7 paths     51 ms

So the lever is coalescing the *query*, not remembering the answer. These tests
pin the spawn count, the markers the coalesced query produces, and the three
things that invalidate it.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
import supertool


def _init_repo(root: Path) -> None:
    def run(*a):
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / ".gitignore").write_bytes(b"ignored.txt\n")
    for name in ("clean.txt", "dirty.txt", "also_clean.txt", "third.txt"):
        (root / name).write_bytes(b"orig\n")
    run("add", "-A")
    run("commit", "-qm", "seed")
    (root / "dirty.txt").write_bytes(b"changed\n")
    (root / "untracked.txt").write_bytes(b"new\n")
    (root / "ignored.txt").write_bytes(b"hush\n")


def _reset_bulk() -> None:
    """Drop any coalesced snapshot. Tolerates the symbol not existing yet so the
    spawn-count test below reds on the count, not on an AttributeError."""
    getattr(supertool, "_PATH_META_BULK", {}).clear()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    _reset_bulk()
    return tmp_path


class _StatusCounter:
    """Counts `git status` spawns while delegating to the real subprocess."""

    def __init__(self) -> None:
        self.status = 0
        self.all_git = 0
        self._real = subprocess.run

    def __call__(self, cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            self.all_git += 1
            if "status" in cmd:
                self.status += 1
        return self._real(cmd, *args, **kwargs)


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> _StatusCounter:
    c = _StatusCounter()
    monkeypatch.setattr(supertool.subprocess, "run", c)
    return c


# Order matters: the first path pays a per-path spawn, the second escalates to
# one repo-wide query that answers it and everything after it.
_PATHS = ("clean.txt", "dirty.txt", "untracked.txt", "ignored.txt",
          "also_clean.txt", "third.txt")


def test_batched_read_does_not_spawn_one_git_status_per_path(
    repo: Path, counter: _StatusCounter
) -> None:
    """Six rendered paths in one repo must not cost six `git status` spawns."""
    for name in _PATHS:
        supertool._path_meta_suffix(str(repo / name), b"x\n")
    assert counter.status <= 2, (
        f"{counter.status} `git status` spawns for {len(_PATHS)} paths - the "
        f"query is not being coalesced"
    )


def test_coalesced_markers_match_the_per_path_answer(repo: Path) -> None:
    """Cheaper must not mean different. Every marker equals the uncoalesced one."""
    per_path = {}
    for name in _PATHS:
        _reset_bulk()                              # force the single-path route
        per_path[name] = supertool._path_meta_suffix(str(repo / name), b"x\n")

    _reset_bulk()
    coalesced = {n: supertool._path_meta_suffix(str(repo / n), b"x\n")
                 for n in _PATHS}

    assert coalesced == per_path
    # and the answers are the real ones, not uniformly empty
    assert " m" in per_path["dirty.txt"]
    assert " ?" in per_path["untracked.txt"]
    assert " !" in per_path["ignored.txt"]
    assert " m" not in per_path["clean.txt"]
    assert " ?" not in per_path["clean.txt"]


def test_a_single_path_costs_exactly_one_spawn(
    repo: Path, counter: _StatusCounter
) -> None:
    """No regression for the 1-op call - it must not start paying for a bulk."""
    supertool._path_meta_suffix(str(repo / "dirty.txt"), b"x\n")
    assert counter.status == 1


def test_a_write_through_supertool_drops_the_snapshot(repo: Path) -> None:
    """An edit between two reads must not be reported against the old snapshot."""
    for name in _PATHS[:3]:
        supertool._path_meta_suffix(str(repo / name), b"x\n")
    assert supertool._PATH_META_BULK, "expected a snapshot to exist by now"

    supertool._atomic_write(str(repo / "clean.txt"), "rewritten\n")

    assert not supertool._PATH_META_BULK, (
        "_atomic_write left a stale git snapshot in place"
    )
    assert " m" in supertool._path_meta_suffix(str(repo / "clean.txt"), b"x\n")


def test_a_file_touched_after_the_snapshot_is_not_served_from_it(repo: Path) -> None:
    """mtime newer than the snapshot means the snapshot cannot speak for it."""
    for name in _PATHS[:3]:
        supertool._path_meta_suffix(str(repo / name), b"x\n")
    assert supertool._PATH_META_BULK

    # Written behind supertool's back - no chokepoint fires, only mtime moves.
    # The write alone already lands after the snapshot; the explicit utime says
    # so unambiguously rather than leaning on the filesystem's mtime resolution.
    # An hour, not a decade: a year-2096 stamp is representable on NTFS and APFS
    # but not on every volume a Windows runner might use, and the assertion here
    # is about ordering, not about how far ahead the clock can be pushed.
    target = repo / "also_clean.txt"
    target.write_bytes(b"changed by someone else\n")
    an_hour_ahead = (time.time() + 3600)
    os.utime(target, (an_hour_ahead, an_hour_ahead))

    assert " m" in supertool._path_meta_suffix(str(target), b"x\n")


def test_a_path_outside_any_repo_still_declines_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """No repo, no marker - and no crash from the coalescing path."""
    _reset_bulk()
    loose = tmp_path / "loose.txt"
    loose.write_bytes(b"x\n")
    out = supertool._path_meta_suffix(str(loose), b"x\n")
    assert " ?" not in out and " m" not in out and " !" not in out


def test_two_repos_in_one_call_do_not_answer_for_each_other(tmp_path: Path) -> None:
    """The snapshot is per repo root - a second repo gets its own."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _init_repo(a)
    _init_repo(b)
    _reset_bulk()

    for name in _PATHS:
        supertool._path_meta_suffix(str(a / name), b"x\n")
    (b / "also_clean.txt").write_bytes(b"only dirty in b\n")
    assert " m" in supertool._path_meta_suffix(str(b / "also_clean.txt"), b"x\n")
    assert " m" not in supertool._path_meta_suffix(str(a / "also_clean.txt"), b"x\n")


def test_the_snapshot_globals_are_registered_for_reset() -> None:
    """#397's list is what stops per-call scratch surviving into the next test."""
    import conftest

    assert "_PATH_META_BULK" in conftest.RESET_GLOBALS
