"""`git-investigate` blames a path without a `--` separator (#759).

Three of the four git calls in `investigate.py` pass the path after an explicit
`--`; the blame call does not. `git blame --line-porcelain -foo.txt` therefore
hands `-foo.txt` to git as an option rather than as a path, and the section
renders "## Blame: unavailable" — which reads as "this file has no blame
history" when what happened is that the argument was never treated as a file.

Read-only, so nothing is lost. What is lost is the answer: the op's own
`git-blame` preset passes the separator, and the sibling `log` / `diff` calls in
this same file pass it, so the section that fails is the only one that can.

Hermetic: a tmp repo per test, no network, no remote, self-cleaning.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "investigate.py"
_spec = importlib.util.spec_from_file_location("git_investigate_759", PRESET)
assert _spec is not None and _spec.loader is not None
investigate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(investigate)


_HERMETIC = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _run(repo: Path, *args: str) -> None:
    env = {**os.environ, **_HERMETIC}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                   capture_output=True)


@pytest.fixture()
def repo_with(tmp_path, monkeypatch):
    """A one-commit repo holding a file under whatever name the test asks for."""
    def _make(filename: str) -> Path:
        repo = tmp_path / "r"
        repo.mkdir()
        _run(repo, "init", "-q")
        (repo / filename).write_text("alpha\nbeta\n", encoding="utf-8")
        _run(repo, "add", "--", filename)
        _run(repo, "commit", "-qm", "one")
        monkeypatch.chdir(repo)
        for var, value in _HERMETIC.items():
            monkeypatch.setenv(var, value)
        return repo
    return _make


def _investigate(filename: str) -> str:
    """Run the op, restoring argv before returning.

    `investigate.sys` is the real `sys` module, so assigning to
    `investigate.sys.argv` mutates the interpreter's argv for the rest of the
    session — which is how the first version of this file made eighteen
    `test_git_push` tests read a fixture filename as a command-line flag.

    Restored here in a `finally` rather than through `monkeypatch`, which undoes
    its patches at *teardown*: that is late enough to keep the leak invisible to
    a test in this same file, and the guard below has to be able to see it.
    """
    buf = io.StringIO()
    saved = list(sys.argv)
    try:
        sys.argv = ["investigate.py", filename]
        with redirect_stdout(buf):
            investigate.main()
    finally:
        sys.argv = saved
    return buf.getvalue()


def test_blame_renders_for_a_path_beginning_with_a_dash(repo_with) -> None:
    """The regression: a leading `-` must be a filename, not a blame flag."""
    repo_with("-dash.txt")

    out = _investigate("-dash.txt")

    assert "## Blame hotspots" in out, (
        "blame was refused for a dash-named file — the path reached git as an "
        f"option, not after a `--` separator. Got:\n{out}"
    )
    assert "## Blame: unavailable" not in out


def test_blame_still_renders_for_an_ordinary_path(repo_with) -> None:
    """The separator must not cost the ordinary case anything."""
    repo_with("plain.txt")

    out = _investigate("plain.txt")

    assert "## Blame hotspots" in out
    assert "## Blame: unavailable" not in out


def test_every_git_call_passes_the_path_after_a_separator(repo_with,
                                                          monkeypatch) -> None:
    """Pin the invariant itself, so a future edit to any of the four is caught.

    The dash fixture proves the behaviour; this proves the *reason*, and names
    the offending call rather than leaving a reader to work out which of four
    git invocations produced an empty section.
    """
    repo_with("plain.txt")
    calls: list[list[str]] = []
    real = investigate._git

    def _record(args, *rest, **kwargs):
        calls.append(list(args))
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(investigate, "_git", _record)
    _investigate("plain.txt")

    path_carrying = [c for c in calls if "plain.txt" in c]
    assert path_carrying, "no git call received the path"
    for call in path_carrying:
        assert "--" in call, f"{call[0]!r} passes the path without a separator: {call}"
        assert call.index("--") < call.index("plain.txt"), (
            f"{call[0]!r} passes the path before its separator: {call}"
        )


def test_running_the_op_leaves_argv_alone(repo_with) -> None:
    """This fixture must not change the interpreter's argv for anyone else.

    The first version of this file assigned to `investigate.sys.argv` — the
    real `sys` module — and eighteen `test_git_push` tests then refused a
    fixture filename as an unknown command-line flag. Cross-file damage from a
    test helper is invisible when the file is run alone, which is exactly how
    it reached CI.
    """
    before = list(sys.argv)
    repo_with("plain.txt")

    _investigate("plain.txt")

    assert sys.argv == before
