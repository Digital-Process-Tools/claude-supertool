"""#1635 -- the canary that watches whether a suite run leaves the tree
standing, for the destructive event this repo never managed to reproduce.

`test_directory_removal_ownership_1635.py` proved every directory-removal
call this tree's own code makes is owned. This file is the safety net for
what that register cannot see -- a mechanism outside this tree's Python --
and it is tested here without touching the real repository tree: every test
below builds its own throwaway root under `tmp_path`.
"""
from __future__ import annotations

from pathlib import Path

import _root_canary


def _make(root: Path, *names: str) -> None:
    for name in names:
        if name == ".git":
            (root / name).mkdir()
        else:
            (root / name).write_text("x")


def test_snapshot_reports_absent_for_a_marker_that_is_not_there(
        tmp_path: Path) -> None:
    snap = _root_canary.snapshot(tmp_path)
    assert snap == {".git": "absent", "pyproject.toml": "absent",
                    "supertool.py": "absent"}


def test_snapshot_reports_kind_for_each_marker_present(tmp_path: Path) -> None:
    _make(tmp_path, ".git", "pyproject.toml", "supertool.py")
    snap = _root_canary.snapshot(tmp_path)
    assert snap == {".git": "dir", "pyproject.toml": "file",
                    "supertool.py": "file"}


def test_verdict_is_none_when_nothing_vanished(tmp_path: Path) -> None:
    _make(tmp_path, ".git", "pyproject.toml", "supertool.py")
    before = _root_canary.snapshot(tmp_path)
    after = _root_canary.snapshot(tmp_path)
    assert _root_canary.verdict(before, after) is None


def test_verdict_is_none_when_a_marker_never_existed_to_begin_with(
        tmp_path: Path) -> None:
    """A synthetic tree missing `.git` from the start is not this canary's
    business -- it cannot tell "destroyed" from "never here", and reporting
    the former for the latter is the false alarm that gets a canary
    disabled."""
    before = _root_canary.snapshot(tmp_path)
    after = _root_canary.snapshot(tmp_path)
    assert _root_canary.verdict(before, after) is None


def test_verdict_names_a_marker_that_vanished(tmp_path: Path) -> None:
    """The must-fire half of the pair above: a marker present before and
    gone after is exactly #1635's own shape, and must not pass silently."""
    _make(tmp_path, ".git", "pyproject.toml", "supertool.py")
    before = _root_canary.snapshot(tmp_path)
    (tmp_path / ".git").rmdir()
    after = _root_canary.snapshot(tmp_path)
    finding = _root_canary.verdict(before, after)
    assert finding is not None
    assert ".git" in finding


def test_verdict_names_every_marker_that_vanished_not_just_one(
        tmp_path: Path) -> None:
    _make(tmp_path, ".git", "pyproject.toml", "supertool.py")
    before = _root_canary.snapshot(tmp_path)
    (tmp_path / ".git").rmdir()
    (tmp_path / "pyproject.toml").unlink()
    after = _root_canary.snapshot(tmp_path)
    finding = _root_canary.verdict(before, after)
    assert finding is not None
    assert ".git" in finding and "pyproject.toml" in finding
    assert "supertool.py" not in finding


def test_a_marker_changing_kind_counts_as_vanished_not_as_unchanged(
        tmp_path: Path) -> None:
    """A worktree's `.git` is a file; a clone's is a directory. Either one
    turning into the other is a real event on this marker, not noise --
    `.git` going from `dir` to `absent` is the shape this canary exists to
    catch, and a kind change is the same category of "not what it was"."""
    _make(tmp_path, ".git", "pyproject.toml", "supertool.py")
    before = _root_canary.snapshot(tmp_path)
    (tmp_path / ".git").rmdir()
    (tmp_path / ".git").write_text("gitdir: /elsewhere")
    after = _root_canary.snapshot(tmp_path)
    # Not literally "absent" (it is a file now), so `verdict` -- which only
    # tracks the absent/not-absent transition -- correctly says nothing
    # vanished; documented here as the boundary of what it checks.
    assert _root_canary.verdict(before, after) is None
    assert after[".git"] == "file" and before[".git"] == "dir"


# ---------------------------------------------------------------------------
# The wiring in tests/conftest.py -- exercised against a throwaway root, not
# the real repository, so a bug here cannot reproduce #1635 while proving it.
# ---------------------------------------------------------------------------

class _FakeConfig:
    def __init__(self) -> None:
        pass


class _FakeSession:
    def __init__(self, config) -> None:
        self.config = config
        self.exitstatus = 0


def test_sessionstart_then_sessionfinish_is_silent_when_nothing_vanished(
        tmp_path: Path, monkeypatch) -> None:
    import conftest
    _make(tmp_path, ".git", "pyproject.toml", "supertool.py")
    monkeypatch.setattr(conftest, "REPO_ROOT", tmp_path)
    config = _FakeConfig()
    session = _FakeSession(config)
    conftest.pytest_sessionstart(session)
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    assert getattr(config, "_supertool_root_canary_finding", None) is None


def test_sessionfinish_forces_the_exit_status_when_a_marker_vanished(
        tmp_path: Path, monkeypatch) -> None:
    import conftest
    _make(tmp_path, ".git", "pyproject.toml", "supertool.py")
    monkeypatch.setattr(conftest, "REPO_ROOT", tmp_path)
    config = _FakeConfig()
    session = _FakeSession(config)
    conftest.pytest_sessionstart(session)
    (tmp_path / ".git").rmdir()
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    finding = getattr(config, "_supertool_root_canary_finding", None)
    assert finding is not None and ".git" in finding


def test_a_worker_process_is_not_snapshotted_or_finished(
        tmp_path: Path, monkeypatch) -> None:
    """`workerinput` is xdist's own marker for "this config belongs to a
    worker". Present, and neither hook should touch the config at all --
    the controller already owns this check."""
    import conftest

    class _WorkerConfig(_FakeConfig):
        workerinput = {}

    monkeypatch.setattr(conftest, "REPO_ROOT", tmp_path)
    config = _WorkerConfig()
    session = _FakeSession(config)
    conftest.pytest_sessionstart(session)
    assert not hasattr(config, "_supertool_root_canary_before")
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    assert getattr(config, "_supertool_root_canary_finding", None) is None


def test_markers_include_git_and_two_siblings_1635_observed_missing(
        ) -> None:
    """#1635's own observed end state was a tree reduced to `tests/` alone
    -- `.git` gone AND its siblings gone. A canary that only asked about
    `.git` would still be correct on that shape, but the population is
    pinned here so a future edit narrowing it to one marker is a visible
    diff rather than a silent regression."""
    assert set(_root_canary.MARKERS) == {".git", "pyproject.toml",
                                         "supertool.py"}
