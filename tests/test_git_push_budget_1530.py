"""#1530 — `git-push` has a 300s budget and no way to ask for more.

`.githooks/pre-push` runs the full suite when the destination is `master`/`main`
(#1242, #894). That suite measured **530.71s** and **288.17s** in two worktrees
on one loaded machine, against `_PUSH_TIMEOUT = 300`. So a push to the default
branch on a busy laptop cannot complete inside the budget, ever, and the op
advertised no flag for it — `force-with-lease`, `no-verify`, `watch`,
`set-upstream`, `to-upstream` and nothing else.

The three routes that existed were each wrong in a different way: `:no-verify`
skips the gate the hook exists to be, raw `git push` is refused by the shipped
guard *naming this op as the remedy* (#1487's `misdirects`), and a retry costs
another 300s and fails again.

What is added is `:budget=SECONDS`, and what is deliberately NOT added is a
self-sizing budget. The op can see that a hook exists and that the destination
is protected; it cannot see what the hook *does*. Sizing from "master + a hook"
would generalise this repository's own convention to every plugin user, and it
fails in both directions — a guess that is low is this defect with extra
machinery, and one that is high makes a genuinely hung push wait. The fact that
decides the number is the current load on the caller's machine, which only the
caller has.

Everything a refusal must be here, per docs/validators.md §"Declining instead of
guessing" — three states, not two:

* absent      — the default `_PUSH_TIMEOUT` applies, unchanged;
* a number    — that number applies, end to end, to git's own clock;
* unusable    — refused by name, **never clamped and never dropped**. A budget
  silently rounded down to 300 is #647's `:no-verifyy` wearing this issue's
  clothes: the caller believes they asked for twenty minutes and gets five.

The receipt `_report_push_timeout` renders is untouched in substance — it reads
the remote back, refuses to guess, names the hook and forbids a force-push on a
timeout alone. It gains the one thing it lacked: the remedy.
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
_spec = importlib.util.spec_from_file_location("git_push_1530", PRESET)
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
    """Bare remote + `mine`, the clone the op is driven in."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1530_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        assert _run(["init", "--bare", "-b", "feature", "remote.git"],
                    self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        Path(self.mine, "a.txt").write_text("base", encoding="utf-8")
        assert _run(["add", "a.txt"], self.mine).returncode == 0
        assert _run(["commit", "-m", "base"], self.mine).returncode == 0

    def head(self) -> str:
        return _run(["rev-parse", "HEAD"], self.mine).stdout.strip()

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
    """The budget is module state, like `_RUN`. Reset it around every test so
    one case cannot decide another's clock."""
    push._reset_budget()
    yield
    push._reset_budget()


def _verdict(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, f"no output at all:{os.linesep}{out}"
    assert lines[-1].startswith("[result] "), (
        f"the receipt does not end on a verdict:{os.linesep}{out}")
    return lines[-1]


# ---------------------------------------------------------------------------
# parsing — the flag exists, and an unusable value is refused rather than taken
# ---------------------------------------------------------------------------

def test_absent_budget_leaves_the_documented_default_in_force() -> None:
    assert push._parse_budget([]) == (None, "")
    assert push._push_budget() == push._PUSH_TIMEOUT


def test_a_budget_token_is_not_reported_as_an_unknown_flag() -> None:
    """`_split_flags` refuses anything it does not recognise (#647), so a
    `budget=` it has never heard of would be refused before it is ever read."""
    known, unknown = push._split_flags(["budget=900"])
    assert unknown == [], unknown
    assert known == set(), known


def test_budget_without_a_value_is_still_an_unknown_flag() -> None:
    """Bare `budget` asks nothing. It goes down the existing refusal, which
    already prints the accepted spellings."""
    assert push._split_flags(["budget"])[1] == ["budget"]


def test_a_number_is_taken_verbatim() -> None:
    assert push._parse_budget(["budget=900"]) == (900, "")
    assert push._parse_budget(["force-with-lease", "budget=1200"]) == (1200, "")


def test_a_non_integer_is_refused_by_name() -> None:
    seconds, why = push._parse_budget(["budget=soon"])
    assert seconds is None
    assert "budget=soon" in why, why


def test_an_empty_value_is_refused() -> None:
    seconds, why = push._parse_budget(["budget="])
    assert seconds is None
    assert why


@pytest.mark.parametrize("tok", ["budget=0", "budget=-30"])
def test_a_non_positive_budget_is_refused(tok: str) -> None:
    seconds, why = push._parse_budget([tok])
    assert seconds is None
    assert "positive" in why, why


@pytest.mark.parametrize("tok", ["budget=1_800", "budget=+900", "budget=9e2",
                                 "budget=٩٠٠"])
def test_only_ascii_digits_count_as_a_number(tok: str) -> None:
    """`int()` would take all four. The last is Arabic-Indic 900, which would
    be honoured and then printed back as `900s budget` on a receipt the caller
    cannot match to what they typed."""
    seconds, why = push._parse_budget([tok])
    assert seconds is None, seconds
    assert why


def test_a_budget_over_the_ceiling_is_refused_not_clamped() -> None:
    """Clamping is this repo's other defect: it turns "I cannot do that" into a
    quietly different answer. The caller asked for a number the op cannot
    honour, and the outer op cap is why — so say the number."""
    over = push._PUSH_TIMEOUT_MAX + 1
    seconds, why = push._parse_budget([f"budget={over}"])
    assert seconds is None, (
        f"a budget above the ceiling was accepted as {seconds}")
    assert str(push._PUSH_TIMEOUT_MAX) in why, why


def test_the_ceiling_itself_is_accepted() -> None:
    assert push._parse_budget([f"budget={push._PUSH_TIMEOUT_MAX}"]) == (
        push._PUSH_TIMEOUT_MAX, "")


def test_two_different_budgets_are_a_contradiction_not_a_precedence_rule() -> None:
    """Same rule as `:set-upstream` + `:to-upstream` (#879): two answers to one
    question is a retype, and picking one silently is a guess."""
    seconds, why = push._parse_budget(["budget=400", "budget=900"])
    assert seconds is None
    assert "400" in why and "900" in why, why


def test_the_same_budget_twice_is_not_a_contradiction() -> None:
    assert push._parse_budget(["budget=400", "budget=400"]) == (400, "")


# ---------------------------------------------------------------------------
# the ceiling is a fact about presets/git.json, not a number in one file
# ---------------------------------------------------------------------------

def test_the_ceiling_stays_strictly_under_the_op_level_cap() -> None:
    """#399's invariant, now load-bearing for the ceiling rather than for the
    default: a push killed by supertool's outer cap can verify nothing, because
    this script is not alive to ask the remote what landed."""
    entry = json.loads((ROOT / "presets" / "git.json").read_text(
        encoding="utf-8"))["ops"]["git-push"]
    assert push._PUSH_TIMEOUT < push._PUSH_TIMEOUT_MAX
    assert push._PUSH_TIMEOUT_MAX < entry["timeout"], (
        f"a caller may ask for {push._PUSH_TIMEOUT_MAX}s inside an op capped "
        f"at {entry['timeout']}s — the outer cap would kill the receipt")


def test_the_op_advertises_the_flag_it_accepts() -> None:
    """#647's lesson, the other way round: a flag the parser honours and the
    registry never mentions is a flag nobody can find."""
    entry = json.loads((ROOT / "presets" / "git.json").read_text(
        encoding="utf-8"))["ops"]["git-push"]
    assert "budget=" in entry["syntax"], entry["syntax"]


# ---------------------------------------------------------------------------
# end to end — the number reaches git's own clock, and a bad one pushes nothing
# ---------------------------------------------------------------------------

def _push_clock(box: _Sandbox, monkeypatch: pytest.MonkeyPatch,
                *argv: str) -> tuple[list[int], int, str]:
    """Every `timeout=` `git push` itself was launched with."""
    seen: list[int] = []
    real = push._git

    def spy(args, **kw):
        if args and args[0] == "push":
            seen.append(kw.get("timeout"))
        return real(args, **kw)

    monkeypatch.setattr(push, "_git", spy)
    rc, out = box.drive_push(*argv)
    return seen, rc, out


def test_the_default_budget_is_what_git_push_is_launched_with(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, rc, out = _push_clock(box, monkeypatch)
    assert rc == 0, out
    assert seen == [push._PUSH_TIMEOUT], out


def test_the_asked_for_budget_is_what_git_push_is_launched_with(
        box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole issue. Anything short of the real `subprocess` clock moving is
    a flag that is accepted and dropped."""
    seen, rc, out = _push_clock(box, monkeypatch, "budget=900")
    assert rc == 0, out
    assert seen == [900], out
    assert box.remote_has_feature()


def test_an_unusable_budget_pushes_nothing_and_says_so(box) -> None:
    rc, out = box.drive_push("budget=soon")
    assert rc != 0, out
    assert not box.remote_has_feature(), (
        "the op pushed anyway on a budget it could not read")
    verdict = _verdict(out)
    assert verdict.startswith("[result] NOT PUSHED - no push attempted"), verdict
    assert "budget=soon" in verdict, verdict


def test_the_budget_in_force_is_disclosed_on_the_receipt(box) -> None:
    """A budget that is not the documented one and is never printed leaves the
    caller unable to tell an honoured flag from a dropped one."""
    _rc, out = box.drive_push("budget=900")
    assert "900" in out, out


# ---------------------------------------------------------------------------
# the timeout receipt — unchanged in substance, plus the remedy it lacked
# ---------------------------------------------------------------------------

def _timeout_out(monkeypatch: pytest.MonkeyPatch) -> str:
    """The failing arm: local HEAD and the remote disagree."""
    monkeypatch.setattr(push, "_local_head", lambda: ("a" * 40, ""))
    monkeypatch.setattr(push, "_live_remote_sha", lambda *a, **k: ("b" * 40, ""))
    monkeypatch.setattr(push, "_prepush_hook_state",
                        lambda flags: ("runs", "/repo/.githooks/pre-push"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        push._report_push_timeout("feature", "a" * 40, "origin", "feature",
                                  set())
    return buf.getvalue()


def test_the_timeout_receipt_names_the_budget_actually_in_force(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """It used to interpolate the constant. With a caller-supplied budget that
    is a receipt reporting a clock that did not cut."""
    push._BUDGET["seconds"] = 900
    out = _timeout_out(monkeypatch)
    assert "900s budget" in out, out
    assert f"{push._PUSH_TIMEOUT}s budget" not in out, out


def test_the_timeout_receipt_offers_the_remedy(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """#1487's `misdirects`, closed. The receipt already names the hook as the
    likely consumer of the budget; naming no way to lengthen it is what left
    `:no-verify` as the only flag that helped."""
    out = _timeout_out(monkeypatch)
    assert "git-push:budget=" in out, out


def test_the_timeout_receipt_keeps_everything_it_already_said(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly not weakened (#1242, #663, #675)."""
    out = _timeout_out(monkeypatch)
    assert "does NOT match local HEAD" in out, out
    assert "do NOT force-push on a timeout alone" in out, out
    assert "_PUSH_TIMEOUT" in out, out
    assert "presets/git/push.py" in out, out
    assert "pre-push hook" in out, out


def test_documented() -> None:
    assert_change_is_findable(1530)
