"""#1919: `os.access(path, os.X_OK)` is a no-op on Windows -- any existing
file answers True, so the wrapper probe cannot tell a runnable `./supertool`
wrapper from a stray file wearing that name. The helper's third state ("no
runnable supertool found") is then unreachable on that platform, and a
refusal prints a remedy that does not run to a caller who is already stuck.

Both duplicated implementations are pinned here, the same pair
`test_printed_invocation_worktree_1012.py` and `test_st_hint_interpreter_1017.py`
already pin for `_git_common.st_hint` alone -- `presets/_st_hint.py::st_hint`
(#1917) carries the identical check and the identical defect.

Grade: **reasoned, not observed** on the machine this was written on -- there
is no Windows host in this project's own dev environment. `os.name` is
monkeypatched to `"nt"`, and `os.access` is monkeypatched to the behaviour the
real Windows implementation has -- true for any existing file, regardless of
the mode argument -- so the fixture reproduces the actual bug rather than
merely a value of `os.name`. Without faking `os.access` too, the pre-fix code
passes on any POSIX host: it never branches on `os.name` at all, so real
filesystem permission bits mask the defect this issue is about. **This does
get exercised for real on CI's own `windows-latest` leg** -- there `os.name`
is already `"nt"` and `os.access` already has the real broken behaviour, so
`_windows_like_access` there fakes nothing new (the fake function reduces to
what `os.access` already does), and the fix is genuinely exercised rather than
only simulated.

**`os.name` is patched INSIDE `monkeypatch.context()`, and every assertion
runs AFTER that context closes (#1930).** A first cut of this file patched
`os.name` with the bare `monkeypatch` fixture and asserted while it was still
patched. On a real Windows CI leg one of those tests failed for an unrelated,
correct reason (see below), and pytest's OWN failure-reporting code
(`_pytest/nodes.py::_repr_failure_py`) builds a fresh `Path(os.getcwd())`
while formatting the traceback -- which, with `os.name` still lying about the
platform, made `pathlib` pick `PosixPath` on a real Windows box and raise
`NotImplementedError`, taking an xdist worker down with it (`INTERNALERROR`,
exit code 3). A test that cannot fail cleanly on a platform is worse than one
that fails there: it turned one legitimate red into an unusable run. So every
`st_hint(...)` call that depends on the fake now happens while the context
manager is open, its result is captured into a plain local, the context
closes (restoring the REAL `os.name`) before control leaves the `with` block,
and only then do the `assert`s run -- so a genuine failure is reported with
the platform's own, true `os.name`, and can never re-trigger this crash.

The control pair the issue asks for, in the SAME fixture: a non-executable
stray file named `supertool` must report the third state, and a file that
looks like a genuine wrapper (matching this repo's own install convention,
README.md -- a symlink to a python file starting with a shebang) must still
print `./supertool`. Both cases explicitly set `install_dir` to a fresh
`tmp_path` so neither can pass by accidentally reading this checkout's own
real `./supertool` symlink -- the trap #1917's lane hit on a neighbouring
test.

**The POSIX sanity test is skipped on a real Windows host (#1930).** Its
whole point is "the POSIX arm is untouched -- still real `os.access(X_OK)`
against real permission bits", and `os.chmod(0o644)` does not produce a
non-executable file on Windows -- there is no POSIX permission model there for
`os.access(X_OK)` to read, so on a real Windows CI leg `os.access` answers
exactly the broken way #1919 is about, and the assertion fails for a REAL
reason that has nothing to do with a regression in this fix: the test is
asking a question Windows cannot meaningfully answer. Monkeypatching `os.name`
to `"posix"` does not change that -- it only changes what Python code branches
on, never what the underlying OS syscall does.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st_hint_mod = _load("_st_hint_1919", "presets/_st_hint.py")
git_common = _load("_git_common_1919", "presets/git/_git_common.py")

_IMPLS = (st_hint_mod, git_common)


def _windows_like_access(monkeypatch, mod) -> None:
    """The actual Windows behaviour: `os.access(path, X_OK)` is true for any
    existing file, mode argument ignored -- there is no execute bit for it to
    read. Faking this is what makes the pre-fix code actually fail here: it
    calls `os.access` unconditionally, so on a real POSIX host the genuine
    permission bits mask the bug this issue is about. On a real Windows host
    this fake reduces to what `os.access` already, genuinely, does."""
    real_exists = os.path.isfile

    def fake_access(path, mode):
        return real_exists(path)

    monkeypatch.setattr(mod.os, "access", fake_access)


def _stray_file(monkeypatch, tmp_path: Path, mod) -> None:
    """A file at the install root named `supertool` that is not a wrapper --
    ordinary content, no shebang, and not executable."""
    (tmp_path / "supertool.py").write_text("", encoding="utf-8")
    stray = tmp_path / "supertool"
    stray.write_text("not a wrapper, just a stray file\n", encoding="utf-8")
    stray.chmod(0o644)
    monkeypatch.setattr(mod, "install_dir", lambda: str(tmp_path))


def _real_wrapper(monkeypatch, tmp_path: Path, mod) -> None:
    """A file at the install root that matches this repo's own install
    convention (README.md): a symlink to a file beginning with a shebang."""
    (tmp_path / "supertool.py").write_text("#!/usr/bin/env python3\n",
                                            encoding="utf-8")
    wrapper = tmp_path / "supertool"
    try:
        wrapper.symlink_to(tmp_path / "supertool.py")
    except OSError:
        # symlinks are not always permitted (e.g. Windows without dev mode) --
        # a plain shebang-carrying file is the same probe input.
        wrapper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setattr(mod, "install_dir", lambda: str(tmp_path))


def test_stray_file_reports_third_state_on_windows(monkeypatch, tmp_path) -> None:
    hints = {}
    with monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        for mod in _IMPLS:
            _windows_like_access(m, mod)
            _stray_file(m, tmp_path, mod)
            hints[mod] = mod.st_hint("git-status")
    # Every assert below runs with the real os.name restored (#1930) -- a
    # failure here must never be reported while os.name still lies about the
    # platform, or pytest's own traceback formatter can crash.
    for mod, hint in hints.items():
        assert "./supertool" not in hint, (
            f"{mod.__name__}: a stray non-executable file named `supertool` "
            f"was printed as the runnable wrapper: {hint!r}"
        )
        assert "supertool.py" in hint, hint


def test_real_wrapper_still_prints_the_wrapper_form_on_windows(
    monkeypatch, tmp_path
) -> None:
    hints = {}
    with monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        for mod in _IMPLS:
            _windows_like_access(m, mod)
            _real_wrapper(m, tmp_path, mod)
            hints[mod] = mod.st_hint("git-status")
    for mod, hint in hints.items():
        assert hint == "./supertool 'git-status'", (
            f"{mod.__name__}: a genuine shebang-carrying wrapper was not "
            f"recognised as runnable on the simulated Windows arm: {hint!r}"
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="this test asks whether real, unfaked os.access(X_OK) still "
           "reads POSIX permission bits correctly -- Windows has no such "
           "bits for chmod(0o644) to clear, so os.access there answers "
           "exactly the way #1919 is about regardless of any os.name patch, "
           "and the assertion fails for a real, unrelated reason (#1930)",
)
def test_stray_file_still_reports_third_state_on_posix(monkeypatch, tmp_path) -> None:
    """Sanity: the POSIX arm is untouched -- still `os.access(X_OK)`, with the
    real filesystem answering it rather than the Windows-shaped fake above."""
    hints = {}
    with monkeypatch.context() as m:
        m.setattr(os, "name", "posix")
        for mod in _IMPLS:
            (tmp_path / "supertool.py").write_text("", encoding="utf-8")
            wrapper = tmp_path / "supertool"
            wrapper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            wrapper.chmod(0o644)  # readable, NOT executable
            m.setattr(mod, "install_dir", lambda: str(tmp_path))
            hints[mod] = mod.st_hint("git-status")
    for mod, hint in hints.items():
        assert "./supertool" not in hint, (
            f"{mod.__name__}: a non-executable file was printed as runnable "
            f"on POSIX: {hint!r}"
        )
