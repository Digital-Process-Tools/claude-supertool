"""#624 — a missing path must name the path it actually tried.

The issue reports two distinct failures wearing one hat:

1. `./supertool` is a relative wrapper, so any `cd` deeper into the repo makes
   the documented invocation vanish. That is a shell-layer fact — no Python
   change can make `./supertool` exist in a directory that has no such file —
   and it is answered in the docs, not here.
2. A path arg that does not resolve says *what* was not found but never *where*
   it looked, so "wrong path" and "wrong cwd" read identically. Auto-recovery
   (#363) already handles the unambiguous case; when it declines, the error is
   all the caller gets, and it has to be honest and complete.

These tests pin (2): the absolute path tried is always printed, and when the
file demonstrably exists under the project root above cwd the error names that
root and the exact `cwd:` prefix that would reach it — without ever resolving
there silently.
"""

import os

import pytest

import supertool


@pytest.fixture
def restore_cwd():
    saved = os.getcwd()
    yield
    os.chdir(saved)


@pytest.fixture
def project(tmp_path, restore_cwd):
    """A project root with a marker, a src/foo.py, and a deep subdir cwd."""
    (tmp_path / ".supertool.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    sub = tmp_path / "tests" / "e2e"
    sub.mkdir(parents=True)
    os.chdir(sub)
    return tmp_path, sub


def test_read_missing_path_names_the_absolute_path_tried(project) -> None:
    """`ERROR: file not found: src/foo.py` alone cannot be acted on."""
    _root, _sub = project
    out = supertool.op_read("nope/missing.py")
    assert "not found" in out
    assert os.path.join(os.getcwd(), "nope/missing.py") in out


def test_read_missing_path_points_at_the_root_that_has_it(project) -> None:
    """The file exists one level up at the project root — say so, and say how."""
    root, _sub = project
    out = supertool.op_read("src/foo.py")
    assert "not found" in out
    assert os.path.realpath(str(root)) in out
    assert f"cwd:{os.path.realpath(str(root))}" in out


def test_grep_missing_path_names_the_absolute_path_tried(project) -> None:
    _root, _sub = project
    out = supertool.op_grep("anything", "nope/missing.py")
    assert "ERROR: path not found" in out
    assert os.path.join(os.getcwd(), "nope/missing.py") in out


def test_grep_missing_path_points_at_the_root_that_has_it(project) -> None:
    root, _sub = project
    out = supertool.op_grep("x", "src/foo.py")
    assert "not found" in out
    assert f"cwd:{os.path.realpath(str(root))}" in out


def test_no_root_is_invented_when_the_file_is_nowhere(project) -> None:
    """The loud failure stays loud: no root claim the tool cannot back up."""
    root, _sub = project
    out = supertool.op_read("src/does-not-exist-anywhere.py")
    assert "not found" in out
    assert "exists at" not in out
    assert f"cwd:{os.path.realpath(str(root))}" not in out


def test_absolute_missing_path_is_not_reresolved_against_the_root(project,
                                                                  tmp_path) -> None:
    """An absolute path means what it says — never re-rooted."""
    root, _sub = project
    out = supertool.op_read(str(root / "src" / "nope.py"))
    assert "not found" in out
    assert "exists at" not in out


def test_root_hint_absent_when_cwd_is_itself_the_project_root(project) -> None:
    """Nothing to recover from at the root — no misleading self-referential hint."""
    root, _sub = project
    os.chdir(root)
    out = supertool.op_read("src/nope.py")
    assert "not found" in out
    assert "exists at" not in out
