"""#1669 — three receipt fields of `git-push` that contradict their own `[result]`.

All three were hit live on 2026-08-14 and all three are `misreports`: the push
outcome was correct, the sentence naming *why* was not.

1. On a REJECTED push, `First error:` selected a `^ counts skips carrying that
   token only, ... an `except OSError` arm ...` continuation out of the pre-push
   hook's own SUCCESS disclosure. Measured here: the selector was the bare
   substring `error` matching inside the identifier `OSError` — not proximity
   (the line sat ~200 lines earlier in a passing summary) and not the head/tail
   elision, the two hypotheses the issue asked to have ruled in or out.
2. The same push's `[result]` said `REJECTED`, which reads as the remote
   refusing the ref, one line under a `Hint:` that correctly said the push never
   reached the remote and that a rebase would not help.
3. On a NO-OP push, `Status: pushed OK` sat three lines above
   `[result] NOT PUSHED - already up to date`.

What these pin is the shared property: a field a consumer keys on and the
`[result]` line are derived from one state, so they cannot disagree.

Measured on git 2.46.2 / macOS before choosing the fix:

* a pre-push hook that exits non-zero puts its whole transcript on the push's
  STDOUT and git writes exactly `error: failed to push some refs to '<url>'` to
  STDERR — so "read stderr instead" alone would have replaced a failing
  assertion with git's contentless epilogue.
* an up-to-date push emits `=<TAB>HEAD:refs/heads/x<TAB>[up to date]` on stdout.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).parent.parent
PRESET = _ROOT / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_1669", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)

common = sys.modules["_git_common"]

LF = chr(10)
TAB = chr(9)

#: Verbatim from the receipt on #1669 — the line that was promoted to
#: `First error:`. Its only error keyword is the one inside `OSError`.
DISCLOSURE = (
    "^ counts skips carrying that token only, not every symlink-dependent "
    "test: one held off this runner by an unrelated collection-time marker "
    "(no O_NOFOLLOW, a posix-only class) skips without it, and a symlink call "
    "inside an `except OSError` arm does not skip at all. Full population: "
    "tests/test_symlink_gating_register_1232.py")

#: What the hook printed either side of it, and the transport error that was
#: last, present and unselected.
HOOK_STDOUT = LF.join([
    "-- pre-push: running full test suite (mirrors CI) --",
    "symlink-capability(#1143): 0 of 51 skipped",
    DISCLOSURE,
    "========== 13259 passed, 51 skipped, 15 warnings in 416.00s ==========",
    "OK Tests passed. Pushing.",
    "",
])
TRANSPORT = "Connection to github.com closed by remote host." + LF


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


# ---------------------------------------------------------------------------
# 1. the selector: a substring inside a longer word is not an error keyword
# ---------------------------------------------------------------------------

def test_error_inside_a_longer_word_is_not_an_error_line() -> None:
    """`OSError` is not the word `error`, and this is the measured selector."""
    assert common._first_error_line(DISCLOSURE + LF + "last line") == "last line"


def test_a_real_error_keyword_still_selects() -> None:
    """The narrowing must not cost the field its job."""
    assert common._first_error_line("a" + LF + "error: refused" + LF) == \
        "error: refused"
    assert common._first_error_line("a" + LF + "3 errors found" + LF) == \
        "3 errors found"
    assert common._first_error_line("a" + LF + "fatal: nope" + LF) == \
        "fatal: nope"
    assert common._first_error_line("a" + LF + "1 failed, 2 passed" + LF) == \
        "1 failed, 2 passed"


# ---------------------------------------------------------------------------
# 2. the seam: git's own channel before the hook's transcript
# ---------------------------------------------------------------------------

def test_the_transport_error_beats_a_passing_hooks_disclosure() -> None:
    """#1669's own input. The answer is the last line git printed, on stderr."""
    got = push._push_error_line(HOOK_STDOUT, TRANSPORT)
    assert got == "Connection to github.com closed by remote host.", got


def test_a_failing_suites_assertion_still_wins_over_gits_epilogue() -> None:
    """The preservation constraint the issue names, and the reason `First
    error:` cannot simply read stderr: on a hook-blocked push git's stderr holds
    only `error: failed to push some refs to '<url>'`, which names nobody."""
    hook = LF.join([
        "-- pre-push: running full test suite (mirrors CI) --",
        "E   assert 1 == 2",
        "1 failed, 13258 passed",
        "X Tests failed. Push aborted.",
        "",
    ])
    got = push._push_error_line(hook, "error: failed to push some refs to "
                                      "'git@github.com:o/r.git'" + LF)
    assert "failed to push some refs" not in got, got
    assert got == "1 failed, 13258 passed", got


def test_gits_epilogue_is_still_answered_when_it_is_all_there_is() -> None:
    """Never silence. With nothing else in either stream it is the answer."""
    got = push._push_error_line("", "error: failed to push some refs to "
                                    "'git@github.com:o/r.git'" + LF)
    assert got == "error: failed to push some refs to 'git@github.com:o/r.git'"


def test_a_remote_side_rejection_is_read_below_gits_To_header() -> None:
    """Where git DID reach the remote, its per-ref line is under `To <url>` on
    stdout — below the boundary `_split_hook_stdout` draws — so it is git's
    side, not the hook's, even though both share the stream."""
    stdout = LF.join([
        "-- pre-push: feature branch, suite NOT run here --",
        "To github.com:o/r.git",
        "!" + TAB + "refs/heads/main:refs/heads/main" + TAB
        + "[remote rejected] (protected branch)",
        "Done",
        "",
    ])
    got = push._push_error_line(stdout, "")
    assert "[remote rejected]" in got, got


# ---------------------------------------------------------------------------
# 3. the verdict word: REJECTED means the remote said no
# ---------------------------------------------------------------------------

def _drive(push_stdout: str, push_stderr: str, rc: int = 1):
    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git" + LF, 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("master" + LF, 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/master" + LF, 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc("aaa1111" + LF, 0)
        if args[0] == "push":
            return _proc(push_stdout, rc, push_stderr)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0" + TAB + "0" + LF, 0)
        return _proc("", 0)
    return fake_git


def test_a_push_that_never_reached_the_remote_is_not_REJECTED(capsys) -> None:
    """Instance 2. `[result] ... REJECTED` and the `Hint:` directly under it said
    opposite things, and a reader acting on the verdict rebases against a
    divergence that does not exist. `_ref_status` already knew: git reported no
    per-ref line at all."""
    with mock.patch.object(push, "_git", side_effect=_drive(HOOK_STDOUT, TRANSPORT)), \
         mock.patch.object(push, "_live_remote_sha", return_value=("", "no")), \
         mock.patch.object(push, "_mr_lookup", return_value=push.MrLookup(None)):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    result = [ln for ln in out.split(LF) if ln.startswith("[result]")]
    assert len(result) == 1, out
    assert "REJECTED" not in result[0], result[0]
    assert "STOPPED BEFORE THE REMOTE" in result[0], result[0]
    # and the cause it names is the transport line, not the hook's footnote
    assert "Connection to github.com closed" in result[0], result[0]
    first = [ln for ln in out.split(LF) if ln.startswith("First error:")]
    assert first and "OSError" not in first[0], first


def test_a_real_remote_rejection_keeps_the_word_REJECTED(capsys) -> None:
    """The other side of the same state: git DID report a per-ref rejection."""
    porc = ("To origin" + LF
            + "!" + TAB + "refs/heads/master:refs/heads/master" + TAB
            + "[remote rejected] (protected branch hook declined)" + LF
            + "Done" + LF)
    with mock.patch.object(push, "_git", side_effect=_drive(porc, "")), \
         mock.patch.object(push, "_live_remote_sha", return_value=("", "no")), \
         mock.patch.object(push, "_mr_lookup", return_value=push.MrLookup(None)):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    assert "NOT PUSHED - REJECTED" in out, out
    assert "server-side rule" in out


# ---------------------------------------------------------------------------
# 4. Status: and [result] may not contradict each other
# ---------------------------------------------------------------------------

def _drive_noop():
    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git" + LF, 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat" + LF, 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat" + LF, 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc("aaa1111" + LF, 0)  # unchanged before and after
        if args[0] == "push":
            return _proc("To origin" + LF
                         + "=" + TAB + "refs/heads/feat:refs/heads/feat" + TAB
                         + "[up to date]" + LF + "Done" + LF, 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0" + TAB + "0" + LF, 0)
        return _proc("", 0)
    return fake_git


def test_a_no_op_push_does_not_say_it_pushed(capsys) -> None:
    """Instance 3. `Status:` is a field a consumer greps for — the reporter did
    exactly that and got two flatly contradictory lines, with the sentence that
    reconciles them outside the window."""
    with mock.patch.object(push, "_git", side_effect=_drive_noop()), \
         mock.patch.object(push, "_live_remote_sha", return_value=("", "no")), \
         mock.patch.object(push, "_mr_lookup", return_value=push.MrLookup(None)):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    keyed = [ln for ln in out.split(LF)
             if ln.startswith("Status:") or ln.startswith("[result]")]
    assert len(keyed) == 2, out
    status, result = keyed
    assert "NOT PUSHED" in result, result
    assert "pushed" not in status, (status, result)
    assert "nothing to push" in status, status
    # the body sentence that used to be the only correct one is still there
    assert "Already up to date" in out


def test_a_push_that_moved_the_remote_still_says_pushed(capsys) -> None:
    """The other side, so the fix is not 'never claim a push'."""
    shas = iter(["aaa1111", "bbb2222"])

    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git" + LF, 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat" + LF, 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat" + LF, 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc(next(shas) + LF, 0)
        if args[:2] == ["rev-list", "--count"]:
            return _proc("1" + LF, 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0" + TAB + "0" + LF, 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "_live_remote_sha", return_value=("", "no")), \
         mock.patch.object(push, "_mr_lookup", return_value=push.MrLookup(None)):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Status: pushed" in out, out
    assert "[result] PUSHED" in out, out
