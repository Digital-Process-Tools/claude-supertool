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
from _symlink import require_symlink


def _init_repo(root: Path) -> None:
    def run(*a):
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / ".gitignore").write_bytes(b"ignored.txt\n")
    for name in ("clean.txt", "dirty.txt", "also_clean.txt", "third.txt"):
        (root / name).write_bytes(b"orig\n")
    # A subdirectory, so a path can be written with a directory component in it
    # the way the CLI hands one over (#1186).
    (root / "sub").mkdir()
    for name in ("clean.txt", "dirty.txt"):
        (root / "sub" / name).write_bytes(b"orig\n")
    run("add", "-A")
    run("commit", "-qm", "seed")
    (root / "dirty.txt").write_bytes(b"changed\n")
    (root / "untracked.txt").write_bytes(b"new\n")
    (root / "ignored.txt").write_bytes(b"hush\n")
    (root / "sub" / "dirty.txt").write_bytes(b"changed\n")
    (root / "sub" / "untracked.txt").write_bytes(b"new\n")
    (root / "sub" / "ignored.txt").write_bytes(b"hush\n")


def _reset_bulk() -> None:
    """Drop any coalesced snapshot, declines included, and the repo-root walk.

    `.clear()` and not `_path_meta_bulk_drop()`: production deliberately keeps
    a `declined` verdict across an invalidation, and a test setting up a fresh
    repo wants neither that nor a root memoised from a previous tmp_path.
    Tolerates the symbols not existing yet so the spawn-count test below reds
    on the count rather than on an AttributeError."""
    getattr(supertool, "_PATH_META_BULK", {}).clear()
    getattr(supertool, "_PATH_META_ROOT_CACHE", {}).clear()


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


# The form the CLI actually hands the op: relative, with a directory component.
_SUB_PATHS = ("sub/clean.txt", "sub/dirty.txt", "sub/untracked.txt",
              "sub/ignored.txt")


def test_a_relative_path_with_a_directory_answers_like_an_absolute_one(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1186 - the parity test above passes absolute paths and top-level names,
    and both of those resolve fine. The per-path query runs with
    cwd=dirname(abspath(path)) while passing the path *as written*, so
    `sub/s.txt` resolved a second time against `<repo>/sub`: git warned on
    stderr, exited 0 with empty stdout, and the marker silently vanished. The
    bulk arm keys off `relpath(absolute, root)` and was always right, so the
    same file got two different answers depending on its position in a batch."""
    monkeypatch.chdir(repo)

    for name in _SUB_PATHS:
        _reset_bulk()                              # force the single-path route
        relative = supertool._path_meta_suffix(name, b"x\n")
        _reset_bulk()
        absolute = supertool._path_meta_suffix(str(repo / name), b"x\n")
        assert relative == absolute, (
            f"{name}: written relative -> {relative!r}, written absolute -> "
            f"{absolute!r}; the pathspec is being resolved against the wrong "
            f"directory"
        )

    per_path = {}
    for name in _SUB_PATHS:
        _reset_bulk()
        per_path[name] = supertool._path_meta_suffix(name, b"x\n")

    _reset_bulk()
    coalesced = {n: supertool._path_meta_suffix(n, b"x\n") for n in _SUB_PATHS}

    assert coalesced == per_path
    # and the answers are the real ones, not uniformly empty
    assert " m" in per_path["sub/dirty.txt"]
    assert " ?" in per_path["sub/untracked.txt"]
    assert " !" in per_path["sub/ignored.txt"]
    assert " m" not in per_path["sub/clean.txt"]
    assert " ?" not in per_path["sub/clean.txt"]


def test_a_filename_that_looks_like_a_glob_is_not_matched_against_a_sibling(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1186, second arm. The per-path route hands a filename to git as a
    *pathspec*, and a pathspec globs: a clean `t[a].txt` matched its modified
    sibling `ta.txt` and reported the sibling's ` m` as its own. The bulk arm
    looks the name up in a dict, which is literal, so the two routes disagreed
    again — this time by inventing a marker rather than by losing one, which is
    the worse direction: the answer names a file that is not the one asked
    about. `[`/`]` and not `*`/`?` in the fixture because Windows will not let a
    filename contain those, and a test that cannot run on one platform proves
    nothing there."""
    monkeypatch.chdir(repo)
    (repo / "sub" / "ta.txt").write_bytes(b"orig\n")
    (repo / "sub" / "t[a].txt").write_bytes(b"orig\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "glob fixture"],
                   check=True, capture_output=True)
    (repo / "sub" / "ta.txt").write_bytes(b"changed\n")

    literal = os.path.join("sub", "t[a].txt")
    _reset_bulk()
    alone = supertool._path_meta_suffix(literal, b"x\n")
    _reset_bulk()
    for name in _SUB_PATHS[:2]:
        supertool._path_meta_suffix(name, b"x\n")   # build the snapshot
    with_snapshot = supertool._path_meta_suffix(literal, b"x\n")

    assert " m" not in alone, (
        "t[a].txt is committed and unmodified; the ' m' belongs to ta.txt"
    )
    assert alone == with_snapshot
    # the fixture is doing what it claims - the sibling really is modified
    assert " m" in supertool._path_meta_suffix(os.path.join("sub", "ta.txt"),
                                               b"x\n")


def test_a_relative_symlink_is_still_answered_about_its_own_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cwd the per-path query runs in is load-bearing, not incidental: it is
    what keeps this route and the bulk route talking about the same repository
    when a link points across a repo boundary (`_path_meta_repo_root`'s
    docstring). A symlink skips the bulk arm entirely, so this pins the per-path
    arm's repo choice for the relative-with-a-directory form too."""
    require_symlink()
    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    _init_repo(here)
    _init_repo(elsewhere)
    _reset_bulk()
    monkeypatch.chdir(here)

    link = here / "sub" / "points_away.txt"
    link.symlink_to(elsewhere / "clean.txt")

    assert " ?" in supertool._path_meta_suffix(
        os.path.join("sub", "points_away.txt"), b"x\n"
    ), "an untracked link is untracked in the repo it sits in, not its target's"


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


def test_a_symlink_gets_the_same_marker_from_either_route(repo: Path) -> None:
    """The bug this test exists for: git keys its records by the name in the
    tree, and looking one up under the link's *target* name missed every time.
    A miss is indistinguishable from clean, so an untracked symlink rendered as
    a tracked, unmodified file — a wrong answer, not a slow one."""
    require_symlink()
    (repo / "real").mkdir()
    (repo / "real" / "target.txt").write_bytes(b"committed\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add target"],
                   check=True, capture_output=True)
    link = repo / "link.txt"
    link.symlink_to(Path("real") / "target.txt")

    _reset_bulk()
    alone = supertool._path_meta_suffix(str(link), b"x\n")
    # Now with a snapshot already built for this repo by earlier paths.
    _reset_bulk()
    for name in _PATHS[:3]:
        supertool._path_meta_suffix(str(repo / name), b"x\n")
    with_snapshot = supertool._path_meta_suffix(str(link), b"x\n")

    assert " ?" in alone, "an untracked symlink is untracked"
    assert with_snapshot == alone


def test_a_symlink_is_never_answered_from_another_repo(tmp_path: Path) -> None:
    """Resolving the link first picked the repo the *target* lives in, so the
    marker beside a file in one repo was computed from another one's status."""
    require_symlink()
    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    _init_repo(here)
    _init_repo(elsewhere)
    _reset_bulk()

    link = here / "points_away.txt"
    link.symlink_to(elsewhere / "clean.txt")
    for name in _PATHS[:3]:
        supertool._path_meta_suffix(str(here / name), b"x\n")

    assert " ?" in supertool._path_meta_suffix(str(link), b"x\n")


def test_a_file_deep_under_an_untracked_directory_keeps_its_marker(
    repo: Path,
) -> None:
    """A repo-wide status collapses a whole untracked directory into one record,
    so the marker for anything inside it comes from the ancestor walk, and that
    walk has to climb more than one level."""
    deep = repo / "newdir" / "sub" / "deeper"
    deep.mkdir(parents=True)
    target = deep / "file.txt"
    target.write_bytes(b"new\n")

    _reset_bulk()
    alone = supertool._path_meta_suffix(str(target), b"x\n")
    _reset_bulk()
    for name in _PATHS[:3]:
        supertool._path_meta_suffix(str(repo / name), b"x\n")
    with_snapshot = supertool._path_meta_suffix(str(target), b"x\n")

    assert " ?" in alone
    assert with_snapshot == alone


def test_a_repo_whose_query_declines_falls_back_and_stays_fallen_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch, counter: _StatusCounter
) -> None:
    """A status query that cannot answer must not be read as a clean tree, and
    the verdict must outlive an edit — otherwise every write re-pays the full
    timeout to rediscover the same thing."""
    monkeypatch.setattr(supertool, "_path_meta_bulk_fill", lambda root: None)

    for name in _PATHS:
        supertool._path_meta_suffix(str(repo / name), b"x\n")

    root = supertool._path_meta_repo_root(str(repo / "clean.txt"))
    assert supertool._PATH_META_BULK[root] == "declined"
    # Every path fell back, so every path was really asked about.
    assert counter.status == len(_PATHS)
    assert " m" in supertool._path_meta_suffix(str(repo / "dirty.txt"), b"x\n")

    supertool._path_meta_bulk_drop()
    assert supertool._PATH_META_BULK.get(root) == "declined", (
        "a declined verdict describes the repo, not the tree - a write must "
        "not throw it away"
    )


def test_a_snapshot_does_not_outlive_a_write_but_a_decline_does(
    repo: Path,
) -> None:
    """The two halves of `_path_meta_bulk_drop`, against each other."""
    for name in _PATHS[:3]:
        supertool._path_meta_suffix(str(repo / name), b"x\n")
    root = supertool._path_meta_repo_root(str(repo / "clean.txt"))
    assert isinstance(supertool._PATH_META_BULK[root], dict)

    supertool._PATH_META_BULK["/somewhere/else"] = "declined"
    supertool._path_meta_bulk_drop()

    assert root not in supertool._PATH_META_BULK
    assert supertool._PATH_META_BULK["/somewhere/else"] == "declined"


def test_the_snapshot_globals_are_registered_for_reset() -> None:
    """#397's list is what stops per-call scratch surviving into the next test."""
    import conftest

    assert "_PATH_META_BULK" in conftest.RESET_GLOBALS
    assert "_PATH_META_ROOT_CACHE" in conftest.RESET_GLOBALS
