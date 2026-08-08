"""The printed hint named `python3`, which is not the launcher on Windows (#1017).

`_git_common.st_hint` is the one place this repo composes a copy-pasteable
invocation, and its second branch hard-coded the string `python3`. On Windows the
launcher is `py` or `python`, so the remedy printed to the reader most likely to
paste it verbatim — mid-conflict, mid-failed-push — did not run at all.

`_watch_argv` in `push.py` had already settled this for the *spawn* in #642: it
resolves `sys.executable`, the interpreter that is demonstrably running. The two
disagreed, and `push.py::_st_hint`'s docstring asserted they agreed — pointing at
the correct implementation while the code used the other one.

So the assertions below are about agreement between the two, not about the
absence of a substring. `sys.executable` on a POSIX box frequently *is* a path
ending in `python3`, which is exactly why "does the output contain python3" is
not the question.
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


push = _load("git_push_1017", "presets/git/push.py")
# The module the presets themselves imported. A second copy under a fresh name
# would give a `st_hint` whose globals no production call site reads, and every
# monkeypatch below would land on nothing (#1012).
git_common = sys.modules["_git_common"]


def _worktree(monkeypatch, tmp_path: Path) -> Path:
    """An install with `supertool.py` and no `./supertool` wrapper."""
    (tmp_path / "supertool.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(git_common, "install_dir", lambda: str(tmp_path))
    monkeypatch.setattr(push, "install_dir", lambda: str(tmp_path))
    return tmp_path


def _interpreter_of(hint: str) -> str:
    """The interpreter token of HINT, with any quoting the platform needed removed."""
    head, sep, _rest = hint.partition(" supertool.py ")
    assert sep, hint
    return head[1:-1] if head[:1] in ("'", chr(34)) else head


def test_the_hint_names_the_interpreter_that_is_running(monkeypatch, tmp_path) -> None:
    _worktree(monkeypatch, tmp_path)
    assert _interpreter_of(git_common.st_hint("git-status")) == sys.executable


def test_the_named_interpreter_exists_on_this_machine(monkeypatch, tmp_path) -> None:
    """Non-vacuity: the point is a command that runs, not a string that parses."""
    _worktree(monkeypatch, tmp_path)
    assert os.path.isfile(_interpreter_of(git_common.st_hint("git-status")))


def test_an_interpreter_path_with_a_space_is_quoted(monkeypatch, tmp_path) -> None:
    """A Windows all-users install lives under `Program Files`, and that has a space.

    Unquoted, the hint asks the shell to run `C:\\Program` — #1017 again one layer in.
    Asserted per platform rather than skipped on one, so neither leg is vacuous.
    """
    _worktree(monkeypatch, tmp_path)
    if os.name == "nt":
        spaced = "C:" + chr(92) + "Program Files" + chr(92) + "py.exe"
    else:
        spaced = "/opt/py 3/bin/python3"
    monkeypatch.setattr(sys, "executable", spaced)

    hint = git_common.st_hint("git-status")
    if os.name == "nt":
        assert hint.startswith(chr(34) + spaced + chr(34) + " supertool.py "), hint
    else:
        assert shlex.split(hint) == [spaced, "supertool.py", "git-status"], hint


def test_the_hint_agrees_with_the_spawn_about_the_interpreter(
        monkeypatch, tmp_path) -> None:
    """The docstring's claim, as a test rather than as prose.

    `_watch_argv` and `st_hint` answer the same question — how do you invoke this
    tool here — for the same install. Two answers is the defect.
    """
    _worktree(monkeypatch, tmp_path)
    argv, _how = push._watch_argv("gitlab-mr", "42")
    assert argv, "fixture did not produce the interpreter branch"
    assert _interpreter_of(git_common.st_hint("git-status")) == argv[0]


def test_the_hint_still_points_at_the_entry_point_beside_the_presets(
        monkeypatch, tmp_path) -> None:
    _worktree(monkeypatch, tmp_path)
    assert "supertool.py 'git-status'" in git_common.st_hint("git-status")


def test_the_op_string_is_still_interpolated_verbatim(monkeypatch, tmp_path) -> None:
    """#1012's shape must not move underneath its own test."""
    _worktree(monkeypatch, tmp_path)
    assert git_common.st_hint("git-resolve:::ours:::a b.py").endswith(
        "'git-resolve:::ours:::a b.py'")


def test_the_wrapper_branch_is_untouched(monkeypatch, tmp_path) -> None:
    """Where `./supertool` exists and is executable it is still the answer."""
    _worktree(monkeypatch, tmp_path)
    wrapper = tmp_path / "supertool"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    assert git_common.st_hint("git-status") == "./supertool 'git-status'"
