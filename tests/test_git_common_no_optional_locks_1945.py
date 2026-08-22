"""#1945 — `_git`/`_git_verbatim` carry `--no-optional-locks` at their chokepoint.

`presets/git`'s shared runner (`_git_common._git`, and the CR/CRLF-preserving
`_git_verbatim`) is the same mechanism #1944 fixed one file over, and it is
worse here: `subprocess.run(timeout=)`'s `TimeoutExpired` arm is
`process.kill()` -- SIGKILL, no grace, no SIGTERM -- so a stalled `git status`
or `git diff` reached through either of these two functions never gets the
chance to unlink `.git/index.lock` itself. 18+ read-only call sites across
`presets/git/*.py` reach `.git/index.lock`-taking subcommands (`status`,
`diff`, `stash list`) through this one chokepoint.

`--no-optional-locks` is a git global flag and must precede the subcommand.
Adding it once here, rather than at each call site, means a future read-only
call site inherits the protection instead of having to remember it -- and it
is harmless on the write commands (`commit`, `checkout`, `stash push`, `push`,
`merge`) that also run through this chokepoint: it suppresses *optional*
locks only, verified against real git 2.46.2 (`git --no-optional-locks
commit -m x`, `checkout -b`, `stash push` all still work, rc=0).

The timeout arm's `CompletedProcess.args` is what several callers render in a
receipt (the lesson #1939/#1941 already paid for one caller of a sibling
function) -- prepending the flag to `cmd` before it is stored means a reader
retyping that command reproduces what actually ran, the same property #1944
established for the validator's own receipt.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock

PRESET = Path(__file__).parent.parent / "presets" / "git" / "_git_common.py"
_spec = importlib.util.spec_from_file_location("git_common_1945", PRESET)
assert _spec is not None and _spec.loader is not None
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)


def _capture(monkeypatch: "mock.Mock", return_bytes: bool = False) -> list:
    """Patches `subprocess.run` inside the loaded module, returns the calls list."""
    calls: list = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if return_bytes:
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(common.subprocess, "run", _fake_run)
    return calls


def test_git_puts_the_flag_right_after_the_binary(monkeypatch) -> None:
    """MUST NOT FIRE. `_git` is the chokepoint for most of `presets/git`."""
    calls = _capture(monkeypatch)
    common._git(["status", "--porcelain"])
    assert len(calls) == 1, calls
    assert calls[0][0] == "git", calls
    assert calls[0][1] == "--no-optional-locks", calls
    assert calls[0][2:] == ["status", "--porcelain"], calls


def test_git_verbatim_puts_the_flag_right_after_the_binary(monkeypatch) -> None:
    """MUST NOT FIRE. The CR/CRLF-preserving sibling shares the same chokepoint."""
    calls = _capture(monkeypatch, return_bytes=True)
    common._git_verbatim(["diff", "--numstat"])
    assert len(calls) == 1, calls
    assert calls[0][0] == "git", calls
    assert calls[0][1] == "--no-optional-locks", calls
    assert calls[0][2:] == ["diff", "--numstat"], calls


def test_a_real_git_call_still_answers_correctly_with_the_flag() -> None:
    """MUST FIRE. The positive control: unmocked, the flag does not break status."""
    result = common._git(["status", "--porcelain"], timeout=10)
    assert result.returncode == 0, result.stderr


def test_the_timeout_receipt_names_the_flag_that_actually_ran(monkeypatch) -> None:
    """A reader retyping the timed-out command must retype what actually ran.

    #1939/#1941 already had to flatten a sibling function's timeout-recorded
    command; the lesson that applies here is narrower -- the flag has to be
    IN `cmd` before `CompletedProcess(args=cmd, ...)` is built on the
    TimeoutExpired arm, not only on the successful-call arm.
    """
    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(common.subprocess, "run", _timeout)
    result = common._git(["status", "--porcelain"], timeout=1)
    assert result.returncode == common.TIMEOUT_RC, result
    assert result.args[:2] == ["git", "--no-optional-locks"], result.args


def test_the_flag_does_not_block_a_real_write(monkeypatch, tmp_path) -> None:
    """MUST FIRE. `--no-optional-locks` suppresses OPTIONAL locks only.

    A write command (`commit`) reached through the same chokepoint must still
    succeed -- this is the harm-check for the blanket-at-the-chokepoint
    decision over a per-call-site flag.
    """
    import os

    def _run(*args):
        r = subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )
        return r

    _run("init", "-q", "-b", "main")
    _run("config", "user.email", "t@t.com")
    _run("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("x\n")
    _run("add", "f.txt")

    monkeypatch.chdir(tmp_path)
    result = common._git(["commit", "-m", "via _git with the flag"])
    assert result.returncode == 0, result.stderr
