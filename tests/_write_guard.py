"""#1998 -- the class guard for the #1981 race, generalised.

#1981 fixed one probe writer (`tests/test_gl_repo_target_676.py`) that
created a real `.py` file inside `tests/`, inside
`repo_python_files()`'s walk root (`tests/_repo_walk.py`), and held it open
for the length of a nested `pytest` subprocess. `test_repo_walk_probe_
location_1981.py` pinned that ONE function: it runs it with `subprocess.run`
faked and asserts where the probe it wrote landed on disk.

#1998 is the same defect from a second, independent writer
(`tests/test_repo_target_673.py`), added *after* #1981 shipped, in a lane
that had been told in as many words to keep its own probe under `tmp_path`.
A per-function guard cannot see a new function -- it was never asked about
this one. What #1981 actually needed was a guard against the invariant
itself: no test in this suite may create a `.py` file inside
`repo_python_files()`'s walk root, ever, no matter which test does it.

## Why this shape and not the other two the issue names

A **static scan** over test sources for `REPO_ROOT / "tests" / "..."` writes
would have caught both #1981's and #1998's probes -- they are both literal.
It would NOT catch a path built by concatenation, an `os.path.join`, or a
helper that returns a path already anchored under `REPO_ROOT` and handed to
a generic `write_probe(path, body)` utility three frames away from the
literal. A scan answers "does this source text look like the bug", not "did
this run create the file" -- it is one static proxy for a runtime question.

A **snapshot fixture** (list the walk root before the test, list it after,
diff) is blind to exactly the case that keeps happening: the file is
created AND deleted inside the same test, inside a `finally:` block, well
within the test's own duration. A before/after diff run outside that window
sees nothing, in both the crashing case and the clean case -- it cannot
tell a fixed probe from a probe that raced and got lucky this run. That is
disqualifying: the whole point of a regression guard is to fail
deterministically on the shape that used to fail by chance.

So: a **runtime hook**, installed once per process (`install()` /
`uninstall()` below, wired to `pytest_configure` / `pytest_unconfigure` in
`conftest.py`) rather than per test, because the race is not scoped to
"whichever test is currently running" -- xdist runs many tests concurrently
across workers, and the write and the competing walk can be in different
tests in different workers. Continuous for the life of the process is the
only scope that covers that.

## What this cannot catch (say it, do not imply completeness)

* A `.py` file created through anything other than `pathlib.Path.write_text`
  / `Path.write_bytes` -- a bare `open(str(path), "w")`, `os.open` +
  `os.write`, `shutil.copy`, or a shelled-out `git checkout` / `touch` /
  `curl -o`. Nothing in this suite currently creates a throwaway `.py` that
  way (grepped for `write_text`/`write_bytes` across `tests/*.py` while
  writing this guard -- #1962's probe was the only match against
  `REPO_ROOT`), but a new writer using one of those routes is invisible to
  this hook exactly as it would be to the static scan.
* A write from a process this one does not share memory with -- a *nested*
  `pytest` subprocess (like the leak-probe tests themselves spawn) has its
  own unpatched `pathlib.Path`. That is not a gap for THIS bug, because the
  probes write their own file from the *outer* process before spawning the
  child; it would be a gap for a hypothetical probe that asked the child to
  write it.
* A write to a `.py` path that is machine state by `_repo_walk.
  is_machine_state` (e.g. inside `.venv/`) -- correctly excluded, because
  `repo_python_files()` itself never walks there either.

## The must-fire / must-not-reproduce-#1981 tension

The regression test for this guard has to prove it catches a `.py` write
into a walk root -- but writing one into the REAL walk root (`tests/`) to
prove that is #1981's own shape, one level down (`test_repo_walk_probe_
location_1981.py`'s docstring names an earlier draft that did exactly this
and was caught in review). So the control below, like that file's own
control, never writes into `REPO_ROOT`: it builds a throwaway sandbox
directory under `tmp_path` and asks the guard to treat THAT as the root,
the same seam `_repo_walk.scanned_with(ignored, root)` already exposes for
the same reason.
"""
from __future__ import annotations

import contextlib
import pathlib
from pathlib import Path

from _repo_walk import git_ignored_dirs, is_machine_state

REPO_ROOT = Path(__file__).resolve().parent.parent

# A stack, not a single (root, ignored) pair: `install()` is called once
# globally from `conftest.py`'s `pytest_configure`, and the regression tests
# below need to nest a SECOND, narrower guard (against a `tmp_path` sandbox)
# inside that global one without silently no-op'ing on top of it. Every
# write is checked against every entry on the stack -- narrowest guard and
# the global one both stay live -- so nesting can only ever add coverage,
# never quietly replace it. `Path.write_text`/`write_bytes` are patched once,
# when the stack goes from empty to non-empty, and restored once, when it
# goes back to empty.
_stack: list = []
_orig_write_text = None
_orig_write_bytes = None


def would_create_walked_py(path: Path, root: Path, ignored) -> bool:
    """True when writing to *path* would land a new `.py` file inside the
    `repo_python_files()`-style walk rooted at *root*.

    Pure and side-effect-free so it can be unit-tested directly, without
    going anywhere near `pathlib.Path` patching.
    """
    # Case-insensitively: `repo_python_files()` walks via `Path.rglob("*.py")`,
    # and CPython's glob matching normalises case on Windows
    # (`ntpath.normcase` lowercases; `posixpath.normcase` is the identity) --
    # so a `.PY`-suffixed file is a real member of the walk root there, even
    # though it never is on POSIX. Matching case-insensitively everywhere is
    # the "wider than needed, never narrower" rule `_repo_walk.py`'s own
    # docstring states for the walk itself: on POSIX this guards a shape the
    # real walk happens not to enumerate, which is a false alarm nobody
    # will ever see fire, not a missed one.
    if path.suffix.lower() != ".py":
        return False
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False  # outside root entirely -- e.g. a normal tmp_path write
    return not is_machine_state(rel.as_posix(), ignored)


def _violation_message(path: Path, root: Path) -> str:
    return (
        f"{path} would create a .py file inside the repo_python_files() "
        f"walk root ({root}) at runtime. That is #1981/#1998's own race: "
        "a concurrent worker running the syntax-floor or bare-python3 walk "
        "can enumerate this file and then fail to open it once this test's "
        "`finally:` unlinks it. Write throwaway .py fixtures under tmp_path "
        "and pass the absolute path to any nested subprocess instead."
    )


def _first_violation(path: Path):
    for root, ignored in _stack:
        if would_create_walked_py(path, root, ignored):
            return root
    return None


def _guarded_write_text(self, *args, **kwargs):
    root = _first_violation(self)
    if root is not None:
        raise AssertionError(_violation_message(self, root))
    return _orig_write_text(self, *args, **kwargs)


def _guarded_write_bytes(self, *args, **kwargs):
    root = _first_violation(self)
    if root is not None:
        raise AssertionError(_violation_message(self, root))
    return _orig_write_bytes(self, *args, **kwargs)


def install(root: Path = REPO_ROOT, ignored=None) -> None:
    """Push a guarded root onto the stack, patching `pathlib.Path.
    write_text`/`write_bytes` process-wide the first time the stack goes
    from empty to non-empty. Any write landing a `.py` file inside ANY
    currently-guarded root raises immediately, at the point of the write,
    rather than racing a concurrent walk that may or may not observe it.

    *ignored* defaults to `git_ignored_dirs(root)` computed once here --
    not on every write, since the ignore set does not change mid-session
    and every write is otherwise paying for a `git` subprocess it does not
    need.
    """
    global _orig_write_text, _orig_write_bytes
    resolved_ignored = ignored if ignored is not None else git_ignored_dirs(root)
    if not _stack:
        _orig_write_text = pathlib.Path.write_text
        _orig_write_bytes = pathlib.Path.write_bytes
        pathlib.Path.write_text = _guarded_write_text
        pathlib.Path.write_bytes = _guarded_write_bytes
    _stack.append((root, resolved_ignored))


def uninstall() -> None:
    """Pop the most recently pushed guarded root. Restores the original
    `Path.write_text`/`write_bytes` only once the stack is empty again.
    Best-effort: calling this with nothing on the stack is a no-op rather
    than an error, so `pytest_unconfigure` can call it unconditionally."""
    if not _stack:
        return
    _stack.pop()
    if not _stack:
        pathlib.Path.write_text = _orig_write_text
        pathlib.Path.write_bytes = _orig_write_bytes


def is_installed() -> bool:
    """True while at least one guarded root is active.

    Exists so `conftest.py` can print whether the process-wide guard this
    module installs from `pytest_configure` is actually live at session end,
    the same way `_git_decline`/`_live_gh`/etc. each print a summary line
    rather than letting a silently-failed import (this module is one of the
    `try: import ... except ImportError: _write_guard = None` fallbacks in
    `conftest.py`, tolerated for the synthetic-repo suites) read exactly
    like a session where the guard ran the whole time and caught nothing.
    """
    return bool(_stack)


@contextlib.contextmanager
def installed_against(root: Path, ignored=None):
    """Context-manager form for tests: install against *root* (a sandbox,
    never the real `REPO_ROOT` -- see the module docstring), run the body,
    always uninstall on the way out even if the body raises."""
    install(root=root, ignored=ignored)
    try:
        yield
    finally:
        uninstall()
