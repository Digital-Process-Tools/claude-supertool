"""#1012: printed remedies prescribed `./supertool` where it does not resolve.

`./supertool` is a gitignored symlink. In a linked git worktree — where agents
work — it is absent, so the command simply fails; and in a `claude-supertool`
worktree specifically it is worse than absent, because the *global* `supertool`
on PATH resolves to the live clone and runs **master's core** against the
branch's presets. The repo's own rule is `python3 supertool.py` there.

So `git-conflicts` closed its output with

    Resolve: ./supertool 'git-resolve:::ours:::PATH'

at the moment the reader is mid-conflict and least likely to second-guess a
copy-pasteable command, and `git-push`'s watch advisory did the same for
`watch:`. A printed command is a claim about the environment it will be pasted
into, and both were written from the environment of whoever wrote them.

`push.py` already had the fix for one line of its own output (`_st_hint`, from
#879). These tests pin it as shared and applied at both sites the issue names.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conflicts = _load("git_conflicts_1012", "presets/git/conflicts.py")
push = _load("git_push_1012", "presets/git/push.py")
# The very module the two presets imported, not a second copy of it. Loading
# `_git_common.py` again under a fresh name would give a `st_hint` whose
# globals nothing else reads — every monkeypatch below would then land on an
# object no production call site consults, and every test would pass against
# unpatched code. That is the failure this repo keeps having.
git_common = sys.modules["_git_common"]
assert conflicts.st_hint.__globals__ is vars(git_common)


def _ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=0,
                                       stdout=stdout, stderr="")


def _worktree(monkeypatch, tmp_path: Path) -> Path:
    """An install with `supertool.py` and no `./supertool` wrapper."""
    (tmp_path / "supertool.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(git_common, "install_dir", lambda: str(tmp_path))
    return tmp_path


def _clone(monkeypatch, tmp_path: Path) -> Path:
    """An install that does have an executable `./supertool` wrapper."""
    _worktree(monkeypatch, tmp_path)
    wrapper = tmp_path / "supertool"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return tmp_path


# --- git-conflicts: the issue's subject ---


def _render_conflicts(monkeypatch, tmp_path: Path, *, wrapper: bool) -> str:
    (_clone if wrapper else _worktree)(monkeypatch, tmp_path)
    conflicted = tmp_path / "f.txt"
    conflicted.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n",
                          encoding="utf-8")

    def fake(args, timeout=None):
        if args[:1] == ["rev-parse"]:
            return _ok(str(tmp_path / ".git"))
        if args[:1] == ["diff"]:
            return _ok(str(conflicted) + "\n")
        return _ok("")

    monkeypatch.setattr(conflicts, "_git", fake)
    monkeypatch.setattr(conflicts, "_list_conflicts",
                        lambda: ([str(conflicted)], ""))
    monkeypatch.setattr(conflicts, "_detect_state", lambda: "rebase")
    monkeypatch.setattr(sys, "argv", ["conflicts.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        conflicts.main()
    return buf.getvalue()


def test_conflicts_hint_is_runnable_without_the_wrapper(monkeypatch, tmp_path) -> None:
    out = _render_conflicts(monkeypatch, tmp_path, wrapper=False)
    assert "git-resolve" in out, out
    assert "./supertool '" not in out, (
        "the resolution hint prescribes `./supertool`, which in a worktree is "
        "absent and in a claude-supertool worktree runs another tree's core:\n"
        + out
    )
    assert "supertool.py 'git-resolve" in out, out


def test_conflicts_hint_still_uses_the_wrapper_where_it_exists(
    monkeypatch, tmp_path
) -> None:
    """Not a blanket rewrite — the hint follows what is on disk."""
    out = _render_conflicts(monkeypatch, tmp_path, wrapper=True)
    assert "./supertool 'git-resolve:::ours:::" in out, out


# --- git-push's watch advisory: the second call site, from the comment ---


def _watch_out(monkeypatch, tmp_path: Path, capsys, *, wrapper: bool) -> str:
    (_clone if wrapper else _worktree)(monkeypatch, tmp_path)
    mr = {"source": "gitlab", "iid": 42, "target": "master"}
    monkeypatch.setattr(push, "_spawn_watch", lambda s, i: (False, "no runnable supertool"))
    push._watch_advisory(push.MrLookup(mr), {"watch"})
    return capsys.readouterr().out


def test_watch_remedy_is_runnable_without_the_wrapper(monkeypatch, tmp_path, capsys) -> None:
    out = _watch_out(monkeypatch, tmp_path, capsys, wrapper=False)
    assert "watch:gitlab-mr:42" in out, out
    assert "./supertool '" not in out, (
        "the receipt tells a user whose watcher failed to start to run a "
        "command that will also fail, in the environment where it fails:\n"
        + out
    )


def test_watch_pipeline_line_is_runnable_without_the_wrapper(
    monkeypatch, tmp_path, capsys
) -> None:
    _worktree(monkeypatch, tmp_path)
    mr = {"source": "gitlab", "iid": 42, "target": "master"}
    push._watch_advisory(push.MrLookup(mr), set())
    out = capsys.readouterr().out
    assert "watch:gitlab-mr:42" in out, out
    assert "./supertool '" not in out, out


# --- the helper itself ---


def test_st_hint_declines_when_neither_route_exists(monkeypatch, tmp_path) -> None:
    """Three states. A hint invented for an install we cannot find is a guess."""
    monkeypatch.setattr(git_common, "install_dir", lambda: str(tmp_path))
    hint = git_common.st_hint("git-status")
    assert "./supertool" not in hint, hint
    assert str(tmp_path) in hint, hint


def test_st_hint_quotes_nothing_it_did_not_build(monkeypatch, tmp_path) -> None:
    """The op string is interpolated verbatim; the shape must stay stable."""
    _worktree(monkeypatch, tmp_path)
    assert git_common.st_hint("git-resolve:::ours:::a b.py").endswith(
        "'git-resolve:::ours:::a b.py'"
    )
