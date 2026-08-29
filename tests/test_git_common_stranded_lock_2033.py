"""#2033 — `_git`/`_git_verbatim`'s timeout arm still SIGKILLs a WRITE
command, so `.git/index.lock` is stranded exactly as #1944/#1945 described —
the flag only closed the read half.

`presets/git/_git_common.py`'s `_git` and `_git_verbatim` are the chokepoint
most of `presets/git` reaches git through. Their `TimeoutExpired` arm used to
be `subprocess.run(timeout=budget)`, and `subprocess.run`'s own timeout
handling calls `Popen.kill()` -- SIGKILL, no grace -- *before* it ever raises
`TimeoutExpired`, so there was never a point at which either function could
interpose a SIGTERM. `--no-optional-locks` (#1945) closed this for the
read-only calls this chokepoint also makes, and says so in its own comment: it
is a documented no-op on the write commands (`commit`, `checkout`, `stash
push`, `push`, `merge`) this same chokepoint runs, because it suppresses
*optional* locks only. Those still take `.git/index.lock` for real, so a
stalled write left the lock stranded, wedging every later `git add`/`commit`/
`checkout`/`stash` in that repository until a human deleted the file by hand.

The fix is the same `_stop()`-shaped arm `validators/git-status/git-status.py`
already carries for its own git calls (#1882): drive `Popen` directly instead
of `subprocess.run(timeout=)`, so `TimeoutExpired` from `communicate()` can be
answered with SIGTERM, a bounded grace, then SIGKILL. `TIMEOUT_RC = 124` and
the `"timed out after {budget}s"` stderr are unchanged, so no caller of
`_git`/`_git_verbatim` needs to change.

Both `must fire` and `must not fire` are driven end to end against a real
child process standing in for git (a shell script on PATH named `git`),
mirroring `tests/test_git_status_validator_timeout_1882.py`'s own pair, so a
stall that reports as a decline and a harness that never launched anything
stay distinguishable. `test_stop_sends_terminate_then_kill_only_if_unsettled`
and `test_settled_does_not_read_a_broken_pipe_as_a_dead_child` are the
in-process half: they prove the code calls the right functions in the right
order; the real-child tests above prove a real process actually gets the
chance to clean up, which a patched signal call cannot demonstrate on its own.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "_git_common.py"


def _load():
    spec = importlib.util.spec_from_file_location("git_common_2033", PRESET)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shim_dir(tmp_path: Path, script: str) -> Path:
    """A directory holding an executable named `git`, put first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(script, encoding="utf-8")
    shim.chmod(0o755)
    return bindir


# ---------------------------------------------------------------------------
# End to end, against a real child process (POSIX signals only)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="POSIX signals: Windows terminate() and kill() "
                           "are both TerminateProcess, so there is no grace "
                           "period to observe and nothing here to assert -- "
                           "see _stop's own docstring for the OBSERVED/"
                           "REASONED split on that platform")
def test_a_real_stalled_write_traps_sigterm_and_removes_its_lock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. The grace period is what lets a real write clean up."""
    mod = _load()
    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    marker = tmp_path / "cleaned"
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "trap 'rm -f " + str(lock) + "; echo term > " + str(marker) + "; exit 0' TERM\n"
        "while true; do sleep 0.1; done\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    started = time.monotonic()
    result = mod._git(["commit", "-m", "stalled"], timeout=1)
    elapsed = time.monotonic() - started

    assert result.returncode == mod.TIMEOUT_RC, result
    assert elapsed < 1 + mod._TERM_GRACE_S + 3, (
        "took " + str(elapsed) + "s -- the grace period is meant to be bounded")
    assert marker.exists(), "the shim never saw SIGTERM -- it was SIGKILLed"
    assert not lock.exists(), "the child was not given time to release its lock"


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="POSIX signals: see the sibling test above")
def test_a_child_that_ignores_sigterm_is_still_killed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT HANG. The grace period is bounded, not a promise to wait."""
    mod = _load()
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "while true; do sleep 0.1; done\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    started = time.monotonic()
    result = mod._git(["commit", "-m", "stalled"], timeout=1)
    elapsed = time.monotonic() - started

    assert result.returncode == mod.TIMEOUT_RC, result
    assert elapsed < 1 + mod._TERM_GRACE_S + 3, (
        "took " + str(elapsed) + "s -- a deaf child must still be killed "
        "inside a bounded wait, not hung on forever")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="see above")
def test_git_verbatim_also_sends_sigterm_before_kill(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. `_git_verbatim` is the same chokepoint with translation off."""
    mod = _load()
    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    marker = tmp_path / "cleaned"
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "trap 'rm -f " + str(lock) + "; echo term > " + str(marker) + "; exit 0' TERM\n"
        "while true; do sleep 0.1; done\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    result = mod._git_verbatim(["commit", "-m", "stalled"], timeout=1)
    assert result.returncode == mod.TIMEOUT_RC, result
    assert marker.exists(), "the shim never saw SIGTERM -- it was SIGKILLed"
    assert not lock.exists(), "the child was not given time to release its lock"


def test_a_real_clean_write_is_unaffected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. The positive control: a real, well-behaved write still works."""
    mod = _load()

    def _run(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )

    _run("init", "-q", "-b", "main")
    _run("config", "user.email", "t@t.com")
    _run("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _run("add", "f.txt")

    monkeypatch.chdir(tmp_path)
    result = mod._git(["commit", "-m", "via _git, unstalled"], timeout=10)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# In process: `_stop`/`_settled` on their own
# ---------------------------------------------------------------------------

def test_stop_sends_terminate_then_kill_only_if_unsettled() -> None:
    """`_stop`'s own escalation order, mocked."""
    mod = _load()

    class _Proc:
        def __init__(self, settles_on_terminate: bool) -> None:
            self.calls: list = []
            self._settles = settles_on_terminate

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

        def communicate(self, timeout=None):
            if "terminate" in self.calls and self._settles:
                return "", ""
            if "kill" in self.calls:
                return "", ""
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=timeout or 0)

        def wait(self, timeout=None):
            return 0

    settles = _Proc(settles_on_terminate=True)
    mod._stop(settles)
    assert settles.calls == ["terminate"], (
        "a child that settles after SIGTERM must not also be sent SIGKILL: "
        + str(settles.calls))

    deaf = _Proc(settles_on_terminate=False)
    mod._stop(deaf)
    assert deaf.calls == ["terminate", "kill"], (
        "a deaf child must be escalated to SIGKILL, in that order: "
        + str(deaf.calls))


def test_settled_does_not_read_a_broken_pipe_as_a_dead_child() -> None:
    """communicate() failing is a statement about the pipes, not the child
    (#1888/#1912's lesson, paid for once in `validators/git-status` and
    reused rather than re-derived here).
    """
    mod = _load()

    class _BrokenPipeProc:
        def __init__(self) -> None:
            self.signals: list = []

        def communicate(self, timeout=None):
            raise OSError(9, "Bad file descriptor")

        def wait(self, timeout=None):
            if "kill" in self.signals:
                return -9
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=timeout or 0)

        def terminate(self):
            self.signals.append("terminate")

        def kill(self):
            self.signals.append("kill")

    live = _BrokenPipeProc()
    assert mod._settled(live, 0) is False, "a live child read as settled"

    gone = _BrokenPipeProc()
    gone.signals.append("kill")
    assert mod._settled(gone, 0) is True, "a dead child read as still running"


def test_a_communicate_oserror_still_reaches_stop_not_a_silent_leak(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. A broken pipe on the first call must not skip `_stop`
    entirely -- that would leave a live git holding the lock, the exact bug
    #1888/#1912 fixed for `validators/git-status`.
    """
    mod = _load()

    class _Proc:
        def __init__(self, cmd) -> None:
            self.args = cmd
            self.returncode = None
            self.calls: list = []

        def communicate(self, timeout=None):
            if "kill" in self.calls or "terminate" in self.calls:
                return "", ""
            raise OSError(9, "Bad file descriptor")

        def wait(self, timeout=None):
            if self.calls:
                return 0
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

    created: list = []

    def _fake_popen(cmd, **kw):
        proc = _Proc(cmd)
        created.append(proc)
        return proc

    monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)
    result = mod._git(["status", "--porcelain"], timeout=1)

    assert result.returncode == mod.TIMEOUT_RC, result
    assert created and created[0].calls, (
        "a broken pipe on the first communicate() must still reach _stop() -- "
        "reading it as \'the child is gone\' is the exact #1888/#1912 leak")


def test_a_communicate_error_with_no_message_still_says_something(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. `exc or "no reason given"` tests the exception OBJECT's
    truthiness, which is always True and never falls back -- `str(exc) or
    "no reason given"` is what `validators/git-status/git-status.py` copied
    this arm from actually writes, and reads `str(exc)`, which IS empty for
    a bare `OSError()`. A stderr that silently drops to "communicate()
    failed: OSError - " (trailing blank) is a smaller instance of the exact
    defect this whole issue is about: an absence read as an answer.
    """
    mod = _load()

    class _BlankErrorProc:
        def __init__(self, cmd) -> None:
            self.args = cmd
            self.returncode = None
            self.calls: list = []

        def communicate(self, timeout=None):
            if self.calls:
                return "", ""
            raise OSError()

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, **kw: _BlankErrorProc(cmd))
    result = mod._git(["status", "--porcelain"], timeout=1)

    assert result.returncode == mod.TIMEOUT_RC, result
    assert "no reason given" in result.stderr, (
        "an exception with no message must still say so explicitly, not "
        "trail off into an empty string a reader cannot distinguish from a "
        "truncated line: " + repr(result.stderr))
