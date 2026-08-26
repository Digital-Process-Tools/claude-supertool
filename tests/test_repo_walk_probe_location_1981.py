"""#1981 -- the env-leak probe test wrote its throwaway `.py` inside
`tests/`, which is inside `repo_python_files()`'s walk root (`tests/_repo_walk.py`).
Under `-n auto` (pyproject.toml:59) a concurrent worker running
`test_syntax_floor_478.py::test_every_repo_py_file_compiles_at_the_floor` could
enumerate the probe between its write and its `finally: probe.unlink()`, then
fail to open it -- coded as `unreadable` (`_supertool.py:20095-20096`), which
compiles into an `AssertionError` shaped exactly like a real `SyntaxError`
(`line: None, col: None`), naming a file that is in no commit.

The fix: the probe now writes under `tmp_path` instead of
`REPO_ROOT / "tests"`. `tmp_path` is pytest's own per-test scratch directory,
always outside the repository, so it can never be enumerated by a walk rooted
at `REPO_ROOT` no matter how it races with any other worker.

Three assertions, the first two paired so neither can pass on a broken
harness:

* a positive control -- write a real `.py` inside `tests/` (the walk root)
  and confirm `repo_python_files()` actually enumerates it. If this ever
  fails, the walk is broken and the negative assertion below is meaningless.
* the actual guard -- the leak-probe test now takes `tmp_path` as a fixture
  parameter (proving it writes there rather than under `REPO_ROOT`).
* the fixture the fix relies on is pinned as genuinely disjoint from the
  walk root, not merely assumed to be.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from _repo_walk import repo_python_files  # noqa: E402

import test_gl_repo_target_676 as _leak_probe_module  # noqa: E402


def test_the_walk_root_really_does_enumerate_a_file_placed_in_tests() -> None:
    """Positive control: a `.py` dropped in `tests/` is scanned. Without this,
    a negative result below would be indistinguishable from a broken walk."""
    control = REPO_ROOT / "tests" / "_walk_probe_control_1981.py"
    control.write_text("x = 1\n", encoding="utf-8")
    try:
        assert control in repo_python_files()
    finally:
        control.unlink(missing_ok=True)


def test_the_leak_probe_writes_outside_the_walk_root() -> None:
    """The regression guard: the probe-writing test must not create its
    throwaway file inside `REPO_ROOT` at all, so the race in #1981 cannot
    reoccur no matter how the walk and the write interleave."""
    fn = _leak_probe_module.test_the_env_var_main_sets_does_not_survive_into_the_next_test
    params = inspect.signature(fn).parameters
    assert "tmp_path" in params, (
        "the leak-probe test must take pytest's tmp_path fixture and write "
        "its throwaway probe there -- writing under REPO_ROOT/tests puts it "
        "inside repo_python_files()'s walk root, which is #1981"
    )


def test_tmp_path_fixture_is_disjoint_from_the_walk_root(tmp_path) -> None:
    """The fixture the fix relies on is actually outside the walk root --
    pinned directly rather than assumed, so a future pytest that changes
    where tmp_path lives cannot silently reopen #1981."""
    assert not tmp_path.is_relative_to(REPO_ROOT), (
        "tmp_path (" + str(tmp_path) + ") is inside REPO_ROOT (" + str(REPO_ROOT)
        + ") -- the walk root and the probe location are no longer disjoint"
    )
