"""#1998 -- the class guard for the #1981 race: no test in this suite may
create a `.py` file inside `repo_python_files()`'s walk root at runtime, no
matter which test does it. See `tests/_write_guard.py`'s module docstring
for why this shape (a runtime hook over `pathlib.Path.write_text`/
`write_bytes`, installed for the life of the process) was chosen over a
static source scan or a before/after snapshot fixture, and what it cannot
catch.

Three assertions, the first two paired so neither can pass on a broken
harness -- the same discipline `test_repo_walk_probe_location_1981.py`
uses, for the same reason:

* a positive control -- install the guard against a throwaway sandbox under
  `tmp_path` (never the real walk root -- see `_write_guard`'s docstring on
  why a control that writes into `tests/` itself would reproduce #1981 one
  level down) and confirm a `.py` write into that sandbox is caught.
* a negative control alongside it -- the same guard, installed against the
  same sandbox, must NOT fire on a write outside the sandbox (an ordinary
  `tmp_path` fixture write) or on a non-`.py` write inside it. A guard that
  fires on everything would pass the positive control for the wrong reason.
* the actual class guard, run against the real `REPO_ROOT` -- installs
  against the true walk root and asserts a direct call that mimics what
  `test_repo_target_673.py` used to do (before its own #1998 fix) is
  caught. This is what proves the class is covered, independent of which
  file the next writer happens to add.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from _write_guard import REPO_ROOT as _GUARD_REPO_ROOT  # noqa: E402
from _write_guard import installed_against, would_create_walked_py  # noqa: E402


def test_positive_control_a_py_write_into_the_sandboxed_root_is_caught(tmp_path) -> None:
    sandbox = tmp_path / "sandbox_walk_root"
    sandbox.mkdir()
    with installed_against(sandbox, ignored=frozenset()):
        target = sandbox / "_would_be_enumerated_1998.py"
        try:
            target.write_text("x = 1\n", encoding="utf-8")
        except AssertionError as exc:
            assert str(sandbox) in str(exc)
        else:
            raise AssertionError(
                "the guard let a .py write into its own sandboxed root "
                "through -- the positive control is meaningless if this "
                "does not raise")


def test_negative_control_writes_outside_the_sandbox_are_not_touched(tmp_path) -> None:
    """Pairs with the positive control: a guard broad enough to catch
    everything would pass that test for the wrong reason. Two writes that
    must both go through untouched -- a `.py` write OUTSIDE the guarded
    root, and a non-`.py` write INSIDE it."""
    sandbox = tmp_path / "sandbox_walk_root"
    sandbox.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with installed_against(sandbox, ignored=frozenset()):
        outside = elsewhere / "fine.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        assert outside.read_text(encoding="utf-8") == "x = 1\n"

        not_python = sandbox / "fine.txt"
        not_python.write_text("hello\n", encoding="utf-8")
        assert not_python.read_text(encoding="utf-8") == "hello\n"


def test_a_probe_written_the_old_way_is_caught_against_the_real_walk_root(tmp_path) -> None:
    """The class guard itself, run against the true `REPO_ROOT` (read-only
    for everything except the one write this test makes, which the guard
    itself refuses before it ever touches disk). This mimics exactly what
    `tests/test_repo_target_673.py`'s own probe writer did before its
    #1998 fix: `REPO_ROOT / "tests" / "<name>.py"`. If a future edit moves
    that probe back into the walk root, or a brand new probe writer is
    added anywhere in this suite using the same shape, this is what catches
    it -- independent of which function it is.
    """
    would_be_probe = REPO_ROOT / "tests" / "_class_guard_demo_1998_not_actually_written.py"
    with installed_against(_GUARD_REPO_ROOT):
        try:
            would_be_probe.write_text("x = 1\n", encoding="utf-8")
        except AssertionError as exc:
            assert "walk root" in str(exc)
        else:
            raise AssertionError(
                "a .py write straight into REPO_ROOT/tests was not caught "
                "-- the class guard is not covering the real walk root")
    assert not would_be_probe.exists(), (
        "the guard is supposed to raise BEFORE the write reaches disk; "
        "finding the file here means it was created for real")


def test_would_create_walked_py_is_the_pure_check_the_hook_delegates_to(tmp_path) -> None:
    """Unit-level pin on the predicate itself, independent of any Path
    patching -- so a change to the predicate's own logic is caught here
    rather than only through the (slower, patch-based) tests above."""
    root = tmp_path / "root"
    root.mkdir()
    assert would_create_walked_py(root / "a.py", root, frozenset())
    assert not would_create_walked_py(root / "a.txt", root, frozenset())
    assert not would_create_walked_py(tmp_path / "outside" / "a.py", root, frozenset())
    assert not would_create_walked_py(root / "venv" / "a.py", root, frozenset())
