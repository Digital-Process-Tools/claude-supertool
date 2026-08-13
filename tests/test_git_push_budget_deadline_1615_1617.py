"""#1615 + #1617 — two defects at `git-push`'s own argv and clock.

**#1615, `misreports`.** `_push_budget()` was handed to `git push` twice — the
initial attempt and the rebase-recovery re-push — with `_RECOVER_TIMEOUT` for
the fetch and again for the rebase in between, and nothing accounting for the
time already spent. Worst case `2B + 240` against `ops.git-push.timeout = 1920`,
so any `B > 840` could reach supertool's outer cap. Past that cap the process is
killed, on the recovery path, where `_report_recovery_timeout` is the only thing
that would have told the caller their worktree is paused mid-rebase — the exact
outcome `_parse_budget`'s ceiling text says the ceiling exists to prevent (#399).

`:budget=N` is now a **deadline** on this op's pushing, not a per-call timeout.
The clock opens when the first `git push` starts and every call on that phase —
the recovery fetch, the rebase, the re-push — draws from what is left of it. A
call with nothing left is declined rather than spawned, because a `git push`
launched on a clock that has already expired cannot verify anything and the
receipt is the whole product here. What that costs is stated where it is
chosen, in `_open_push_deadline`.

**#1617, `splices`.** `reject_fetch_option` is the chokepoint written for a
value the tool picked up rather than received as an op argument, and it was
called on the fetch sink and in `merge.py` but not before
`push_args += ["-u", remote_name, "HEAD"]`. `_resolve_push_remote` will return a
remote literally named `--receive-pack=<cmd>` — from `git remote add`, or from
`branch.<b>.pushRemote`, which rung 1 takes verbatim by design.

The issue graded its own evidence as resolution-only. It is not: on git 2.46.2
(macOS 15, observed) the exact argv this file builds **executes the payload**.

    $ git push --porcelain -u --receive-pack=/tmp/evil.sh HEAD
    fatal: 'HEAD' does not appear to be a git repository
    $ ls /tmp/PWNED            # written by evil.sh
    /tmp/PWNED

git parses `--receive-pack=` as its own option, which slides `HEAD` into the
repository slot; it then spawns the receive-pack program for that local path
*before* failing to find a repository there. The abort is not a mitigation, it
just happens afterwards. One remote is enough — no second remote and no
steering config, contrary to the issue's caveat. The reproduction is kept as a
test below so the premise cannot rot.

The `:to-upstream` arm (`[remote_name, "HEAD:<ref>"]`) did **not** execute on
the same git: `HEAD:feature` parses as an scp-style ssh URL, so receive-pack
runs on the far side of a hostname that does not resolve. Guarded anyway — the
refusal belongs at the sink, not at whichever argv shape happens to be reachable
this release.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _changelog_findable import assert_change_is_findable  # noqa: E402

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_1615", PRESET)
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
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")


class _Sandbox:
    """Bare remote, `mine` (the clone the op is driven in), and `theirs`.

    `theirs` exists so the remote can be moved ahead without touching `mine`,
    which is what makes `mine`'s next push a genuine non-fast-forward and sends
    it down the rebase-recovery path both issues live on.
    """

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1615_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        self.theirs = os.path.join(self.tmp, "theirs")
        assert _run(["init", "--bare", "-b", "feature", "remote.git"],
                    self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        self.commit("a.txt", "base")
        assert _run(["push", "-u", "origin", "feature"],
                    self.mine).returncode == 0

    def commit(self, name: str, body: str, cwd: str = "") -> None:
        where = cwd or self.mine
        Path(where, name).write_text(body, encoding="utf-8")
        assert _run(["add", name], where).returncode == 0
        assert _run(["commit", "-m", body], where).returncode == 0

    def move_remote_ahead(self) -> None:
        """A commit lands on the remote that `mine` does not have."""
        assert _run(["clone", "-b", "feature", self.remote, "theirs"],
                    self.tmp).returncode == 0
        self.commit("theirs.txt", "theirs", cwd=self.theirs)
        assert _run(["push", "origin", "feature"],
                    self.theirs).returncode == 0

    def drop_remotes(self) -> None:
        for name in _run(["remote"], self.mine).stdout.split():
            assert _run(["remote", "remove", name], self.mine).returncode == 0

    def config(self, key: str, value: str) -> None:
        assert _run(["config", key, value], self.mine).returncode == 0

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


@pytest.fixture(autouse=True)
def _no_tracker(monkeypatch: pytest.MonkeyPatch):
    """No `gh`/`glab` round-trip: this file is about clocks and argv.

    Left as an answered lookup rather than an unknown one, so no test here
    passes on a disclosure line it did not mean to assert about.
    """
    monkeypatch.setattr(push, "_mr_lookup", lambda branch: push.MrLookup(None))


def _verdict(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "no output at all:" + os.linesep + out
    assert lines[-1].startswith("[result] "), (
        "the receipt does not end on a verdict:" + os.linesep + out)
    return lines[-1]


def _clock(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every (git subcommand, timeout) on the pushing phase, in order."""
    seen: list = []
    real = push._git

    def spy(args, **kw):
        if args and args[0] in ("push", "fetch", "rebase"):
            seen.append((args[0], kw.get("timeout")))
        return real(args, **kw)

    monkeypatch.setattr(push, "_git", spy)
    return seen


def _slack(monkeypatch: pytest.MonkeyPatch) -> list:
    """Per pushing-phase call: (subcommand, timeout, seconds of deadline left
    once that timeout has run out).

    A negative slack is the defect: a call handed a clock that runs past the
    instant the whole op promised to be done by. Summing the timeouts instead
    would be the wrong invariant — a first push that returns in 3s legitimately
    leaves 597 of a 600s budget, so the ceilings sum to far more than the
    budget on a run that never came close to it.
    """
    seen: list = []
    real = push._git

    def spy(args, **kw):
        if args and args[0] in ("push", "fetch", "rebase"):
            budget = kw.get("timeout")
            deadline = push._BUDGET["deadline"]
            assert deadline is not None, (
                "a `git " + args[0] + "` was launched with no push deadline "
                "open — the budget is a per-call timeout again")
            seen.append((args[0], budget,
                         round(float(deadline)
                               - (time.monotonic() + float(budget)), 2)))
        return real(args, **kw)

    monkeypatch.setattr(push, "_git", spy)
    return seen


# ---------------------------------------------------------------------------
# #1615 — the budget is a deadline, and the recovery path draws from it
# ---------------------------------------------------------------------------

def test_the_pushing_phase_never_outlasts_the_budget_it_was_given(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole of #1615.

    Every call between the first `git push` and the last is inside the budget
    the caller asked for, so the clocks handed out cannot sum past it. Before
    this, the recovery path spent `2B + 2*_RECOVER_TIMEOUT` on a budget of `B`.

    Asserted on the fetch and the rebase as well as on the two pushes: a fix
    that only shrank the re-push would leave those two spending 240s nobody
    accounted for, which is 120s more than the whole headroom between
    `_PUSH_TIMEOUT_MAX` and `ops.git-push.timeout`.
    """
    box.move_remote_ahead()
    box.commit("mine.txt", "mine")
    seen = _slack(monkeypatch)

    rc, out = box.drive_push("budget=600")

    assert rc == 0, out
    assert [name for name, _, _ in seen] == ["push", "fetch", "rebase", "push"], (
        "this test did not exercise the rebase-recovery path, so it proves "
        "nothing about the second spend: " + repr(seen) + os.linesep + out)
    overruns = [row for row in seen if row[2] < 0]
    assert not overruns, (
        "these calls were handed a clock that runs past the deadline the "
        "600s budget promised — (call, timeout, seconds of overrun): "
        + repr(overruns) + os.linesep + out)


def test_the_recovery_repush_gets_what_is_left_not_a_fresh_budget(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-push's clock is strictly smaller than the first push's.

    Distinct from the sum above: this is the relation that would still be
    wrong if the fetch and the rebase were bounded and the second push were
    handed `_push_budget()` again.
    """
    box.move_remote_ahead()
    box.commit("mine.txt", "mine")
    seen = _clock(monkeypatch)

    rc, out = box.drive_push("budget=600")

    assert rc == 0, out
    pushes = [int(t) for name, t in seen if name == "push"]
    assert len(pushes) == 2, repr(seen) + os.linesep + out
    assert pushes[0] == 600, repr(pushes)
    assert pushes[1] < pushes[0], (
        "the recovery re-push was handed " + str(pushes[1]) + "s after the "
        "first push already spent from the same " + str(pushes[0]) + "s budget")


def test_a_spent_budget_declines_the_repush_instead_of_launching_it(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing left is a third state, not a zero-second push.

    Spawning a `git push` on an expired clock is the worst available outcome
    here: it is killed before it can be verified, and the receipt then has to
    reason about a remote that may or may not have moved — the absence this
    file is mostly about, manufactured by the tool itself.

    Aimed at the re-push arm specifically, not at the recovery generally: the
    rebase has already run and been clean by then, so this is the one decline
    whose verdict has to tell the caller their tree is not where they left it.
    """
    box.move_remote_ahead()
    box.commit("mine.txt", "mine")
    seen = _clock(monkeypatch)
    # Full budget for the fetch and the rebase allowances, nothing left by the
    # time the re-push asks — i.e. the deadline expires during the rebase.
    asked: list = []

    def left() -> int:
        asked.append(1)
        return 0 if len(asked) > 2 else 600

    monkeypatch.setattr(push, "_budget_left", left)

    rc, out = box.drive_push("budget=600")

    assert rc != 0, out
    assert [name for name, _ in seen] == ["push", "fetch", "rebase"], (
        "a push was launched on a budget with nothing left in it: "
        + repr(seen) + os.linesep + out)
    verdict = _verdict(out)
    assert verdict.startswith("[result] NOT PUSHED"), verdict
    assert "budget" in verdict.lower(), verdict
    assert "rebased" in verdict.lower(), (
        "the rebase ran and the verdict does not say so — the caller is told "
        "nothing happened when their branch has been replayed:" + os.linesep
        + verdict)


def test_the_timeout_receipt_names_the_clock_the_push_actually_got(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-push cut short by the deadline must not report the full budget.

    #1530 made this receipt name the budget in force instead of the constant,
    for exactly this reason: a receipt quoting a clock that did not cut sends
    the caller to raise a number that was never reached.
    """
    monkeypatch.setattr(push, "_local_head", lambda: ("a" * 40, ""))
    monkeypatch.setattr(push, "_live_remote_sha", lambda *a, **k: ("b" * 40, ""))
    monkeypatch.setattr(push, "_prepush_hook_state",
                        lambda flags: ("none", "no hook"))
    push._BUDGET["seconds"] = 900
    push._BUDGET["allowed"] = 240
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            push._report_push_timeout("feature", "a" * 40, "origin", "feature",
                                      set())
    finally:
        push._BUDGET["seconds"] = None
        push._BUDGET["allowed"] = None
    out = buf.getvalue()
    assert "240s budget" in out, out
    assert "900s budget" not in out, out
    assert "900s" in out, (
        "the receipt names the shortened clock but never the budget it was "
        "cut from, so the caller cannot tell why it was short:" + os.linesep + out)


def test_the_deadline_is_cleared_between_runs(box) -> None:
    """Module state, like `_RUN` and `_BUDGET['seconds']` (#686).

    A deadline surviving into the next `main()` is a push whose clock expired
    before it started, in a harness that imports once and calls `main()` many
    times — which is every test in this file.
    """
    box.commit("mine.txt", "mine")
    rc, out = box.drive_push()
    assert rc == 0, out
    rc, out = box.drive_push()
    assert rc == 0, out
    assert "already up to date" in _verdict(out).lower(), out


def test_the_ceiling_still_clears_the_op_cap_with_the_recovery_inside_it() -> None:
    """The static half of #1615, alongside the single-push relation (#1530).

    Under a deadline the whole pushing phase — both pushes, the fetch and the
    rebase — costs `_PUSH_TIMEOUT_MAX` at worst rather than
    `2 * _PUSH_TIMEOUT_MAX + 2 * _RECOVER_TIMEOUT`, which is the relation that
    was false before.
    """
    entry = json.loads((ROOT / "presets" / "git.json").read_text(
        encoding="utf-8"))["ops"]["git-push"]
    assert push._PUSH_TIMEOUT_MAX < entry["timeout"], (
        "a caller may ask for _PUSH_TIMEOUT_MAX inside an op capped lower")
    assert push._RECOVER_TIMEOUT < push._PUSH_TIMEOUT, (
        "a recovery call cannot be allowed to outlast the default budget it "
        "is drawn from")


# ---------------------------------------------------------------------------
# #1617 — an option-shaped remote never reaches the argv this file builds
# ---------------------------------------------------------------------------

_EVIL_REMOTE = "--receive-pack=st1617-should-never-run"


def test_an_option_shaped_remote_is_refused_before_the_push(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_resolve_push_remote` takes rung 1 verbatim; the sink has to refuse.

    Driven through `branch.<b>.pushRemote` rather than `git remote add`
    because that is the rung git documents as accepting anything — a URL
    included — so no second-guessing of the value happens on the way here.
    """
    box.drop_remotes()
    box.config("branch.feature.pushRemote", _EVIL_REMOTE)
    box.commit("mine.txt", "mine")
    seen = _clock(monkeypatch)

    rc, out = box.drive_push()

    assert rc != 0, out
    assert seen == [], (
        "an option-shaped remote reached a git call: " + repr(seen)
        + os.linesep + out)
    verdict = _verdict(out)
    assert verdict.startswith("[result] NOT PUSHED"), verdict
    assert "looks like a git option" in out, out
    assert _EVIL_REMOTE in out, (
        "the refusal does not name the value it refused, so the caller cannot "
        "find the config key holding it:" + os.linesep + out)


def test_the_refusal_covers_the_to_upstream_arm_too(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """`:to-upstream` builds `[remote_name, "HEAD:<ref>"]` from `@{upstream}`,
    a remote-tracking ref name whoever controls the remote can choose. It did
    not execute on git 2.46.2 — `HEAD:feature` parses as an ssh URL — but the
    guard belongs at the sink, not at the argv shape that happens to run.
    """
    box.commit("mine.txt", "mine")
    monkeypatch.setattr(push, "_upstream_ref",
                        lambda: (_EVIL_REMOTE + "/other", ""))
    seen = _clock(monkeypatch)

    rc, out = box.drive_push("to-upstream")

    assert rc != 0, out
    assert seen == [], repr(seen) + os.linesep + out
    assert "looks like a git option" in out, out


def test_a_legitimate_dash_free_remote_is_not_refused(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not cost the ordinary push.

    `git clone -o gitlab` is the layout #656 is about, and it is exactly the
    single-remote rung the refusal above travels through.
    """
    box.drop_remotes()
    assert _run(["remote", "add", "gitlab", box.remote],
                box.mine).returncode == 0
    box.commit("mine.txt", "mine")
    seen = _clock(monkeypatch)

    rc, out = box.drive_push()

    assert rc == 0, out
    assert [name for name, _ in seen] == ["push"], repr(seen) + os.linesep + out
    assert "looks like a git option" not in out, out


@pytest.mark.skipif(
    os.name == "nt",
    reason="the payload is a POSIX shell script; the argv shape it proves is "
           "asserted cross-platform by the refusal tests above")
def test_the_option_shaped_remote_really_executes(box) -> None:
    """The issue graded itself resolution-only. It is not — observed here.

    Kept as a test rather than written down in the issue, because the severity
    of the guard above rests on it and a claim about git's own argument parser
    is exactly the kind that goes stale between releases. If a future git stops
    spawning receive-pack for a repository it has not found, this fails and the
    next reader is told the premise moved rather than inheriting it.
    """
    canary = os.path.join(box.tmp, "PWNED")
    payload = os.path.join(box.tmp, "evil.sh")
    Path(payload).write_text(
        "#!/bin/sh" + os.linesep + "touch " + canary + os.linesep,
        encoding="utf-8")
    os.chmod(payload, os.stat(payload).st_mode | stat.S_IEXEC)

    # The exact argv `_push_op` builds for a branch with no upstream.
    r = _run(["push", "--porcelain", "-u", "--receive-pack=" + payload, "HEAD"],
             box.mine)

    assert r.returncode != 0, r.stdout
    assert os.path.exists(canary), (
        "git did not spawn the receive-pack program for the local path it "
        "then failed to find — the mechanism this guard exists for is gone, "
        "and #1617's severity should be re-derived rather than inherited: "
        + (r.stderr or "").strip())


def test_documented() -> None:
    assert_change_is_findable(1615)
    assert_change_is_findable(1617)
