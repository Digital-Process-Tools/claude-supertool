"""Three v0.42.0 audit findings in `presets/git/push.py`, all about a number
this op decides on the caller's behalf.

**#1659 — the two budget paths validated against different ceilings.** #1631 made
`ops.git-push.timeout` a value a repository sets, and taught `_config_budget` to
check `ops.git-push.budget` against the merged entry. `_parse_budget` was left
checking `:budget=SECONDS` against the module constant alone, so a project that
lowered its op timeout to 60s could still ask the flag for 1700s. Core then kills
the child at `subprocess.run(timeout=60)` — the #399 outcome the budget exists to
prevent, and on the recovery path it costs the receipt that would have said the
worktree is paused mid-rebase (#1615).

The two ceilings are both real and neither is redundant, which is the thing to
state rather than delete:

* `ops.git-push.timeout` is where **supertool** kills the process. Past it there
  is no receipt, so a budget at or above it buys a `FAIL (timeout)` and no
  verdict.
* `_PUSH_TIMEOUT_MAX` is the longest **the caller** is made to wait. A project
  may raise its op timeout to two hours; that is a statement about the process
  cap, not permission to block whoever typed `git-push` for two hours.

So the effective ceiling is the smaller of the two, on both paths, from one
implementation — `_budget_ceiling_refusal`. Two implementations of one rule is
what produced this.

**#1647 — the #1617 chokepoint comment claimed an invariant the code lacked.** It
said `reject_fetch_option` is called "before the argv is built rather than inside
each arm, so a future arm cannot reintroduce the hole by forgetting". It was not:
`_remote_sha(upstream)` ran `git rev-parse --short <upstream>` 33 lines above the
guard, spending the same `@{upstream}`-derived value the guard exists to refuse.

Not exploitable on `rev-parse`, which spawns no helper. Fixed by moving the guard
above its first consumer rather than by weakening the sentence: the property the
comment states is the one that makes it safe to add an arm without re-reading the
function, and it is cheaper to make it true than to ask every future contributor
to remember that it is not.

**#1649 — the recovery fetch could be launched on an allowance nothing completes
in.** `_recover_allowance` returns `min(_RECOVER_TIMEOUT, _budget_left())` since
#1615, and `if not fetch_budget` declined only at exactly zero. A push that spent
295s of 300 and was then rejected non-fast-forward launched the fetch on 5s, timed
out, and reported `fetch TIMED OUT (5s)` — true about the fetch, false about the
cause, since nothing was ever going to finish.

**Where the floor comes from, measured rather than picked.** `_CHECK_TIMEOUT` is
30s, and it is what this file already gives `git ls-remote` — one network
round-trip to this same remote, ref advertisement and nothing else. A recovery
fetch is that round-trip plus negotiation plus a packfile, so it cannot need less.
Measured against github.com from a fast link on 2026-08-14: an up-to-date
`git fetch origin master` took 1.77-2.02s, and one that actually transferred
(`--depth 200` onto a `--depth 1` clone) took 4.33s. 5s is inside the noise of the
*good* case and nowhere near a bad one; 30s is this file's own existing statement
of what a bad one is worth.

Declining is the third state, not a clamp (docs/validators.md §"Declining instead
of guessing"): the receipt says how many seconds were left, what the minimum is,
and that a retry gets a fresh budget.
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

sys.path.insert(0, str(Path(__file__).parent))
from _changelog_findable import assert_change_is_findable  # noqa: E402

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_1659", PRESET)
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
    """Bare remote, `mine` (the clone the op is driven in), and `theirs`."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1659_")
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
        assert _run(["clone", "-b", "feature", self.remote, "theirs"],
                    self.tmp).returncode == 0
        self.commit("theirs.txt", "theirs", cwd=self.theirs)
        assert _run(["push", "origin", "feature"],
                    self.theirs).returncode == 0

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
    monkeypatch.setattr(push, "_mr_lookup", lambda branch: push.MrLookup(None))


@pytest.fixture(autouse=True)
def _reset_budget():
    """Module state, reset in `main()`'s prologue; several tests here reach
    past `main()`, so it is reset around every one."""
    def _clear() -> None:
        push._BUDGET["seconds"] = None
        push._BUDGET["deadline"] = None
        push._BUDGET["allowed"] = None
        push._BUDGET["source"] = ""
    _clear()
    yield
    _clear()


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch):
    """Pin the merged op entry both budget paths read."""
    def _set(**keys: object) -> None:
        monkeypatch.setattr(push, "_merged_op_entry", lambda: dict(keys))
    return _set


def _verdict(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "no output at all:" + os.linesep + out
    assert lines[-1].startswith("[result] "), (
        "the receipt does not end on a verdict:" + os.linesep + out)
    return lines[-1]


# ---------------------------------------------------------------------------
# #1659 — one ceiling rule, both paths
# ---------------------------------------------------------------------------

def test_the_flag_is_refused_against_a_lowered_op_timeout(entry) -> None:
    """The reproduction from the issue, as a test.

    A project that writes `ops.git-push.timeout: 60` has said where supertool
    kills this process. A `:budget=1700` accepted under it is a promise of 1700
    seconds against a clock that cuts at 60.
    """
    entry(timeout=60)

    seconds, why = push._parse_budget(["budget=1700"])

    assert seconds is None, (
        "the flag path accepted a budget 28x its own op cap; the config path "
        "refuses the same number")
    assert "ops.git-push.timeout" in why, why
    assert "60" in why and "1700" in why, (
        "the refusal has to name both numbers, or the caller cannot tell "
        "which one to move: " + why)


def test_both_paths_refuse_the_same_number_for_the_same_reason(entry) -> None:
    """The invariant, stated as the equality it is.

    `tests/test_git_push_budget_from_config_1631.py` already asserts
    `test_a_project_timeout_override_is_the_one_the_budget_is_checked_against`
    of the config path. That name is true of the op or of neither half of it.
    """
    entry(timeout=600)

    flag_seconds, flag_why = push._parse_budget(["budget=900"])
    entry(timeout=600, budget=900)
    config_seconds, config_why = push._config_budget()

    assert (flag_seconds is None) == (config_seconds is None) is True
    assert "600" in flag_why and "600" in config_why, (flag_why, config_why)


def test_a_flag_under_the_project_op_timeout_is_still_taken(entry) -> None:
    """The guard must not cost the ordinary raise, which is what it is for."""
    entry(timeout=600)

    assert push._parse_budget(["budget=599"]) == (599, "")


def test_the_module_ceiling_survives_a_project_that_raises_the_op_timeout(
        entry) -> None:
    """What `_PUSH_TIMEOUT_MAX` is *for*, once the op timeout is settable.

    It is not a weaker copy of the process cap and it does not become dead when
    the flag starts honouring that cap. It is the longest this op makes a
    caller wait, and a repository raising its own `timeout` has said nothing
    about that.
    """
    entry(timeout=7200)
    over = push._PUSH_TIMEOUT_MAX + 1

    seconds, why = push._parse_budget([f"budget={over}"])
    assert seconds is None
    assert str(push._PUSH_TIMEOUT_MAX) in why, why

    entry(timeout=7200, budget=over)
    seconds, why = push._config_budget()
    assert seconds is None
    assert str(push._PUSH_TIMEOUT_MAX) in why, why


def test_an_unreadable_op_timeout_refuses_the_flag_as_it_refuses_the_key(
        entry) -> None:
    """Refused rather than assumed safe, on both paths (#1631's own wording).

    A budget that cannot be checked against the cap that will kill it is not a
    budget known to be under it.
    """
    entry(timeout="soon")

    seconds, why = push._parse_budget(["budget=100"])
    assert seconds is None
    assert "ops.git-push.timeout" in why, why


def test_the_refusal_text_no_longer_points_at_presets_git_json(entry) -> None:
    """The prose #1631 falsified and did not update.

    The old text told the caller the ceiling "has to stay strictly under
    ops.git-push.timeout **in presets/git.json**" — a claim about a value a
    repository now overrides, sending whoever reads it to edit the shipped
    preset instead of their own config.
    """
    entry(timeout=push._PUSH_TIMEOUT_MAX + 500)
    _seconds, why = push._parse_budget(
        [f"budget={push._PUSH_TIMEOUT_MAX + 1}"])

    assert "presets/git.json" not in why, why
    assert "ops.git-push.timeout" in why, why


# ---------------------------------------------------------------------------
# #1647 — the guard runs before anything spends the value
# ---------------------------------------------------------------------------

_EVIL_UPSTREAM = "--upload-pack=st1647-should-never-run"


def _all_git(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every `git` argv this run launches, in order.

    Wider than `_clock` in test_git_push_budget_deadline_1615_1617.py on
    purpose: that spy watches `push`/`fetch`/`rebase` only, which is why a
    `rev-parse --short <upstream>` above the guard went unseen by the tests
    that were written to pin the guard.
    """
    seen: list = []
    real = push._git

    def spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(push, "_git", spy)
    return seen


def test_no_git_call_spends_an_option_shaped_upstream_before_the_guard(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The chokepoint comment's own claim, as an assertion.

    `_remote_sha(upstream)` sits above `reject_fetch_option` and hands the raw
    `@{upstream}` string to `git rev-parse --short`. Harmless on `rev-parse`,
    which spawns no helper — and precisely the pre-guard slot the comment says
    a new arm cannot land in.
    """
    box.commit("mine.txt", "mine")
    monkeypatch.setattr(push, "_upstream_ref",
                        lambda: (_EVIL_UPSTREAM + "/feature", ""))
    seen = _all_git(monkeypatch)

    rc, out = box.drive_push("to-upstream")

    assert rc != 0, out
    spent = [argv for argv in seen
             if any(_EVIL_UPSTREAM in str(tok) for tok in argv)]
    assert spent == [], (
        "a git call spent the untrusted upstream before the guard refused "
        "it: " + repr(spent) + os.linesep + out)
    assert "looks like a git option" in out, out


# ---------------------------------------------------------------------------
# #1649 — an allowance nothing completes in is declined, not launched
# ---------------------------------------------------------------------------

def test_a_sub_minimum_remainder_is_declined_rather_than_handed_to_git(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """5s is not a budget, it is a timeout with extra steps."""
    monkeypatch.setattr(push, "_budget_left", lambda: 5)

    assert push._recover_allowance() == 0, (
        "the fetch would be launched on 5s and reported as having timed out")


def test_the_floor_is_the_allowance_this_file_gives_one_round_trip() -> None:
    """The number is derived, not picked (#1649 asked for exactly this).

    `_CHECK_TIMEOUT` is what `_live_remote_sha` gets for a `git ls-remote` —
    one network round-trip to this remote, ref advertisement only. A recovery
    fetch is that plus negotiation plus a packfile.
    """
    assert push._RECOVER_MIN == push._CHECK_TIMEOUT
    assert push._RECOVER_MIN < push._RECOVER_TIMEOUT


def test_a_remainder_at_the_floor_is_still_spent(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor declines below itself, not at it — a clamp in the other
    direction would be the same defect facing the other way."""
    monkeypatch.setattr(push, "_budget_left", lambda: push._RECOVER_MIN)

    assert push._recover_allowance() == push._RECOVER_MIN


def test_the_repush_is_declined_on_a_sub_minimum_remainder_too(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-push is a network call to the same remote, so the same floor.

    It also records what it hands out, so a receipt naming the clock that cut
    must not name a clock no push was ever launched with.
    """
    monkeypatch.setattr(push, "_budget_left", lambda: 3)

    assert push._repush_allowance() == 0


def test_the_decline_says_how_long_was_left_and_what_the_minimum_is(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """A third state the caller can act on, not a silent clamp.

    `fetch TIMED OUT (5s)` was true about the fetch and misleading about the
    cause. The replacement has to name the remainder, the floor, and the fact
    that a retry gets a fresh budget — otherwise it is the same non-answer with
    a different verb.
    """
    box.move_remote_ahead()
    box.commit("mine.txt", "mine")
    seen: list = []
    real = push._git

    def spy(args, **kw):
        if args and args[0] in ("push", "fetch", "rebase"):
            seen.append(args[0])
        return real(args, **kw)

    monkeypatch.setattr(push, "_git", spy)
    asked: list = []

    def left() -> int:
        asked.append(1)
        # Full budget while the first push runs; 5s by the time the recovery
        # fetch asks for its allowance.
        return 5 if asked else 600

    monkeypatch.setattr(push, "_budget_left", left)

    rc, out = box.drive_push("budget=600")

    assert rc != 0, out
    assert "fetch" not in seen, (
        "the fetch was launched on 5s: " + repr(seen) + os.linesep + out)
    verdict = _verdict(out)
    assert verdict.startswith("[result] NOT PUSHED"), verdict
    assert "5s" in out, (
        "the receipt does not say how much was left, so the caller cannot "
        "tell a spent budget from a nearly-spent one:" + os.linesep + out)
    assert str(push._RECOVER_MIN) in out, (
        "the receipt does not name the minimum it declined against:"
        + os.linesep + out)


def test_documented() -> None:
    assert_change_is_findable(1659)
    assert_change_is_findable(1647)
    assert_change_is_findable(1649)
