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

* a positive control -- write a real `.py` into a throwaway sandbox root
  under `tmp_path` and confirm `scanned_with()` (the parameterised walk
  `repo_python_files()` itself calls) enumerates it there. If this ever
  fails, the walk is broken and the negative assertion below is meaningless.
  Deliberately not written into the real `tests/` walk root: an earlier
  draft did exactly that and reintroduced #1981's own shape, narrower but
  real, under this test's own name.
* the actual guard -- runs the real leak-probe test (faking only the nested
  pytest subprocess) and checks where its throwaway file actually landed on
  disk, not merely whether the function's signature mentions `tmp_path`.
* the fixture the fix relies on is pinned as genuinely disjoint from the
  walk root, not merely assumed to be.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from _repo_walk import scanned_with  # noqa: E402

import test_gl_repo_target_676 as _leak_probe_module  # noqa: E402


def test_the_walk_root_really_does_enumerate_a_file_placed_in_it(tmp_path) -> None:
    """Positive control, rewritten per review: an earlier draft of this test
    wrote its control file directly into `tests/` -- the real walk root --
    to prove the walk was not vacuous. That reintroduced #1981's own shape
    one level down: `scanned_with`'s own docstring in `tests/_repo_walk.py`
    names exactly this risk ("a test that writes into the repository root
    races every other worker's copy of these very walks, which is a worse
    bug than the one it would be pinning"), and `-n auto` (pyproject.toml:59)
    runs `test_syntax_floor_478.py` concurrently in every ordinary run, so a
    real worker could in principle have observed this control mid-delete --
    narrower than #1981's own 60s window, but the same shape, created by
    this very regression test rather than merely inherited from elsewhere.

    `scanned_with(ignored, root)` takes the walk root as a parameter for
    exactly this reason -- `test_syntax_floor_478.py:253` already calls it
    against a throwaway sandbox rather than `REPO_ROOT`. This control does
    the same: it writes into a sandbox under `tmp_path` and asks
    `scanned_with` to walk THAT root, proving the walk logic actually
    enumerates a file placed in it, with no write anywhere inside the real
    walk root `repo_python_files()` scans.
    """
    control = tmp_path / "_walk_probe_control_1981.py"
    control.write_text("x = 1\n", encoding="utf-8")
    assert control in scanned_with(frozenset(), tmp_path)


def test_the_leak_probe_actually_writes_outside_the_walk_root(tmp_path, monkeypatch) -> None:
    """The regression guard, driven at the byte level rather than the
    signature level: runs the REAL leak-probe test function, faking only the
    expensive nested pytest subprocess, and inspects the actual probe path it
    wrote to on disk. A weaker version of this check (does the function merely
    TAKE a `tmp_path` parameter) can be satisfied by a future edit that adds
    the parameter but never uses it, or that resolves it right back inside
    `REPO_ROOT` (`tmp_path.parent.parent / "tests" / ...`) -- caught by
    review, not by an earlier draft of this file. This version cannot be
    fooled that way: it asserts on where the write actually landed.
    """
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kwargs):
        # The probe path is the argv element ending in `.py` with no
        # `::nodeid` suffix -- the OTHER `.py` argument (`f"{__file__}::..."`)
        # names this very test file, plus a pytest node id, and is excluded
        # by the `::` check.
        candidates = [a for a in cmd
                      if isinstance(a, str) and a.endswith(".py") and "::" not in a]
        assert candidates, "no bare .py argv element found in " + repr(cmd)
        probe_path = Path(candidates[0])
        captured["probe_path"] = probe_path
        captured["existed_when_run_was_called"] = probe_path.is_file()
        return _FakeCompleted()

    monkeypatch.setattr(_leak_probe_module.subprocess, "run", _fake_run)

    fn = _leak_probe_module.test_the_env_var_main_sets_does_not_survive_into_the_next_test
    fn(tmp_path)

    assert captured, (
        "subprocess.run was never called -- the leak-probe test's shape "
        "changed and this guard no longer observes what it needs to")
    assert captured["existed_when_run_was_called"], (
        "the probe file did not exist on disk at the point subprocess.run "
        "was invoked -- the write must happen before the child pytest runs")
    assert not captured["probe_path"].resolve().is_relative_to(REPO_ROOT), (
        str(captured["probe_path"]) + " is inside REPO_ROOT (" + str(REPO_ROOT)
        + ") -- the leak-probe test is writing inside repo_python_files()'s "
        "walk root again, which is #1981"
    )


def test_tmp_path_fixture_is_disjoint_from_the_walk_root(tmp_path) -> None:
    """The fixture the fix relies on is actually outside the walk root --
    pinned directly rather than assumed, so a future pytest that changes
    where tmp_path lives cannot silently reopen #1981."""
    assert not tmp_path.is_relative_to(REPO_ROOT), (
        "tmp_path (" + str(tmp_path) + ") is inside REPO_ROOT (" + str(REPO_ROOT)
        + ") -- the walk root and the probe location are no longer disjoint"
    )
