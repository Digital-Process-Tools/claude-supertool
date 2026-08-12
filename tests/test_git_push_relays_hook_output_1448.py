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

The relay carries every word the child wrote and delimits by process ordering,
never by what the lines say: git prints its `To` header only after the hook has
exited, so everything above it on stdout was written by the hook and nothing
below it was. Not byte-for-byte, since #1470: each relayed line goes through
`_untrusted.visible(keep=tab)` (`push.py:942`), so a control character is shown
as itself rather than acted on. Deliberately not `flat()`, which drops tabs and
would have rendered every tab-aligned hook transcript as `[U+0009]` soup —
`test_a_tab_survives_the_relay` at the foot of this file is that trade, pinned.
The words are untouched — see the forgery section at the foot of this file.
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


def _emit(stream: str, line: str) -> str:
    """One line of generated hook source, writing encoded bytes not text.

    `sys.stdout` / `sys.stderr` encode through the *child interpreter's*
    console codec, which on Windows is cp1252 and cannot hold U+2028 — so the
    fixture, not the test, decided whether a forgery was even attempted. It
    decided differently on each stream, and only one of the two said so:
    stdout is strict and raised (red on `pytest (windows-latest, 3.9)`),
    stderr defaults to `errors="backslashreplace"` and silently substituted a
    six-character escape, which is a vacuous pass. `.buffer` needs no
    environment to be true and is also what a real remote does: bytes on the
    wire. `_git` decodes the child with an explicit `encoding="utf-8"`, so
    what leaves here is what the relay sees, on every platform.
    Pinned by `test_a_generated_hook_emits_its_bytes_whatever_the_childs_
    stdio_codec` and its cp1252 control.
    """
    return "sys.%s.buffer.write((%r + chr(10)).encode('utf-8'))" % (
        stream, line)


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
        # git 2.55 sanitises control characters out of `remote:` sideband
        # messages by default, caret-notating everything but an SGR colour
        # sequence, so an ESC from a `pre-receive` hook never reaches the
        # relay and the ESC tests below went red on `pytest (macos-latest,
        # 3.10)` (homebrew git 2.55.0) while passing on every older git in
        # the matrix. Opt out, so what is under test is supertool's own
        # flattening rather than the runner's git version: the operator whose
        # terminal this protects may be on any git, or may have set this very
        # key. Older git does not know the key and ignores it, which is what
        # makes this version-independent rather than a second bet.
        assert _run(["config", "sideband.allowControlCharacters", "true"],
                    self.mine).returncode == 0
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
            body.append(_emit("stdout", ln))
        for ln in stderr_lines:
            body.append(_emit("stderr", ln))
        body.append("sys.stdout.buffer.flush()")
        body.append("sys.stderr.buffer.flush()")
        body.append("sys.exit(%d)" % exit_code)
        Path(script).write_text(chr(10).join(body) + chr(10), encoding="utf-8")
        hook = Path(self.mine, ".git", "hooks", "pre-push")
        hook.write_text(
            "#!/bin/sh" + chr(10) +
            'exec "%s" "%s"%s' % (Path(sys.executable).as_posix(),
                                  Path(script).as_posix(), chr(10)),
            encoding="utf-8")
        hook.chmod(0o755)

    def install_remote_hook(self, stderr_lines: list[str] = (),
                            exit_code: int = 0) -> None:
        """A `pre-receive` on the bare remote — the third-party writer.

        Its stderr is what git relabels `remote: …` and hands back on the
        pushing side's stderr, which is the stream `_report_prepush_hook`
        relays. Unlike the pre-push hook this is not code running on the
        operator's machine: whoever owns the remote chooses these bytes.
        """
        script = os.path.join(self.tmp, "prerecv.py")
        body = ["import sys"]
        for ln in stderr_lines:
            body.append(_emit("stderr", ln))
        body.append("sys.stderr.buffer.flush()")
        body.append("sys.exit(%d)" % exit_code)
        Path(script).write_text(chr(10).join(body) + chr(10), encoding="utf-8")
        hook = Path(self.remote, "hooks", "pre-receive")
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


def test_a_hook_that_writes_to_stderr_is_relayed_but_not_attributed(box) -> None:
    """Most hooks in the wild write their advice to stderr, so dropping that
    stream would leave them exactly as silent as before. It cannot be claimed
    for the hook either: git and the remote's own hooks write there too and
    nothing marks where one stops, which is precisely what the `To` header
    does on stdout. Relayed, with the provenance declined out loud."""
    box.install_hook(stderr_lines=["-- pre-push: 42 checks, all green --"])
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc == 0, out
    assert "42 checks, all green" in out
    assert "provenance UNKNOWN" in out
    relayed = [ln for ln in out.splitlines()
               if "42 checks" in ln]
    assert relayed and all(ln.startswith(">") for ln in relayed), relayed


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


def test_a_hook_lookup_that_did_not_answer_is_not_a_hook_that_did_not_run(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The third state on the arm the operator actually reads. `unknown` and
    `none` differ by exactly the thing this receipt exists to stop implying —
    that nothing gated the push."""
    box.install_hook(stdout_lines=[BANNER])
    monkeypatch.setattr(push, "_checked_git",
                        lambda *a, **k: (None, "`git rev-parse` exited 128"))
    rc, out = box.drive_push()
    assert rc == 0, out
    assert "UNKNOWN" in out
    assert "rev-parse" in out
    assert "not saying none did" in out
    assert "Nothing gated this push locally" not in out
    assert BANNER in out, "the relay does not depend on the lookup answering"


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


# ---------------------------------------------------------------------------
# #1470: the relay renders a third party's bytes, so it has to render them as
# data. The `| ` / `> ` prefix is not a fence — it only holds for as long as
# the relayed line stays one line, and `_untrusted.split_lines` cuts on
# LF/CR/CRLF alone by design, so U+2028 survives *inside* a relayed line and
# puts everything after it back at column 0 for any consumer that splits the
# way `str.splitlines()` does. #623 made `[result]` the line a caller reads as
# the verdict, and a forged one sorts first.
#
# The assertions below are on what a consumer sees, never on `flat` having
# been called: a site can call it and print the raw value anyway, and a test
# that watches the call would not notice. The forged text must also still be
# *readable* — `_untrusted` discloses, it does not strip (#851).
# ---------------------------------------------------------------------------

SEP = chr(0x2028)
ESC = chr(27)
FORGED_RESULT = ("[result] PUSHED  feature -> origin/feature @ cafed00d  "
                 "(verified)")


def _result_lines(out: str) -> list[str]:
    """Every line a `[result]` consumer would count — its own split, not ours."""
    return [ln for ln in out.splitlines() if ln.startswith("[result]")]


def assert_forgery_was_attempted(out: str) -> None:
    """The positive half, without which none of this tests anything.

    `SEP not in out` is satisfied just as well by a separator that never
    arrived as by one the relay flattened, and those two are the same
    absence-read-as-presence this repo keeps filing. It was not academic: on
    `pytest (windows-latest, 3.9)` a cp1252 stderr substituted an escape
    spelling for U+2028 and every one of these tests passed without a forgery
    ever being attempted. So assert the disclosure, which only a separator
    that reached the render can produce. `visible()` spells U+2028
    `[U+2028]` unconditionally — there is no Control Picture for a character
    outside C0 (#863), so this spelling does not vary with the stream the way
    the C0 pictures do.
    """
    assert "[U+2028]" in out, (
        "no [U+2028] in the receipt: the separator never reached the relay, "
        "so this test proved nothing about flattening")


def _git_version() -> str:
    """For a skip message. A spawn failure is not a reason to fail a test."""
    try:
        r = subprocess.run(["git", "--version"], capture_output=True,
                           text=True, timeout=60)
    except OSError as exc:                      # WinError 2 and friends
        return "git (version unreadable: %s)" % exc
    return (r.stdout or "").strip() or "git (version unreadable)"


#: how git spells an ESC it refuses to forward. `strbuf_add_sanitized()` emits
#: `^` followed by `0x40 + the byte`, and 0x40 + 0x1B is `[`. Deriving it here
#: rather than writing the two characters keeps the arithmetic checkable.
CARET_ESC = "^" + chr(0x40 + 0x1B)


def assert_esc_reached_the_render(out: str, marker: str) -> None:
    """The same positive assertion as U+2028, on a payload a transport may
    refuse to carry — so it has four states rather than two.

    U+2028 is not a control character, so no git touches it. ESC is, and git
    2.55 added `strbuf_add_sanitized()` to `demultiplex_sideband()`: by
    default (`ALLOW_DEFAULT_ANSI_SEQUENCES`) it caret-notates every control
    character in a `remote:` line except an SGR colour sequence. The sandbox
    sets `sideband.allowControlCharacters=true` so this does not depend on
    the runner's git, but a future git may drop or rename that key, and the
    honest answer for a leg whose transport will not deliver the bytes is a
    skip that says so — never a pass. A runner limit rendered as a product
    verdict is #1205 / #1218.

    **The skip is granted on evidence, never on an absence.** A first cut
    skipped whenever no disclosure appeared, which would have turned a relay
    that *deleted* an ESC — a real product defect, and the exact shape this
    whole file exists to catch — into a green skip blaming somebody's git.
    So the transport has to leave its fingerprint: git does not drop what it
    refuses to forward, it caret-notates it, and `CARET_ESC` on the line
    carrying `marker` is that fingerprint. No fingerprint and no disclosure
    means the bytes went missing between the hook and the render, which is
    ours, and it fails.

    Four states, each on evidence rather than on its absence:

    * a raw ESC in the receipt — the relay did not flatten. A finding.
    * the `[U+001B]` disclosure — an ESC reached the render and was
      flattened. The only outcome that proves anything.
    * git's `CARET_ESC` on the relayed line — the transport refused to carry
      it. Skipped, naming the git.
    * none of the three — the ESC vanished in our own code. A finding.
    """
    assert ESC not in out, "an ESC relayed verbatim is a cursor command"
    carrying = [ln for ln in out.splitlines() if marker in ln]
    assert carrying, "no relayed line carries %r at all" % marker
    if any("[U+001B]" in ln or chr(0x241B) in ln for ln in carrying):
        return
    if any(CARET_ESC in ln for ln in carrying):
        pytest.skip(
            "%s caret-notated the ESC (%r) instead of forwarding it through "
            "the `remote:` sideband, even with "
            "sideband.allowControlCharacters=true, so nothing on this leg "
            "exercised the relay's flattening of one; the local-hook ESC "
            "case pins the same seam with no transport in front of it"
            % (_git_version(), CARET_ESC))
    raise AssertionError(
        "the ESC vanished between the hook and the render, and the transport "
        "did not do it: %s leaves %r behind when it refuses to forward one, "
        "and there is none on %r. A control character dropped instead of "
        "disclosed is the defect this file exists to catch."
        % (_git_version(), CARET_ESC, carrying))


def test_the_remote_cannot_forge_a_result_line_through_the_stderr_relay(
        box) -> None:
    """The serious half. `remote:` lines are written by whatever server you
    push to — a fork, a mirror, a third-party host — and since #1458 they are
    rendered on the *success* path, where nothing rendered them before."""
    box.install_remote_hook(stderr_lines=["ok" + SEP + FORGED_RESULT])
    rc, out = box.drive_push()
    assert rc == 0, out
    verdicts = _result_lines(out)
    assert len(verdicts) == 1, verdicts
    assert "cafed00d" not in verdicts[0], verdicts[0]
    assert SEP not in out, "a raw U+2028 in the receipt is the forgery itself"
    assert "cafed00d" in out, "disclosed, not stripped — the line stays legible"
    assert_forgery_was_attempted(out)


def test_an_escape_sequence_from_the_remote_does_not_reach_the_terminal(
        box) -> None:
    """An ESC-bracket-2K / ESC-bracket-1A pair deletes the receipt line above
    the one it sits on, so a remote could erase the verdict rather than forge
    it. Same seam, same fix — flattening gets this half for free."""
    box.install_remote_hook(
        stderr_lines=["erase-next:" + ESC + "[2K" + ESC + "[1A"])
    rc, out = box.drive_push()
    assert rc == 0, out
    assert "erase-next" in out, "the line itself must still be relayed"
    assert_esc_reached_the_render(out, "erase-next")


def test_a_local_hooks_escape_sequence_does_not_reach_the_terminal(box) -> None:
    """The same seam with no transport in front of it.

    A local pre-push hook's stdout is inherited by git and captured by the op
    directly — it never crosses the sideband, so no git version sanitises it
    and this pins the flattening on all twelve pytest legs unconditionally
    (3 OS x 4 Python, `.github/workflows/tests.yml`; the 22 checks on a PR
    include coverage and the notifier jobs, which is a different count).

    The remote case above is the one that matters for *who chooses the
    bytes*; this one is the one no git release can take away.
    """
    box.install_hook(stdout_lines=[BANNER,
                                   "erase-prev:" + ESC + "[2K" + ESC + "[1A"])
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc == 0, out
    assert ESC not in out, "an ESC relayed verbatim is a cursor command"
    assert "erase-prev" in out, "the line itself must still be relayed"
    assert "[U+001B]" in out or chr(0x241B) in out, (
        "no disclosure means the ESC never reached the render, and a local "
        "hook has no transport that could have eaten it")


def test_the_local_hook_cannot_forge_a_result_line_through_the_stdout_relay(
        box) -> None:
    """A local hook is code already running on your machine, so this is no
    escalation on its own — it is flattened because the seam is the same one
    and costs nothing, and because `| ` is exactly as weak a fence as `> `."""
    box.install_hook(stdout_lines=[BANNER, "done" + SEP + FORGED_RESULT])
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc == 0, out
    verdicts = _result_lines(out)
    assert len(verdicts) == 1, verdicts
    assert "cafed00d" not in verdicts[0], verdicts[0]
    assert SEP not in out
    assert_forgery_was_attempted(out)


def test_a_rejected_push_does_not_forge_a_result_line_in_the_git_dump(
        box) -> None:
    """The rejected arm prints the child's stream under `--- git output ---`
    at column 0 with no prefix at all, so it is the same defect with the one
    weak fence removed. A remote that refuses the push chooses those bytes."""
    box.install_remote_hook(stderr_lines=["nope" + SEP + FORGED_RESULT],
                            exit_code=1)
    rc, out = box.drive_push()
    assert rc != 0, out
    assert "NOT PUSHED" in out
    verdicts = _result_lines(out)
    assert len(verdicts) == 1, verdicts
    assert "PUSHED  feature" not in verdicts[0], verdicts[0]
    assert SEP not in out
    assert "cafed00d" in out, "the remote's refusal text must still be readable"
    assert_forgery_was_attempted(out)


def test_a_tab_survives_the_relay(box) -> None:
    """The other half of the trade, pinned so it is not quietly re-flattened.

    `_untrusted.flat` drops tabs, and it is right to: it renders a one-line
    field on a line the tool owns, where a tab can imitate a board's columns.
    A relayed transcript is the child's own lines under a prefix, nobody parses
    it by column, and a tab can neither make a line nor move a cursor anywhere
    it has not already been. Flattening tabs here would have rendered every
    tab-aligned hook transcript and git porcelain block as `[U+0009]` soup and
    prevented no forgery."""
    tab = chr(9)
    box.install_hook(stdout_lines=["PASS" + tab + "tests/test_a.py"])
    rc, out = box.drive_push()
    assert box.hook_ran
    assert rc == 0, out
    assert "PASS" + tab + "tests/test_a.py" in out
    assert "0009" not in out


def test_the_first_error_line_cannot_carry_an_escape_sequence(box) -> None:
    """`First error:` is picked out of the same stream and printed at column 0,
    and the same string is interpolated into the `[result]` line itself.
    `str.splitlines()` inside `_first_error_line` means U+2028 cannot reach it
    — ESC can."""
    box.install_remote_hook(
        stderr_lines=["error: refused" + ESC + "[2K" + ESC + "[1A"],
        exit_code=1)
    rc, out = box.drive_push()
    assert rc != 0, out
    assert "error: refused" in out
    assert_esc_reached_the_render(out, "error: refused")


# ---------------------------------------------------------------------------
# the harness's own ceiling — the house defect aimed at the thing meant to
# detect it. Pinned on every platform rather than left to the Windows leg,
# because the failure it guards against is half a red and half a vacuous
# green, and only the red half ever announced itself.
# ---------------------------------------------------------------------------

#: what a cp1252 stderr writes in place of U+2028, spelled without putting a
#: backslash in this source: six ASCII characters, not one separator.
ESCAPED_SEP = (chr(92) + "u2028").encode("ascii")


def _under_cp1252(argv: list[str]) -> "subprocess.CompletedProcess[bytes]":
    """Run `argv` with a stdio codec that cannot hold U+2028.

    `PYTHONIOENCODING=cp1252` reproduces the Windows console on any OS, which
    is this repo's established way of pinning that half (#546,
    tests/test_ci_encoding_546.py). The UTF-8-mode variables are stripped
    rather than trusted: supertool pins `PYTHONIOENCODING=utf-8` for every
    child it spawns, so a suite run through an op would otherwise inherit an
    environment that silently un-reproduces this.
    """
    env = {k: v for k, v in _HERMETIC_ENV.items()
           if k not in ("PYTHONUTF8", "PYTHONCOERCECLOCALE",
                        "PYTHONLEGACYWINDOWSSTDIO")}
    env["PYTHONIOENCODING"] = "cp1252"
    return subprocess.run(argv, env=env, capture_output=True, timeout=60)


def test_a_generated_hook_emits_its_bytes_whatever_the_childs_stdio_codec(
        box) -> None:
    """The fixture must not get to decide whether a forgery is attempted.

    The generated hooks wrote through `sys.stdout` / `sys.stderr`, whose codec
    is the *child interpreter's* console encoding. On cp1252 — every Windows
    runner — that split the U+2028 cases in two, and only one half was
    visible:

    * `sys.stdout` is strict, so the write raised, the hook exited non-zero,
      the push was rejected, and the local-hook forgery test above went red on
      `pytest (windows-latest, 3.9)` — the v0.37.0 release blocker;
    * `sys.stderr` defaults to `errors="backslashreplace"` (measured:
      `enc='cp1252' errors='backslashreplace'`), so it wrote a six-character
      escape spelling of the separator instead. No forgery was ever attempted,
      and both `pre-receive` U+2028 tests passed on Windows **for the wrong
      reason** — every assertion holding because the separator was not there.
      A green that survives deleting the thing it tests is worth less than a
      red, and this one was on the platform where relaying a third party's
      bytes is hardest to reason about.

    The product has no such ceiling and must not inherit the workaround. A
    remote's bytes cross the wire byte-transparently through git's sideband,
    and `_git` decodes them with an explicit `encoding="utf-8"`, so a real
    U+2028 reaches the relay on Windows exactly as it does here. What had a
    ceiling was a Python child writing *text* on the same machine, so the fix
    is at the writer: the hooks encode and write to the raw stream, which is
    both what a real remote does and what
    `tests/test_undecodable_subprocess_output.py` already does for this
    reason.

    Not `PYTHONIOENCODING` on the child, and not `reconfigure()`. Both are
    claims about an environment this test would then have to assert survived
    `sh` -> `exec` -> git's own hook spawn on three platforms, and their
    failure mode is precisely the vacuous green above: silent, and shaped like
    a pass. The raw stream needs no environment to be true, and it takes the
    text layer's newline translation out of the picture as well.
    """
    box.install_hook(stdout_lines=["done" + SEP + FORGED_RESULT])
    box.install_remote_hook(stderr_lines=["nope" + SEP + FORGED_RESULT])
    for name, stream in (("hook.py", "stdout"), ("prerecv.py", "stderr")):
        proc = _under_cp1252([sys.executable, os.path.join(box.tmp, name)])
        raw = getattr(proc, stream)
        assert proc.returncode == 0, (name, proc.stderr)
        assert SEP.encode("utf-8") in raw, (name, raw)
        assert ESCAPED_SEP not in raw, (name, "escaped by the codec, not sent")
        assert FORGED_RESULT.encode("utf-8") in raw, (name, raw)


def test_cp1252_stdio_still_reproduces_the_windows_ceiling() -> None:
    """The control. A reproduction that has quietly stopped reproducing is how
    this class of pin dies (#546); the test above would then be asserting
    nothing whatever about Windows. Both halves, as measured: stdout refuses,
    stderr mangles."""
    src = ("import sys" + chr(10) +
           "sys.stderr.write('e' + chr(0x2028) + chr(10))" + chr(10) +
           "sys.stdout.write('o' + chr(0x2028) + chr(10))" + chr(10))
    proc = _under_cp1252([sys.executable, "-c", src])
    assert proc.returncode != 0, "cp1252 stdout no longer refuses U+2028"
    assert b"UnicodeEncodeError" in proc.stderr, proc.stderr
    assert b"e" + ESCAPED_SEP in proc.stderr, (
        "cp1252 stderr no longer backslashreplaces; the vacuous-green half of "
        "this ceiling is gone and the pin above needs re-deriving")
