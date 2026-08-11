"""The call's verdict must be about the call, not the thread's history.

`dispatch_verdict` returns `(out, _call_failed())`, and `_call_failed()` is a
pure read of the thread-local `_DISPATCH_STATE.call_failed`. The only clear
lives inside `dispatch` at depth 0 — and `dispatch` is a name callers replace,
which `dispatch_verdict`'s own docstring records ("both the tests and the MCP
layer monkeypatch it"). With the clear skipped, the verdict returned belongs to
whatever last refused on this thread.

Cost: `master` red on macOS only at df34db5 (3.10/3.11/3.12), ubuntu and
windows green on the same commit. Not a platform fact — xdist `--dist load`
schedules individual tests, so which test follows a real refusing dispatch in
one worker is decided by core count. Issue #1359.
"""
from __future__ import annotations

import pytest

import supertool


@pytest.fixture
def stub_dispatch(monkeypatch):
    """Replace `dispatch` — the documented monkeypatch contract (#1359)."""
    seen: list[str] = []
    monkeypatch.setattr(supertool, "dispatch", lambda a: (seen.append(a), "")[-1])
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    return seen


@pytest.fixture(autouse=True)
def clean_verdict():
    """This suite sets the flag on purpose; never let it leave the module."""
    yield
    supertool._DISPATCH_STATE.call_failed = False


def test_a_stale_flag_is_not_this_call_s_exit_code(stub_dispatch) -> None:
    supertool._mark_op_failure()  # an earlier call refused, on this thread
    # Two ops, neither in _PARALLEL_SAFE_OPS, so this is the sequential path
    # that `main` takes for a mutating batch — the one CI went red on.
    rc = supertool.main(["edit:@-", "@-"])
    assert rc == 0
    assert stub_dispatch == ["edit:@-", "@-"]


def test_a_stale_flag_does_not_make_the_tally_claim_refusals(
        stub_dispatch, capsys) -> None:
    """The worse half: #1284's tally is a sentence built on that bit."""
    supertool._mark_op_failure()
    supertool.main(["edit:@-", "@-"])
    assert "refused" not in capsys.readouterr().out


def test_a_real_refusal_on_a_previous_call_does_not_carry(
        monkeypatch, tmp_path) -> None:
    """Reached without poking internals: a real op refuses, then a stub call."""
    monkeypatch.chdir(tmp_path)
    supertool.dispatch("read:definitely-not-here-1359.txt")
    assert supertool._call_failed() is True
    monkeypatch.setattr(supertool, "dispatch", lambda a: "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    assert supertool.main(["edit:@-", "@-"]) == 0


def test_the_clear_does_not_swallow_a_refusal_raised_inside_dispatch(
        monkeypatch) -> None:
    """Do not trade the loud bug for the quiet one.

    The flag must be established BEFORE dispatch runs, never after: a sub-op
    that refuses at any depth still has to reach the parent's verdict.
    """
    def refusing(arg: str) -> str:
        supertool._mark_op_failure()
        return "ERROR: no" + chr(10)

    monkeypatch.setattr(supertool, "dispatch", refusing)
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    assert supertool.main(["edit:@-", "@-"]) == 1


def test_verdict_helper_reports_only_its_own_call(monkeypatch) -> None:
    """The unit under the two above, so a regression names the right function."""
    monkeypatch.setattr(supertool, "dispatch", lambda a: "")
    supertool._mark_op_failure()
    _out, failed = supertool.dispatch_verdict("read:x")
    assert failed is False
