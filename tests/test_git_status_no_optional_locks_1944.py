"""#1944 — every git call `run()` builds must carry `--no-optional-locks`.

`git diff` and `git status` are not pure reads: while scanning they refresh
the in-memory index and write it back, taking `.git/index.lock` to do so.
When a call is killed mid-refresh (the adapter's own budget firing, or a
SIGKILL that outraces the SIGTERM-then-grace `_stop()` gives it — see that
function's own docstring on Windows, where terminate() and kill() are the
same call), git never reaches its own cleanup and a stale 0-byte
`.git/index.lock` wedges every later `git add`/`git commit`/`git stash` in
that repository.

`--no-optional-locks` is a git *global* flag (must precede the subcommand)
that tells git this invocation is a read-only status query, so it skips the
index writeback and the lock is never created — the process can be killed at
any point and leaves nothing behind, unlike relying on `_stop()`'s SIGTERM
cleanup working.

This asserts on the argv `run()` builds — the level the issue itself asks
for — because reproducing a real timeout would be slow and load-dependent
and would prove less. Paired with a "must fire" control: without the fixture
actually driving real git calls, an assertion that a flag is present
vacuously passes on an empty call list.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "git-status" / "git-status.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("git_status_1944", ADAPTER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ImmediateProcess:
    """Answers instantly with empty output — stands in for a real git call."""

    def __init__(self, argv: list) -> None:
        self.args = argv
        self.returncode = 0

    def communicate(self, timeout=None):  # noqa: ANN001
        # rev-parse must answer "true" or main() takes the outside-repo
        # branch and never reaches the diff/status calls this test exists
        # to observe.
        if "rev-parse" in self.args:
            return "true\n", ""
        return "", ""

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout=None):  # noqa: ANN001
        return 0


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "base.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "base.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return tmp_path


def _drive(monkeypatch: pytest.MonkeyPatch, target: Path) -> list:
    """Runs the real adapter, capturing every argv passed to `Popen`."""
    mod = _load()
    calls: list = []

    def _popen(argv, **_kwargs):
        calls.append(list(argv))
        return _ImmediateProcess(list(argv))

    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/git")
    monkeypatch.setattr(mod.sys, "argv", [str(ADAPTER), str(target)])
    emitted: list = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    mod.main()
    assert emitted, "the adapter emitted nothing"
    json.loads(emitted[-1])  # still a well-formed payload
    return calls


def test_every_git_call_carries_no_optional_locks_right_after_the_binary(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT FIRE (the flag missing). The whole issue, on real argv."""
    calls = _drive(monkeypatch, repo / "base.txt")
    for argv in calls:
        assert len(argv) >= 2, argv
        assert argv[1] == "--no-optional-locks", (
            f"--no-optional-locks must be the token immediately after the "
            f"git binary (it is a global flag and must precede the "
            f"subcommand): got {argv}")


def test_the_fixture_actually_drove_all_four_real_subcommands(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. The positive control for the assertion above.

    Without this, a fixture that drives zero calls (a broken harness, an
    adapter that exits before spawning anything) would make the "every call
    carries the flag" assertion vacuously true.
    """
    calls = _drive(monkeypatch, repo / "base.txt")
    subcommands = [argv[2] if len(argv) > 2 else None for argv in calls]
    assert "rev-parse" in subcommands, calls
    assert "status" in subcommands, calls
    assert subcommands.count("diff") == 2, calls
    assert len(calls) == 4, calls


def test_the_flag_survives_into_a_real_stalled_call_receipt(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The timed-out-command receipt must name the command that actually ran.

    #1939/#1941 flattened a *different* adapter's timeout receipt so a
    control character in the rendered argv couldn't corrupt the footer; the
    lesson that applies here is narrower: whatever `_NoAnswer.argv` carries
    is what the decline message names, so if the flag is silently added only
    to the `Popen` call and never to what gets recorded, a reader retyping
    the quoted command would not reproduce the actual invocation.
    """
    mod = _load()

    def _popen(argv, **_kwargs):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/git")
    monkeypatch.setattr(mod.sys, "argv", [str(ADAPTER), str(repo / "base.txt")])
    emitted: list = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    mod.main()
    payload = json.loads(emitted[-1])
    msg = payload["errors"][0]["msg"]
    assert "--no-optional-locks rev-parse --is-inside-work-tree" in msg, msg
