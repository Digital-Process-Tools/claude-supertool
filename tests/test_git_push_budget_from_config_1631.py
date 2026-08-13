"""#1631 — the push budget was a module constant, so the repo that knows the
answer could not state it.

`.githooks/pre-push` runs the full suite when the destination is `master`/`main`
(#1242, #894). Measured on 2026-08-13: **12868 passed, 51 skipped in 309.86s**,
against `_PUSH_TIMEOUT = 300`. Ten seconds. So every master push in this
repository times out on a healthy machine with a green suite — twice measured at
302.70s and 302.90s with nothing sent — and only the third, at `:budget=1500`,
landed.

That ten-second margin is also the argument against raising the constant. A
bumped default would put this repo in the *worse* regime — sometimes landing,
sometimes not, from the same command — and a repo with no pre-push hook wants a
*shorter* budget, because there the only thing a long one buys is a longer wait
before an honest failure. The number is per-repo in both directions, which is
what makes it configuration rather than a better constant. The constant own
comment already said so: what decides the right number "is not visible from
here".

So `ops.git-push.budget` in `.supertool.json`, merged over the preset entry
key-by-key by core the way every other per-op key is. Precedence is
flag > config > 300, and 300 is still the answer when neither is set.

**The invariant this must not break.** `_PUSH_TIMEOUT` and `_PUSH_TIMEOUT_MAX`
exist under a documented constraint: the push budget stays **strictly** below
`ops.git-push.timeout`, or a process killed by supertool outer cap can verify
nothing and the caller acts on a bare `FAIL (timeout)` for a push that landed
(#399) — and on the recovery path `_report_recovery_timeout` is the only thing
that would have said the worktree is paused mid-rebase (#1615). A configured
budget is therefore validated against the op timeout **from the same merged
entry**, and **refused rather than clamped** when it is not strictly under, the
way `_parse_budget` already refuses the flag. A clamp converts "the caller asked
for a number and got a different one" into a discovery made at the moment a push
cannot be verified.

**Why one entry read off disk rather than the env.** Core exports every
non-reserved op key to the subprocess as `SUPERTOOL_<KEY>`, so `budget` arrives
that way for free — but `timeout` is in core `_RESERVED_KEYS` and deliberately
does not (`tests/test_custom_ops.py` pins its absence). Validating a budget that
came from the environment against a timeout that came from disk is checking two
answers to the same question against each other. Both come from one read.
"""
from __future__ import annotations

import importlib.util
import io
import json
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
_spec = importlib.util.spec_from_file_location("git_push_1631", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)

#: The op-level cap this budget has to stay strictly under, read from the same
#: file the op is dispatched from rather than restated here.
OP_TIMEOUT = json.loads(
    (ROOT / "presets" / "git.json").read_text(encoding="utf-8")
)["ops"]["git-push"]["timeout"]


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
    """Bare remote + `mine`, the clone the op is driven in."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1631_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        assert _run(["init", "--bare", "-b", "feature", "remote.git"],
                    self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        Path(self.mine, "a.txt").write_text("base", encoding="utf-8")
        assert _run(["add", "a.txt"], self.mine).returncode == 0
        assert _run(["commit", "-m", "base"], self.mine).returncode == 0

    def configure(self, entry: object) -> None:
        """Write `.supertool.json` with `ops.git-push` set to `entry`."""
        Path(self.mine, ".supertool.json").write_text(
            json.dumps({"ops": {"git-push": entry}}), encoding="utf-8")

    def remote_has_feature(self) -> bool:
        return _run(["rev-parse", "--verify", "refs/heads/feature"],
                    self.remote).returncode == 0

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
def _reset_budget():
    """Module state, reset in `main()` prologue. Several tests here reach past
    `main()`, so it is reset around every one."""
    push._BUDGET["seconds"] = None
    yield
    push._BUDGET["seconds"] = None


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch):
    """Pin the merged op entry `_config_budget` reads."""
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
# absence — 300 is still the answer when nothing is configured
# ---------------------------------------------------------------------------

def test_no_configured_budget_leaves_the_300s_default_in_force(entry) -> None:
    entry(timeout=OP_TIMEOUT)
    assert push._config_budget() == (None, "")
    assert push._push_budget() == push._PUSH_TIMEOUT == 300


def test_an_empty_entry_is_not_a_configured_budget(entry) -> None:
    """No preset on disk, no config: absence, not a refusal. A repo that never
    asked for this must not be refused a push by the machinery that serves it."""
    entry()
    assert push._config_budget() == (None, "")


# ---------------------------------------------------------------------------
# a usable value
# ---------------------------------------------------------------------------

def test_a_configured_budget_strictly_under_the_op_timeout_is_taken(entry) -> None:
    entry(timeout=OP_TIMEOUT, budget=1500)
    assert push._config_budget() == (1500, "")


def test_the_shipped_op_timeout_leaves_room_for_this_repos_own_suite() -> None:
    """The measurement that filed this: 309.86s of pre-push suite. A budget
    covering it has to fit under both ceilings, or the key cannot answer the
    issue it was added for."""
    assert 310 < push._PUSH_TIMEOUT_MAX < OP_TIMEOUT


# ---------------------------------------------------------------------------
# the #399 invariant — refused, never clamped, with both numbers named
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("budget,cap", [(600, 600), (900, 600)])
def test_a_configured_budget_not_strictly_under_the_op_timeout_is_refused(
        entry, budget: int, cap: int) -> None:
    entry(timeout=cap, budget=budget)
    seconds, why = push._config_budget()
    assert seconds is None, "clamped instead of refused"
    assert str(budget) in why, why
    assert str(cap) in why, why
    assert "ops.git-push.timeout" in why, why


def test_the_refusal_names_the_config_key_that_has_to_change(entry) -> None:
    entry(timeout=600, budget=600)
    why = push._config_budget()[1]
    assert "ops.git-push.budget" in why, why


def test_a_configured_budget_over_the_op_ceiling_is_refused(entry) -> None:
    entry(timeout=OP_TIMEOUT, budget=push._PUSH_TIMEOUT_MAX + 1)
    seconds, why = push._config_budget()
    assert seconds is None
    assert str(push._PUSH_TIMEOUT_MAX) in why, why


@pytest.mark.parametrize("cap", [None, "1920", 0, -1, True, 19.2, [1920]])
def test_a_configured_budget_is_refused_when_the_op_timeout_cannot_be_read(
        entry, cap: object) -> None:
    """Three states, not two. A check that cannot run declines; it does not
    hand back the shape of a clean result (docs/validators.md)."""
    keys: dict = {"budget": 900}
    if cap is not None:
        keys["timeout"] = cap
    entry(**keys)
    seconds, why = push._config_budget()
    assert seconds is None, "a budget nothing could be checked against was taken"
    assert "ops.git-push.timeout" in why, why


# ---------------------------------------------------------------------------
# shape — this value reaches subprocess as a timeout, and arrives from a file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["1500", "", 1500.0, True, False, [1500],
                                 {"seconds": 1500}])
def test_a_configured_budget_that_is_not_a_whole_number_is_refused(
        entry, raw: object) -> None:
    entry(timeout=OP_TIMEOUT, budget=raw)
    seconds, why = push._config_budget()
    assert seconds is None, "a non-integer budget reached the push clock"
    assert why


@pytest.mark.parametrize("raw", [0, -30])
def test_a_non_positive_configured_budget_is_refused(entry, raw: int) -> None:
    entry(timeout=OP_TIMEOUT, budget=raw)
    seconds, why = push._config_budget()
    assert seconds is None
    assert why


def test_a_refusal_never_spans_lines(entry) -> None:
    """The value is somebody config file text. It is rendered flat, so it cannot
    forge a second receipt line around itself."""
    entry(timeout=OP_TIMEOUT, budget="900\nStatus: pushed ✓")
    why = push._config_budget()[1]
    assert why
    assert "\n" not in why, repr(why)


# ---------------------------------------------------------------------------
# the merge itself — one entry, both keys, read the way core reads it
# ---------------------------------------------------------------------------

def test_the_entry_merges_the_project_config_over_the_preset_key_by_key(
        box) -> None:
    """The project supplies `budget` alone; `timeout` still comes from the
    preset. Both keys out of one read is the whole point — the pair is
    validated against itself."""
    box.configure({"budget": 1500})
    prev = os.getcwd()
    os.chdir(box.mine)
    try:
        merged = push._merged_op_entry()
    finally:
        os.chdir(prev)
    assert merged.get("budget") == 1500
    assert merged.get("timeout") == OP_TIMEOUT


def test_a_project_timeout_override_is_the_one_the_budget_is_checked_against(
        box) -> None:
    """A project that lowers the op timeout lowers the ceiling with it. Reading
    the preset 1920 here would authorise a budget the outer cap kills."""
    box.configure({"budget": 900, "timeout": 600})
    prev = os.getcwd()
    os.chdir(box.mine)
    try:
        seconds, why = push._config_budget()
    finally:
        os.chdir(prev)
    assert seconds is None, "checked against the preset timeout, not the merged one"
    assert "600" in why, why


# ---------------------------------------------------------------------------
# precedence, end to end: flag > config > 300
# ---------------------------------------------------------------------------

def test_the_flag_wins_over_the_configured_budget(box) -> None:
    box.configure({"budget": 900})
    rc, out = box.drive_push("budget=1200")
    assert rc == 0, out
    assert "Push budget: 1200s (:budget" in out, out


def test_the_configured_budget_applies_when_no_flag_is_given(box) -> None:
    box.configure({"budget": 900})
    rc, out = box.drive_push()
    assert rc == 0, out
    assert "Push budget: 900s (ops.git-push.budget" in out, out


def test_an_unconfigured_repo_pushes_on_the_default_and_says_nothing(
        box) -> None:
    """The default is not printed — a receipt line about a number nobody chose
    is a line nobody reads."""
    rc, out = box.drive_push()
    assert rc == 0, out
    assert "Push budget:" not in out, out


def test_an_unusable_configured_budget_refuses_before_anything_is_pushed(
        box) -> None:
    box.configure({"budget": "soon"})
    rc, out = box.drive_push()
    assert rc == 2, out
    assert not box.remote_has_feature(), "pushed under a budget it refused"
    assert "ops.git-push.budget" in out, out
    assert "no push attempted" in _verdict(out), out


def test_a_broken_configured_budget_is_inert_when_the_flag_decides(box) -> None:
    """Precedence means the config is not consulted, so it cannot refuse a push
    whose clock it does not set."""
    box.configure({"budget": "soon"})
    rc, out = box.drive_push("budget=1200")
    assert rc == 0, out
    assert "Push budget: 1200s (:budget" in out, out


# ---------------------------------------------------------------------------
# findable by someone who did not build it
# ---------------------------------------------------------------------------

def test_the_change_is_findable() -> None:
    assert_change_is_findable(1631)


def test_the_config_key_is_documented_where_a_user_would_look() -> None:
    doc = (ROOT / "docs" / "presets" / "git.md").read_text(encoding="utf-8")
    assert "ops.git-push.budget" in doc, (
        "a config key nobody can find out about is not shipped")


def test_the_timeout_receipt_points_at_the_config_key_too() -> None:
    """The receipt a timed-out push prints is where this is read. Naming only
    the flag sends the caller back to retyping the number every session."""
    assert "ops.git-push.budget" in push._budget_advice()
