"""#1448: a pre-push hook that says which arm it took, into a void.

`.githooks/pre-push` has four arms and announces every one of them —
`feature branch - suite NOT run here`, `no refs to update`, the full-suite
banner, the non-git-caller fallback. None of those lines reached the operator:
`git-push` captures the child's streams and renders its own receipt, and the
receipt for a 7s push that skipped the suite is the same shape as the receipt
for a 227s push that ran ~9,600 tests.

That is the house defect aimed at our own gate. A selective gate whose
selection is invisible is indistinguishable from no gate at all, and "it pushed
fine" then carries an implied local-green claim it never earned.

**Established rather than assumed, because it decides the fix** (measured
2026-08-12, local bare remote, instrumented hook): a pre-push hook inherits
git's stdout and stderr, and `subprocess` captures both. On the success path
the hook's stdout arrives *above* git's own `To <url>` porcelain header, and
its stderr arrives on stderr. So the output was never lost at capture — it was
held and not rendered. This is a rendering change, and `no-verify` can carry
the same disclosure because that arm is a fact about flags, not about output.

The relay is verbatim and delimited by process ordering, never by what the
lines say: git prints its `To` header only after the hook has exited, so
everything above it on stdout was written by the hook and nothing below it was.
The op therefore reports the hook rather than asserting what the hook did,
which is the distinction #1447 refused to blur when it declined to budget the
hook from its prose.

Hermetic: a bare "remote" plus a working clone in a tmp dir, no network. The
hook is a `#!/bin/sh` shim that execs *this* interpreter on a Python file —
`echo` is a cmd.exe builtin and a bare `python3` can hit the Windows App
Execution Alias, so neither is used. Every test that depends on the hook
asserts it actually ran; a fixture that cannot spawn would otherwise make these
tests pass while testing nothing.
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
_spec = importlib.util.spec_from_file_location("git_push_1448", PRESET)
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


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")


class _Sandbox:
    """Bare remote + `mine`, the repo whose push is under test."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1448_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        self.sentinel = os.path.join(self.tmp, "hook_ran")
        assert _run(["init", "--bare", "remote.git"], self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        self.commit("a.txt", "base")
        assert _run(["push", "-u", "origin", "feature"],
                    self.mine).returncode == 0
        self.commit("b.txt", "local work")

    def commit(self, fname: str, msg: str) -> None:
        Path(self.mine, fname).write_text(msg, encoding="utf-8")
        assert _run(["add", fname], self.mine).returncode == 0
        assert _run(["commit", "-m", msg], self.mine).returncode == 0

    def install_hook(self, stdout_lines: list[str] = (),
                     stderr_lines: list[str] = (), exit_code: int = 0) -> None:
        script = os.path.join(self.tmp, "hook.py")
        body = [
            "import sys",
            "open(%r, 'a').write('ran')" % self.sentinel,
        ]
        for ln in stdout_lines:
            body.append("sys.stdout.write(%r + chr(10))" % ln)
        for ln in stderr_lines:
            body.append("sys.stderr.write(%r + chr(10))" % ln)
        body.append("sys.exit(%d)" % exit_code)
        Path(script).write_text(chr(10).join(body) + chr(10), encoding="utf-8")
        hook = Path(self.mine, ".git", "hooks", "pre-push")
        hook.write_text(
            "#!/bin/sh" + chr(10) +
            'exec "%s" "%s"%s' % (Path(sys.executable).as_posix(),
                                  Path(script).as_posix(), chr(10)),
            encoding="utf-8")
        hook.chmod(0o755)

    @property
    def hook_ran(self) -> bool:
        return os.path.exists(self.sentinel)

    def drive_push(self, *argv: str) -> tuple[int, str]:
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


# ---------------------------------------------------------------------------
# the deliverable: the hook's own words reach the operator
# ---------------------------------------------------------------------------

def test_a_skipping_hook_says_so_on_the_receipt(box) -> None:
    """The 7-second push. The suite was not run and the receipt must say which
    arm decided that — in the hook's words, not in the op's summary of them."""
    box.install_hook(stdout_lines=[BANNER,
                                   "   Force it locally with: PREPUSH_FULL=1"])
    rc, out = box.drive_push()
    assert box.hook_ran, "fixture never spawned the hook"
    assert rc == 0, out
    assert "PUSHED" in out
    assert BANNER in out, "the hook announced its arm and the receipt ate it"
    assert "PREPUSH_FULL=1" in out, "the override the hook named is part of it"


def test_a_hook_that_writes_to_stderr_is_relayed_too(box) -> None:
    """Most hooks in the wild write their advice to stderr. Relaying only
    stdout would leave the same silence for them."""
    box.install_hook(stderr_lines=["-- pre-push: 42 checks, all green --"])
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc == 0, out
    assert "42 checks, all green" in out


def test_gits_own_porcelain_block_is_not_attributed_to_the_hook(box) -> None:
    """The delimiter, pinned. Everything from git's `To` header down is git
    talking; folding it into a section headed 'pre-push hook' would be the op
    inventing provenance, which is the thing this change exists not to do."""
    box.install_hook(stdout_lines=[BANNER])
    _rc, out = box.drive_push()
    lines = out.splitlines()
    hook_idx = next(i for i, ln in enumerate(lines) if BANNER in ln)
    relayed = [ln for i, ln in enumerate(lines)
               if i > hook_idx and ln.lstrip().startswith("|")]
    assert not any("To " in ln or "refs/heads/feature" in ln
                   for ln in relayed), relayed


def test_no_hook_at_all_is_disclosed_as_such(box) -> None:
    """Three states. 'Nothing gated this locally' is a fact the operator needs
    exactly as much as the hook's own lines, and an empty relay does not say
    it — silence reads identically to a hook that printed nothing."""
    rc, out = box.drive_push()
    assert rc == 0, out
    assert not box.hook_ran
    low = out.lower()
    assert "pre-push hook" in low
    assert "no executable pre-push hook" in low


def test_no_verify_says_the_gate_was_skipped_by_the_caller(box) -> None:
    box.install_hook(stdout_lines=[BANNER])
    rc, out = box.drive_push("no-verify")
    assert rc == 0, out
    assert not box.hook_ran, "--no-verify was passed; git must not run it"
    assert "skipped the local hook" in out, out
    assert "Nothing gated this push locally" in out
    assert BANNER not in out, "the hook did not run; nothing of its may appear"


def test_a_silent_hook_is_not_reported_as_an_absent_one(box) -> None:
    """The op ran a gate and the gate said nothing. That is a third state and
    it must not render as 'no hook ran'."""
    box.install_hook()
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc == 0, out
    assert "printed nothing" in out.lower()


def test_a_long_hook_transcript_keeps_its_first_and_last_lines(box) -> None:
    """The master push: ~9,600 tests of pytest output. The arm is announced on
    the hook's FIRST line and its outcome is on the LAST, so a plain tail would
    drop the very disclosure this issue is about. Elision is named, never
    silent."""
    body = ["-- pre-push: running full test suite (mirrors CI) --"]
    body += ["dot line %d" % i for i in range(200)]
    body += ["OK - Tests passed. Pushing."]
    box.install_hook(stdout_lines=body)
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc == 0, out
    assert "running full test suite" in out
    assert "Tests passed. Pushing." in out
    assert "dot line 100" not in out, "the middle must be elided"
    assert "not shown" in out, "an elision nobody is told about is a truncation"


def test_a_blocked_push_does_not_dump_the_whole_suite_transcript(box) -> None:
    """Adjacent to #1448 and measured by the same probe. When the hook REFUSES,
    `--- git output ---` printed the child's entire output verbatim — and on
    the master arm that is a full pytest run: 11,449 items, ~11,000 lines,
    into a receipt whose job is to carry the one reason the push was refused.

    Bounded the same way and for the same reason as the relay, with more room
    because this is the arm a reader has to act on: the refusal is announced at
    the top and the failing assertions are at the bottom.
    """
    body = ["-- pre-push: running full test suite (mirrors CI) --"]
    body += ["dot line %d" % i for i in range(400)]
    body += ["X Tests failed. Push aborted."]
    box.install_hook(stdout_lines=body, exit_code=1)
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc != 0, out
    assert "NOT PUSHED - REJECTED" in out
    assert "running full test suite" in out, "the arm it took must survive"
    assert "Tests failed. Push aborted." in out, "the refusal must survive"
    assert "dot line 200" not in out, "the middle must be elided"
    assert "not shown" in out
    assert len(out.splitlines()) < 200, "a receipt, not a transcript"


# ---------------------------------------------------------------------------
# the one arm that is plumbing, not rendering
# ---------------------------------------------------------------------------

def test_a_timeout_says_the_hooks_words_were_never_captured(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay is a rendering change on every arm but this one. `_git` kills
    the child on timeout and returns no captured output, so the hook's lines
    are gone before the receipt is built — and after this change an empty relay
    reads as a silent hook everywhere else. The absence is therefore named.

    Everything #1242 and #1447 put here survives it: this asserts only the
    added sentence, and the receipt's own invariants are pinned next door in
    tests/test_git_push_timeout_names_the_hook_1242.py.
    """
    monkeypatch.setattr(push, "_local_head", lambda: ("a" * 40, ""))
    monkeypatch.setattr(push, "_live_remote_sha", lambda *a, **k: ("b" * 40, ""))
    monkeypatch.setattr(push, "_prepush_hook_state",
                        lambda flags: ("runs", ".git/hooks/pre-push"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = push._report_push_timeout("fix/1", "c" * 40, "origin",
                                       "refs/heads/fix/1", set())
    out = buf.getvalue()
    assert rc == 1
    assert "NOT part of this receipt" in out
    assert "not a hook that stayed silent" in out
    assert "do NOT force-push" in out, "the #1242 receipt must be untouched"


def test_a_timeout_with_no_hook_does_not_apologise_for_a_missing_relay(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`none` is a settled answer: there were no hook lines to lose."""
    monkeypatch.setattr(push, "_local_head", lambda: ("a" * 40, ""))
    monkeypatch.setattr(push, "_live_remote_sha", lambda *a, **k: ("b" * 40, ""))
    monkeypatch.setattr(push, "_prepush_hook_state",
                        lambda flags: ("none", "no executable pre-push hook"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        push._report_push_timeout("fix/1", "c" * 40, "origin",
                                  "refs/heads/fix/1", set())
    assert "NOT part of this receipt" not in buf.getvalue()


# ---------------------------------------------------------------------------
# the delimiter, as a unit
# ---------------------------------------------------------------------------

def test_split_takes_the_last_to_header_not_the_first() -> None:
    """A hook line may begin with the word `To`. git prints exactly one `To`
    header and prints it after the hook has exited, so scanning from the end is
    what makes hook prose unable to move the boundary."""
    tab = chr(9)
    stdout = (chr(10).join([
        "To do: nothing",
        "still the hook",
        "To /tmp/remote.git",
        "*" + tab + "refs/heads/f:refs/heads/f" + tab + "[new branch]",
        "Done",
    ]) + chr(10))
    lines, delimited = push._split_hook_stdout(stdout)
    assert delimited is True
    assert lines == ["To do: nothing", "still the hook"]


def test_no_to_header_means_the_boundary_is_unknown() -> None:
    """A hook that blocks the push: git never reaches the remote and prints no
    header, so there is nothing to delimit against. `False` is what makes the
    caller say so instead of claiming the whole stream for the hook."""
    lines, delimited = push._split_hook_stdout("something" + chr(10))
    assert delimited is False
    assert lines == ["something"]
