"""#1490: the rebase-recovery route never inherited #1448 or #1454.

`_recover_by_rebase` runs its own `git push` and prints its own receipts, and
neither the pre-push hook disclosure (#1448) nor the head/tail bound on a
relayed git dump (#1454) followed it there. Two instances, one cause: the route
grew its receipts before either of those existed and nothing pulled them in.

Instance 1 turns #1448's own premise back on it: a selective gate whose
selection is invisible is indistinguishable from no gate, and "it pushed fine"
then carries an implied local-green claim it never earned. A push that lands
*after* a rebase is exactly a push whose hook just ran, and it was the one
receipt that said nothing about the hook at all.

Instance 2 is the same route's two `--- git output ---` dumps, unbounded. A push
rejected after a clean rebase is the arm where the transcript is largest - the
hook has just run the suite - and the bound's own commit measured the unbounded
case at ~11,000 lines in a receipt.

Hermetic: a bare "remote", `mine` (the repo under test) and `other` (which moves
the remote ahead so the first push is a non-fast-forward). No network.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_1490", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)

_HERMETIC_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}

BANNER = "-- pre-push: feature branch - suite NOT run here --"


def _emit(stream: str, line: str) -> str:
    """One line of generated hook source, writing encoded bytes not text.

    `.buffer`, for the reason `tests/test_git_push_relays_hook_output_1448.py`
    established: `sys.stdout` encodes through the child interpreter's console
    codec, which on Windows is cp1252, so the fixture rather than the test would
    decide what was emitted.
    """
    return "sys.%s.buffer.write((%r + chr(10)).encode(%r))" % (
        stream, line, "utf-8")


def _run(args: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")


class _Sandbox:
    """A remote that has moved ahead of `mine`, so every push here rebases."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1490_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        self.other = os.path.join(self.tmp, "other")
        self.sentinel = os.path.join(self.tmp, "hook_ran")
        assert _run(["init", "--bare", "remote.git"], self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["config", "sideband.allowControlCharacters", "true"],
                    self.mine).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        self._commit(self.mine, "a.txt", "base")
        assert _run(["push", "-u", "origin", "feature"],
                    self.mine).returncode == 0
        # Somebody else pushes to `feature`, so `mine`'s next push is a
        # non-fast-forward and lands in _recover_by_rebase.
        assert _run(["clone", "-b", "feature", self.remote, "other"],
                    self.tmp).returncode == 0
        self._commit(self.other, "theirs.txt", "their work")
        assert _run(["push", "origin", "feature"], self.other).returncode == 0
        # ... and local work of our own, on a different file so the rebase is
        # clean.
        self._commit(self.mine, "b.txt", "my work")

    def _commit(self, repo: str, fname: str, msg: str) -> None:
        Path(repo, fname).write_text(msg, encoding="utf-8")
        assert _run(["add", fname], repo).returncode == 0
        assert _run(["commit", "-m", msg], repo).returncode == 0

    def install_hook(self, stdout_lines=(), stderr_lines=(),
                     exit_code: int = 0) -> None:
        script = os.path.join(self.tmp, "hook.py")
        body = ["import sys",
                "open(%r, %r).write(%r)" % (self.sentinel, "a", "ran")]
        for ln in stdout_lines:
            body.append(_emit("stdout", ln))
        for ln in stderr_lines:
            body.append(_emit("stderr", ln))
        body.append("sys.stdout.buffer.flush()")
        body.append("sys.stderr.buffer.flush()")
        body.append("sys.exit(%d)" % exit_code)
        Path(script).write_text(chr(10).join(body) + chr(10), encoding="utf-8")
        self._install(Path(self.mine, ".git", "hooks", "pre-push"), script)

    def install_remote_hook(self, stderr_lines=(), exit_code: int = 0) -> None:
        """A `pre-receive` on the bare remote, to reject the re-push."""
        script = os.path.join(self.tmp, "prerecv.py")
        body = ["import sys"]
        for ln in stderr_lines:
            body.append(_emit("stderr", ln))
        body.append("sys.stderr.buffer.flush()")
        body.append("sys.exit(%d)" % exit_code)
        Path(script).write_text(chr(10).join(body) + chr(10), encoding="utf-8")
        self._install(Path(self.remote, "hooks", "pre-receive"), script)

    def _install(self, hook: Path, script: str) -> None:
        interp = Path(sys.executable).as_posix()
        lines = ["#!/bin/sh",
                 'exec "%s" "%s"' % (interp, Path(script).as_posix()), ""]
        hook.write_text(chr(10).join(lines), encoding="utf-8")
        hook.chmod(0o755)

    @property
    def hook_ran(self) -> bool:
        return os.path.exists(self.sentinel)

    def drive_push(self, *argv: str) -> tuple:
        prev_cwd = os.getcwd()
        prev_argv = sys.argv[:]
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.mine)
        os.environ.update({k: v for k, v in _HERMETIC_ENV.items()
                           if v is not None})
        sys.argv = ["push.py", *argv]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = push.main()
        finally:
            os.chdir(prev_cwd)
            sys.argv = prev_argv
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, buf.getvalue()

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture
def box():
    s = _Sandbox()
    try:
        yield s
    finally:
        s.close()


def _took_the_rebase_route(out: str) -> None:
    assert "Rebase clean" in out or "rebased onto remote" in out, out


# -- instance 1: the disclosure follows the route -------------------------

def test_the_rebase_route_relays_the_hooks_own_words(box) -> None:
    """The receipt this route prints is the deliverable, not a helper's."""
    box.install_hook(stdout_lines=[BANNER])
    rc, out = box.drive_push()
    assert rc == 0, out
    assert box.hook_ran, "fixture never spawned the hook"
    _took_the_rebase_route(out)
    assert "Pre-push hook" in out, out
    assert BANNER in out, "the rebase route ate the hook own announcement"


def test_the_rebase_route_says_when_nothing_gated_the_push(box) -> None:
    """`no-verify` on this route is the same three-state claim about
    configuration, and it was the arm with no line at all."""
    box.install_hook(stdout_lines=[BANNER])
    rc, out = box.drive_push("no-verify")
    assert rc == 0, out
    assert not box.hook_ran, "--no-verify was passed; git must not run it"
    _took_the_rebase_route(out)
    assert "Nothing gated this push locally" in out, out
    assert BANNER not in out


def test_the_rebase_route_does_not_report_a_silent_hook_as_no_hook(box) -> None:
    """A hook that ran and printed nothing is a third state here too."""
    box.install_hook()
    rc, out = box.drive_push()
    assert rc == 0, out
    assert box.hook_ran
    _took_the_rebase_route(out)
    assert "printed nothing" in out.lower(), out


def test_a_push_rejected_after_a_clean_rebase_still_names_the_hook(box) -> None:
    """The dump below it carries the child's words; what it cannot say is
    whether a hook was in the picture at all."""
    box.install_hook(stdout_lines=[BANNER])
    box.install_remote_hook(stderr_lines=["nope, protected"], exit_code=1)
    rc, out = box.drive_push()
    assert rc != 0, out
    assert "after a clean rebase" in out, out
    assert "Pre-push hook" in out, out


# -- instance 2: the two dumps on this route are bounded ------------------

def test_the_rejected_after_rebase_dump_is_bounded(box) -> None:
    """The arm where the transcript is largest: the hook has just run the
    suite, and the remote refused anyway."""
    box.install_hook(stdout_lines=["hook line %d" % i for i in range(120)])
    box.install_remote_hook(
        stderr_lines=["remote line %d" % i for i in range(120)], exit_code=1)
    rc, out = box.drive_push()
    assert rc != 0, out
    assert "after a clean rebase" in out, out
    assert "line(s) not shown" in out, out
    dump = out.split("--- git output ---", 1)[1]
    assert len(dump.splitlines()) < 120, len(dump.splitlines())


def test_the_rebase_could_not_start_dump_is_bounded(box, monkeypatch) -> None:
    """The other unbounded dump on this route. Reached with an unstaged change,
    which git refuses to rebase over and which leaves no unmerged paths."""
    # Spied in `_git_common`, not in `push`: the dump moved behind
    # `relayed_block`, which is where the header and the `> ` prefix are now
    # emitted together so neither can be shipped without the other (#1569).
    # The bound is the property this asserts, and it did not change.
    git_common = sys.modules["_git_common"]
    calls = []
    real = git_common.bounded_lines

    def spy(lines, head=git_common.GIT_OUTPUT_HEAD_LINES,
            tail=git_common.GIT_OUTPUT_TAIL_LINES):
        calls.append((head, tail))
        return real(lines, head, tail)

    monkeypatch.setattr(git_common, "bounded_lines", spy)
    Path(box.mine, "a.txt").write_text("dirty", encoding="utf-8")
    rc, out = box.drive_push()
    assert rc != 0, out
    assert "could not start" in out, out
    want = (push._GIT_OUTPUT_HEAD_LINES, push._GIT_OUTPUT_TAIL_LINES)
    assert want in calls, calls
