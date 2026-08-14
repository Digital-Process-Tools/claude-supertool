"""A wall on ONE unit of a multi-unit adapter is not a verdict either (#1709).

Observed on `pytest (windows-latest, 3.10)`, PR #1703, 2026-08-14 -- one leg
red, everything else green, and green again on a re-run with no code change:

    FAILED tests/test_html_check.py::test_two_script_blocks_are_paired_and_
        checked_independently - assert 2 == 1

The evidence that settles which side it is on is in that job's own log, not
inferred:

    34.77s call  tests/test_html_check.py::test_two_script_blocks_...
    30.90s call  tests/test_html_check.py::test_valid_inline_script_is_ok
    adapter-wall(#794,#1604): 4 of 868 skipped tests did NOT get an adapter
        verdict

`html-check`'s own budget is 30s per `<script>` block. Four sibling tests in
the same file spent it and declined through the #794 gate; this one spent it
too. It is the only test in the file with **two** blocks, and that is the
whole difference: `node --check` stalled on the first block and answered on
the second, so the payload carried an `adapter` wall AND a real `syntax`
finding at once.

`stalled_at_its_own_wall`'s second clause requires *every* error to be a wall
and returned `None` for that mixture, so the payload was asserted on as a
verdict -- and `count == 1` is not a claim about the file at all once a block
went unchecked. It is a claim about how many units got an answer. The finding
on line 4 was real; the number next to it was not.

So the direction is the same as #1296 and the fix is the same shape one clause
in: a mixture of a wall and findings is a **partial** verdict, and a partial
verdict is not one. `count`, the ordering of `errors`, and `errors[0]` are all
unsupported when a unit was never checked.

What deliberately does NOT move -- each of these stays red:

* a wall beside an `adapter` error that is not a wall (an absent `node`, a
  crashed adapter). That is a real fault someone has to fix and #1604 exists
  to keep it loud; a decline that swallows it is the loud bug traded for the
  quiet one.
* a wall reported faster than the adapter's own budget. Clause four, unchanged.
* findings with no wall anywhere in them. That is a verdict.

Windows claim grade: **observed**. The failing leg's log is the source for
every number above, not a reading of CPython.
"""
from __future__ import annotations

import pytest

import test_html_check as html_tests
from _adapter_verdict import ADAPTER_WALL_TOKEN, stalled_at_its_own_wall
from test_html_check_stall_1296 import _a_skip_here_is_a_failure, _stub_adapter

INNER_S = 30

# The adapter's real wording for the block that never answered, per
# `validators/html-check/html-check.py`'s TimeoutExpired arm.
WALL_MSG = ("timeout -- node --check did not return within 30s "
            "for the <script> block starting at line 2")


def _wall_error(msg: str = WALL_MSG) -> dict:
    return {"line": None, "col": None, "severity": "error",
            "code": "adapter", "msg": msg}


def _syntax_error(line: int = 4) -> dict:
    return {"line": line, "col": None, "severity": "error",
            "code": "syntax", "msg": "SyntaxError: Unexpected token ';'"}


def _payload(errors: list, duration_ms: int = 34_770) -> dict:
    return {"tool": "html-check", "file": "two.html", "ok": False,
            "count": len(errors), "errors": list(errors),
            "duration_ms": duration_ms}


def test_a_wall_beside_a_finding_is_not_a_verdict() -> None:
    """The reported payload, classified. One block stalled, one answered."""
    payload = _payload([_wall_error(), _syntax_error()])
    reason = stalled_at_its_own_wall(payload, inner_s=INNER_S)
    assert reason is not None, payload


def test_the_decline_carries_the_counted_token() -> None:
    """A decline nothing counts is #1274's hole; the register keys on this."""
    reason = stalled_at_its_own_wall(
        _payload([_wall_error(), _syntax_error()]), inner_s=INNER_S)
    assert reason is not None and ADAPTER_WALL_TOKEN in reason, reason


def test_the_wall_may_arrive_after_the_finding() -> None:
    """Block order is the file's, not the runner's -- either block can stall."""
    payload = _payload([_syntax_error(line=2), _wall_error()])
    assert stalled_at_its_own_wall(payload, inner_s=INNER_S) is not None, payload


def test_the_reported_test_declines_rather_than_asserting_two_equals_one(
        tmp_path, monkeypatch) -> None:
    """The failure itself, driven end to end through the real test body."""
    monkeypatch.setattr(
        html_tests, "ADAPTER",
        _stub_adapter(tmp_path, _payload([_wall_error(), _syntax_error()])))
    with pytest.raises(pytest.skip.Exception):
        html_tests.test_two_script_blocks_are_paired_and_checked_independently(
            tmp_path)


# ---------------------------------------------------------------------------
# and the three shapes that must stay red
# ---------------------------------------------------------------------------

def test_two_real_findings_are_still_a_verdict() -> None:
    payload = _payload([_syntax_error(line=2), _syntax_error(line=4)])
    assert stalled_at_its_own_wall(payload, inner_s=INNER_S) is None, payload


def test_a_wall_beside_an_absent_node_is_still_red() -> None:
    """`node not on PATH` is instant, actionable and not a statement about
    the machine's load. A gate that let it through would hide an unconfigured
    runner behind a skip."""
    payload = _payload([_wall_error(),
                        _wall_error("node not on PATH -- inline <script> "
                                    "blocks were NOT checked")])
    assert stalled_at_its_own_wall(payload, inner_s=INNER_S) is None, payload


def test_a_partial_wall_reported_impossibly_fast_is_still_red() -> None:
    """Clause four, unchanged: 12ms is not 30 seconds."""
    payload = _payload([_wall_error(), _syntax_error()], duration_ms=12)
    assert stalled_at_its_own_wall(payload, inner_s=INNER_S) is None, payload


def test_a_real_two_block_verdict_still_reaches_its_assertion(
        tmp_path, monkeypatch) -> None:
    """The bar. With no wall anywhere, the pinned line stays exactly as
    strong as it was -- a wrong line is still a red, not a skip."""
    misplaced = _payload([_syntax_error(line=2)], duration_ms=88)
    monkeypatch.setattr(html_tests, "ADAPTER", _stub_adapter(tmp_path, misplaced))
    with _a_skip_here_is_a_failure(), pytest.raises(AssertionError):
        html_tests.test_two_script_blocks_are_paired_and_checked_independently(
            tmp_path)


def test_this_files_inner_s_matches_the_adapters_own_budget() -> None:
    """The payloads above are hand-built, so INNER_S here is a literal -- and
    a literal that drifts from the adapter would make every guard above
    classify against a budget nobody uses (#702)."""
    assert INNER_S == html_tests.INNER_S
