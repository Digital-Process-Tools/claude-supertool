"""#675 — once the push has landed, this op owes the caller a verdict.

`presets/git/push.py` runs every one of its receipt checks through `_git`,
which is a `subprocess.run` with a timeout. A helper that calls it with no
handler turns a `TimeoutExpired` — or an `OSError` — into a stack trace out of
`main()`, *for a push that already succeeded*. The remote moved; the caller
gets a traceback and no `[result]` line, which reads as "the push blew up".
That is #399/#640 wearing a different hat: the receipt exists to say what
happened to the remote, and losing it is losing the answer.

Two things are pinned here, and they are not the same thing:

  * **Per check** — a check that could not run says so *by name*, and the rest
    of the receipt still runs. A guard that returns `""` per helper would stop
    the traceback and re-introduce the house defect, because an empty string
    flows into this receipt as silence, and silence here is a positive claim.
  * **Per receipt** — whatever else fails, exactly one `[result]` line is
    printed and it does not claim a landed push failed. This is the structural
    half: the invariant stops depending on every future call site remembering.

Not trading the loud bug for a quiet one: every decline names the command that
could not run and the reason, and the catch-all prints the exception it caught
in full on stdout rather than swallowing it.

No `_git` is mocked (#649). The failures are real:

  * `PATH` scrubbed to an empty directory — `subprocess.run(["git", …])` then
    raises a genuine `FileNotFoundError` (an `OSError`) from the real call.
    This is the reachable shape of the bug: not a hypothetical clock, an
    interpreter-level failure to start the process at all.
  * an unborn `HEAD` — `git rev-parse HEAD` genuinely exits non-zero while
    `git ls-remote` still answers, which is the only way to see what the
    verdict says when it can read the remote but not the local head.
  * `_CHECK_TIMEOUT` set to 0 for one call, so the post-push checks are killed
    by `subprocess.run`'s own clock. Only the constant is substituted; the
    timeout path executes for real (the #674 technique).

The silent cases are pinned as hard as the disclosures: a healthy push must
still print no `UNKNOWN` at all, or "the check ran and found nothing" stops
being what silence means.
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
_spec = importlib.util.spec_from_file_location("git_push_675", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


_HERMETIC_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Mate",
    "GIT_AUTHOR_EMAIL": "mate@t",
    "GIT_COMMITTER_NAME": "Mate",
    "GIT_COMMITTER_EMAIL": "mate@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60)


def _commit(cwd: str, fname: str, msg: str) -> None:
    Path(cwd, fname).write_text(msg, encoding="utf-8")
    assert _run(["add", fname], cwd).returncode == 0
    assert _run(["commit", "-m", msg], cwd).returncode == 0


class _Sandbox:
    """Bare remote + `mine`, the repo the op is driven in."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st675_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        self.empty_path_dir = os.path.join(self.tmp, "nothing")
        os.makedirs(self.empty_path_dir)

        assert _run(["init", "--bare", "-b", "feature", "remote.git"],
                    self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        _commit(self.mine, "a.txt", "base on remote")
        assert _run(["push", "-u", "origin", "feature"], self.mine).returncode == 0

    # ── setup ────────────────────────────────────────────────────────────

    def add_commit_on_top(self) -> None:
        _commit(self.mine, "d.txt", "more of my own work")

    def unborn_head_repo(self) -> str:
        """A repo whose `rev-parse HEAD` fails while `ls-remote` answers.

        The only mechanism that separates the two halves of the verdict's
        verification claim: the remote is readable, the local head is not.
        """
        d = os.path.join(self.tmp, "unborn")
        assert _run(["init", "-b", "feature", "unborn"], self.tmp).returncode == 0
        assert _run(["remote", "add", "origin", self.remote], d).returncode == 0
        assert _run(["rev-parse", "HEAD"], d).returncode != 0, (
            "HEAD resolves — this test would prove nothing")
        assert _run(["ls-remote", "origin", "feature"], d).stdout.strip(), (
            "ls-remote cannot answer either; the two halves are not separated")
        return d

    # ── observation ──────────────────────────────────────────────────────

    def head(self) -> str:
        return _run(["rev-parse", "HEAD"], self.mine).stdout.strip()

    def remote_tip(self, branch: str = "feature") -> str:
        return _run(["rev-parse", branch], self.remote).stdout.strip()

    def assert_push_landed(self, branch: str = "feature") -> None:
        assert self.remote_tip(branch) == self.head(), (
            "the push did not land; a receipt about a push that never "
            "happened proves nothing")

    def drive_push(self, *argv: str) -> tuple[int, str]:
        return _drive(self.mine, push.main, argv)

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def _drive(cwd: str, fn, argv: tuple[str, ...] = ()) -> tuple[int, str]:
    """Run `fn` with cwd/argv/env set as the op sees them; capture stdout."""
    prev_cwd = os.getcwd()
    prev_argv = sys.argv[:]
    prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
    os.chdir(cwd)
    os.environ.update({k: v for k, v in _HERMETIC_ENV.items() if v is not None})
    sys.argv = ["push.py", *argv]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = fn()
    finally:
        os.chdir(prev_cwd)
        sys.argv = prev_argv
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, buf.getvalue()


@pytest.fixture
def box():
    s = _Sandbox()
    try:
        yield s
    finally:
        s.close()


def _verdict(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "no output at all"
    assert lines[-1].startswith("[result] "), (
        f"the verdict is not the last line (#623/#638):{chr(10)}{out}")
    return lines[-1]


def _scrub_path(monkeypatch, box: _Sandbox) -> None:
    """Make `git` genuinely unavailable to `subprocess.run`.

    Not a mock: the next `_git` call really fails to start a process, which is
    exactly the `OSError` this issue is about. Asserted to have taken effect.
    """
    monkeypatch.setenv("PATH", box.empty_path_dir)
    with pytest.raises(OSError):
        subprocess.run(["git", "--version"], capture_output=True)


# ══ the OSError nobody caught — every post-push helper, one by one ════════

_POST_PUSH_HELPERS = [
    ("_upstream_ref", lambda: push._upstream_ref()),
    ("_remote_sha", lambda: push._remote_sha("origin/feature")),
    ("_local_head", lambda: push._local_head()),
    ("_live_remote_sha", lambda: push._live_remote_sha("origin", "feature")),
    ("_uncommitted_leftovers", lambda: push._uncommitted_leftovers()),
    ("_discarded_by_force", lambda: push._discarded_by_force("HEAD~0")),
    ("_stale_base_advisory", lambda: push._stale_base_advisory("main", "origin")),
]


@pytest.mark.parametrize("name,call",
                         _POST_PUSH_HELPERS,
                         ids=[n for n, _ in _POST_PUSH_HELPERS])
def test_no_post_push_helper_raises_when_git_cannot_be_started(
        box, monkeypatch, name, call) -> None:
    """Every one of these runs *after* the push has landed.

    A raise here costs the caller the entire receipt of a push that succeeded.
    `_live_remote_sha` is in this list on purpose: it is the one helper that
    was already guarded, and it catches `TimeoutExpired` only — so the
    reachable failure, git not starting at all, still takes the process down.
    """
    prev = os.getcwd()
    os.chdir(box.mine)
    try:
        _scrub_path(monkeypatch, box)
        try:
            call()
        except OSError as exc:
            pytest.fail(
                f"#675: {name} let an OSError out — the push landed and the "
                f"caller gets a traceback instead of the receipt: {exc!r}")
    finally:
        os.chdir(prev)


def test_a_check_that_could_not_run_names_itself_rather_than_going_quiet(
        box, monkeypatch) -> None:
    """Not-raising is half the fix; the other half is not going silent.

    `_stale_base_advisory` prints nothing when the base is fresh, so a guard
    that swallows the failure and returns makes "could not check" render
    exactly like "checked, you are up to date" — the house defect this issue
    exists to stop being re-introduced one helper at a time.
    """
    prev = os.getcwd()
    os.chdir(box.mine)
    buf = io.StringIO()
    try:
        _scrub_path(monkeypatch, box)
        with redirect_stdout(buf):
            push._stale_base_advisory("main", "origin")
    finally:
        os.chdir(prev)
    out = buf.getvalue()
    assert "UNKNOWN" in out.upper(), (
        f"#675: the stale-base check could not run and said nothing, which in "
        f"this receipt means 'checked, base is fresh':{chr(10)}{out!r}")
    assert "DID NOT RUN" in out.upper(), (
        f"#675: this check has two failures and they need different words. A "
        f"ref that does not resolve is 'skipped' and `git fetch` fixes it; a "
        f"call that never completed is a check going missing, and fetching is "
        f"not the lever:{chr(10)}{out!r}")
    assert "git fetch" not in out, (
        f"#663: git could not be started at all, and the advice is to run a "
        f"git command — a lever that cannot move:{chr(10)}{out!r}")


def test_the_discard_check_declines_rather_than_raising_on_a_dead_git(
        box, monkeypatch) -> None:
    """The force path's `git log`, which decides whether work was destroyed.

    `_force_aftermath` already knows how to say "DISCARD CHECK DID NOT RUN"
    (#655) — but only for the states `_discarded_by_force` returns. An OSError
    goes straight past that machinery and out of the process, on the one
    operation here that destroys work irrecoverably.
    """
    prev = os.getcwd()
    os.chdir(box.mine)
    try:
        _scrub_path(monkeypatch, box)
        commits, why = push._discarded_by_force("deadbee")
    finally:
        os.chdir(prev)
    assert commits is None, "a check that could not run returned a list"
    assert why, "a decline with no reason is not a decline"


# ══ the quiet bug inside the loud one ═════════════════════════════════════

def test_a_verdict_never_claims_the_remote_differs_from_a_head_it_could_not_read(
        box) -> None:
    """`_local_head()` returning "" is read as a *fact about the remote*.

    `_push_verdict` says "verified, but remote != local HEAD" on the `else` of
    `head and live == head`. Both halves of that conjunction fail the same way,
    so a `rev-parse HEAD` that did not answer is reported as a remote that
    disagrees with local work — a positive claim of divergence, built on an
    absence, in the one line #623 says the caller reads.

    The mechanism is real and separates the two halves: an unborn HEAD makes
    `rev-parse HEAD` exit non-zero while `ls-remote` still answers, so the
    verdict genuinely has a verified remote SHA and no local head.
    """
    repo = box.unborn_head_repo()
    _, out = _drive(repo, lambda: push._push_verdict(
        True, "feature", "origin", "feature", "", "1"))

    assert "verified" in out, (
        f"the remote was readable; the verdict should say so:{chr(10)}{out}")
    assert "remote != local HEAD" not in out, (
        f"#675: `rev-parse HEAD` did not answer and the verdict reports the "
        f"remote as differing from local work — a divergence claim made out of "
        f"an absence:{chr(10)}{out}")
    assert "UNKNOWN" in out.upper() or "could not" in out.lower(), (
        f"#675: the verdict does not say the comparison against local HEAD "
        f"could not be made:{chr(10)}{out}")


# ══ the receipt-level invariant ═══════════════════════════════════════════

def test_an_unforeseen_failure_after_the_push_still_ends_on_a_verdict(
        box, monkeypatch) -> None:
    """The structural half — "whatever else fails".

    This is the one contract that cannot be provoked by a known mechanism: by
    construction it covers the failures nobody enumerated, which is precisely
    the set this file cannot list. So the fault is injected into a post-push
    helper rather than produced by git. `_git` is still not mocked — what is
    simulated here is a *future bug*, which is what the guard is for.

    A per-site `try/except` cannot satisfy this. That is the argument for the
    guard being structural: the next helper added to this receipt is unguarded
    the moment it is written, and this test is what makes that harmless.
    """
    box.add_commit_on_top()

    def boom(*_a, **_k):
        raise RuntimeError("a helper nobody guarded")

    monkeypatch.setattr(push, "_ahead_behind_line", boom)
    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0, (
        f"the push landed; a broken receipt must not report it as a failed "
        f"push:{chr(10)}{out}")
    verdict = _verdict(out)
    assert verdict.startswith("[result] PUSHED"), (
        f"#675: the push landed and the verdict does not say so:{chr(10)}{out}")
    assert "a helper nobody guarded" in out, (
        f"#675: the failure was swallowed — a crash converted into a silently "
        f"degraded receipt is not a fix:{chr(10)}{out}")


def test_a_post_push_timeout_costs_a_check_not_the_receipt(
        box, monkeypatch) -> None:
    """A real `subprocess.run` kill on the checks that follow the push.

    Only `_CHECK_TIMEOUT` is substituted; the calls are really started and
    really killed. The push itself keeps its own budget and lands.
    """
    box.add_commit_on_top()
    monkeypatch.setattr(push, "_CHECK_TIMEOUT", 0)

    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0, f"the push landed:{chr(10)}{out}"
    assert _verdict(out).startswith("[result] PUSHED"), (
        f"#675: post-push checks timed out and the receipt lost its "
        f"verdict:{chr(10)}{out}")
    assert "UNKNOWN" in out.upper(), (
        f"#675: every post-push check was killed and the receipt says nothing "
        f"about it:{chr(10)}{out}")


def test_the_receipt_ends_on_exactly_one_result_line(box) -> None:
    """One verdict, not zero and not two — the channel #623 bought."""
    box.add_commit_on_top()
    rc, out = box.drive_push()

    assert rc == 0
    results = [ln for ln in out.splitlines() if ln.startswith("[result] ")]
    assert len(results) == 1, (
        f"expected exactly one verdict, got {len(results)}:{chr(10)}{out}")


# ══ silence has to keep meaning something ═════════════════════════════════

def test_a_healthy_push_says_nothing_it_does_not_have_to(box) -> None:
    """The half of the contract that rots.

    Every disclosure above is worthless if the receipt now says UNKNOWN on a
    run where all the checks worked: a warning that fires on every push is one
    nobody reads.
    """
    box.add_commit_on_top()
    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0
    assert "UNKNOWN" not in out.upper(), (
        f"a push where every check ran is reporting an unknown:{chr(10)}{out}")
    assert "DID NOT RUN" not in out.upper(), (
        f"a push where every check ran claims a check did not:{chr(10)}{out}")
    assert _verdict(out).startswith("[result] PUSHED"), out

def test_a_crash_after_the_verdict_does_not_print_a_second_one(
        box, monkeypatch) -> None:
    """The catch-all must not double-answer a question already answered.

    `_crash_receipt` only speaks when `_result` has not. Without that guard the
    receipt ends on two `[result]` lines that disagree — the verdict for the
    push that landed, then a crash verdict below it — and a caller reading the
    last line (#623) gets the one that knows least. Nothing in production
    raises after the verdict today; the guard exists for the call added
    tomorrow, which is exactly why it needs a test rather than an argument.
    """
    box.add_commit_on_top()
    real = push._push_verdict

    def verdict_then_boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("a crash below the verdict")

    monkeypatch.setattr(push, "_push_verdict", verdict_then_boom)
    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0, out
    results = [ln for ln in out.splitlines() if ln.startswith("[result] ")]
    assert len(results) == 1, (
        f"#675: the crash guard printed a second, worse-informed verdict "
        f"under the real one:{chr(10)}{out}")
    assert results[0].startswith("[result] PUSHED"), out
    assert "a crash below the verdict" in out, (
        f"the crash was swallowed rather than reported:{chr(10)}{out}")


def test_a_crash_before_the_push_landed_advises_a_command_that_runs(
        box, monkeypatch) -> None:
    """The mid-push verdict names `git ls-remote` — and it has to work.

    Advice is only advice if pulling the lever settles the question. The
    joined `origin/feature` this receipt prints everywhere else is not a
    remote git can resolve, so `git ls-remote origin/feature` fails with
    "does not appear to be a git repository" — a caller told to settle an
    UNKNOWN and handed a command that errors is worse off than one told
    nothing (#663).

    So the command is extracted from the verdict and actually executed, rather
    than matched as a string: a spelling assertion would pass on the broken
    form the day someone rejoins the two halves.
    """
    box.add_commit_on_top()

    def boom(*_a, **_k):
        raise RuntimeError("crashed before we knew what landed")

    monkeypatch.setattr(push, "_note_landed", boom)
    rc, out = box.drive_push()

    assert rc == 1, (
        f"nothing was established about the remote; that is not a "
        f"success:{chr(10)}{out}")
    verdict = _verdict(out)
    assert "UNKNOWN" in verdict, verdict
    assert "settle it: " in verdict, (
        f"an UNKNOWN with no way to settle it is half a receipt:{chr(10)}{verdict}")

    advised = verdict.split("settle it: ", 1)[1].strip()
    assert advised.startswith("git "), advised
    proc = _run(advised.split()[1:], box.mine)
    assert proc.returncode == 0, (
        f"#675/#663: the verdict advises `{advised}`, which does not run:"
        f"{chr(10)}{proc.stderr}")
    assert proc.stdout.strip(), (
        f"`{advised}` ran but answered nothing, so it cannot settle "
        f"anything:{chr(10)}{advised}")

# ══ a decline with no reason is the silent bug in a louder coat ═══════════

_REASON_CARRYING_HELPERS = [
    ("_upstream_ref", lambda: push._upstream_ref()),
    ("_remote_sha", lambda: push._remote_sha("origin/feature")),
    ("_local_head", lambda: push._local_head()),
    ("_live_remote_sha", lambda: push._live_remote_sha("origin", "feature")),
    ("_pushed_commit_count", lambda: push._pushed_commit_count("HEAD~1", "HEAD")),
]


@pytest.mark.parametrize("name,call",
                         _REASON_CARRYING_HELPERS,
                         ids=[n for n, _ in _REASON_CARRYING_HELPERS])
def test_a_helper_that_could_not_ask_returns_no_value_and_says_why(
        box, monkeypatch, name, call) -> None:
    """Not raising is not enough — the empty value has to be distinguishable.

    Every helper here returns `(value, why)`, and every one of them is read by
    a caller that treats the empty value as an *answer*: no upstream
    configured, a ref that does not resolve, a remote that has no such branch.
    A guard that catches the failure and returns a bare `("", "")` stops the
    traceback and hands the receipt a false answer instead — the house defect,
    re-entered through the fix for it.

    `_upstream_ref` is the one with teeth. `_push_op` reads an empty upstream
    as "this branch has none" and pushes `-u origin HEAD`, so a swallowed
    failure here does not merely misreport: it **retargets the branch's
    upstream** on the strength of an answer git never gave (#642).
    """
    prev = os.getcwd()
    os.chdir(box.mine)
    try:
        _scrub_path(monkeypatch, box)
        value, why = call()
    finally:
        os.chdir(prev)
    assert value == "", f"{name} produced a value from a call that never ran: {value!r}"
    assert why, (
        f"#675: {name} could not ask git anything and returned an empty "
        f"reason, which every caller here reads as an answer rather than as "
        f"an absence of one")


def test_a_commit_count_that_could_not_be_read_is_not_printed_as_a_count(
        box, monkeypatch) -> None:
    """`(? commit(s))` is a decline nobody reads as one.

    The old fallback put a literal `?` in the body line and carried it into
    the verdict, where `@ abc1234  (verified, ? commit(s))` reads as a quirk of
    formatting rather than as a check that failed. And it was no fallback at
    all when the call raised instead of exiting non-zero.
    """
    box.add_commit_on_top()
    monkeypatch.setattr(push, "_pushed_commit_count",
                        lambda *_a, **_k: ("", "`git rev-list` did not complete"))
    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0, out
    assert "? commit" not in out, (
        f"#675: the count could not be read and the receipt printed it as a "
        f"count anyway:{chr(10)}{out}")
    assert "UNKNOWN" in out.upper(), (
        f"#675: the count went missing and the receipt does not say "
        f"so:{chr(10)}{out}")
    assert _verdict(out).startswith("[result] PUSHED"), out
