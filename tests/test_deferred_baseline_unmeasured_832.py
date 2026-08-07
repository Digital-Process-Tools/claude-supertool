"""A baseline nothing measured renders as a baseline that measured zero (#832).

Reported against `phpunit-mcp`, which printed

    phpunit-mcp : 0 → 7  (+7) ✗

after an edit that introduced nothing. All seven tests were already failing —
the local test DB was missing a table. The reader nearly reverted a correct
fix, and the `0 →` is why: it asserts a pre-edit measurement that returned
zero.

The mechanism is not specific to phpunit. `_drain_validator_queue` calls
`_validator_render_diff(None, data)` — the slow tier has **no baseline pass at
all**, by design, because its whole point is running once on the final bytes.
`before is None` then falls through `before.get("count", 0) if before else 0`
and becomes a literal `0`. So every slow-tier validator reports every
pre-existing failure in the repo as a regression this edit caused, on every
run. Same arithmetic as #969, one caller over: an absence given a number and
subtracted as though it were a measurement.

Three states, not two, applied to the *left* side of the arrow: measured,
measured-and-nonzero, and not measured.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

SEVEN_FAILURES = json.dumps({
    "tool": "faketest", "ok": False, "count": 7,
    "errors": [{"line": i, "col": None, "severity": "error", "code": "E",
                "msg": f"Table 'project_bill_i18n' doesn't exist ({i})"}
               for i in range(1, 8)],
    "duration_ms": 1,
})
CLEAN = json.dumps({"tool": "faketest", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})


def _adapter(script: Path, payload: str) -> str:
    script.write_text(f"import sys\nsys.stdout.write({payload!r})\n",
                      encoding="utf-8")
    return f"{{python}} {script.as_posix()}"


def _set_slow(tmp_path: Path, payload: str) -> None:
    cmd = _adapter(tmp_path / "_adapter.py", payload)
    supertool._CONFIG = {"validators": {
        "faketest": {"cmd": cmd, "match": "*", "cache": False, "tier": "slow",
                     "hooks_into": ["edit", "replace", "replace_lines",
                                    "paste", "append", "vim"]},
    }}
    supertool._CONFIG_CHECKED = True


def _set_fast(tmp_path: Path, payload: str) -> None:
    cmd = _adapter(tmp_path / "_adapter.py", payload)
    supertool._CONFIG = {"validators": {
        "faketest": {"cmd": cmd, "match": "*", "cache": False,
                     "hooks_into": ["edit", "replace", "replace_lines",
                                    "paste", "append", "vim"]},
    }}
    supertool._CONFIG_CHECKED = True


@pytest.fixture(autouse=True)
def _stable_branch(monkeypatch):
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("f", ""))


def _two_op_edit(tmp_path: Path, capsys) -> str:
    """Two args, so `main()` takes the defer path and the slow tier queues."""
    f = tmp_path / "s.txt"
    f.write_text("exit 0\n", encoding="utf-8")
    supertool.main([f"edit:::exit :::exit:::{f}", f"read:{f}"])
    return capsys.readouterr().out


def _one_op_edit(tmp_path: Path, capsys) -> str:
    f = tmp_path / "s.txt"
    f.write_text("exit 0\n", encoding="utf-8")
    supertool.main([f"edit:::exit :::exit:::{f}"])
    return capsys.readouterr().out


def _row(out: str, tool: str = "faketest") -> str:
    for line in out.splitlines():
        if line.strip().startswith(tool):
            return line.strip()
    raise AssertionError(f"no {tool} row in:\n{out}")


# ---------------------------------------------------------------------------
# THE bug — the reported row
# ---------------------------------------------------------------------------

def test_an_unmeasured_baseline_is_not_printed_as_zero(
        tmp_path: Path, capsys) -> None:
    """`0 →` is the assertion that cost the reporter a near-revert."""
    _set_slow(tmp_path, SEVEN_FAILURES)
    out = _two_op_edit(tmp_path, capsys)
    assert "[validators-deferred]" in out, out
    row = _row(out)
    assert "0 → 7" not in row, row
    assert "? → 7" in row, row


def test_the_row_says_the_baseline_was_not_measured(
        tmp_path: Path, capsys) -> None:
    """A `?` on its own is a puzzle. The row has to name what is missing."""
    _set_slow(tmp_path, SEVEN_FAILURES)
    row = _row(_two_op_edit(tmp_path, capsys))
    assert "baseline not measured" in row, row


def test_the_delta_is_not_fabricated_either(tmp_path: Path, capsys) -> None:
    """`(+7)` is the same subtraction, one column over.

    Seven minus an unmeasured baseline is not seven. Fixing the arrow and
    leaving the delta would move the false number rather than remove it.
    """
    _set_slow(tmp_path, SEVEN_FAILURES)
    row = _row(_two_op_edit(tmp_path, capsys))
    assert "(+7)" not in row, row


def test_the_findings_are_not_labelled_new(tmp_path: Path, capsys) -> None:
    """The `+ ` prefix under the row means "introduced by this op".

    With no baseline, every error looks new because `before_msgs` is empty —
    the third place the same absence gets read as a measurement.
    """
    _set_slow(tmp_path, SEVEN_FAILURES)
    out = _two_op_edit(tmp_path, capsys)
    detail = [ln for ln in out.splitlines() if "project_bill_i18n" in ln]
    assert detail, out
    assert not any(ln.strip().startswith("+") for ln in detail), detail


def test_a_clean_deferred_result_does_not_claim_no_new_errors(
        tmp_path: Path, capsys) -> None:
    """`(no new errors)` is a comparison, and there was nothing to compare to.

    The equal-counts branch is reached because an unmeasured baseline is 0 and
    a clean run is 0. It reads as "this edit introduced nothing", which is the
    same fabrication as `0 → 7` with the sign flipped — quieter, and therefore
    the one that would have been left behind.
    """
    _set_slow(tmp_path, CLEAN)
    row = _row(_two_op_edit(tmp_path, capsys))
    assert "no new errors" not in row, row
    assert "? → 0" in row, row


# ---------------------------------------------------------------------------
# What must not regress — a real baseline still gets real arithmetic
# ---------------------------------------------------------------------------

def test_a_measured_clean_baseline_still_reads_zero(
        tmp_path: Path, capsys) -> None:
    """The fast tier runs a real pre-op pass. `0 → 7` there is a measurement."""
    _set_fast(tmp_path, SEVEN_FAILURES)
    row = _row(_one_op_edit(tmp_path, capsys))
    assert "7 err" in row or "0 → 7" in row, row
    assert "baseline not measured" not in row, row


def test_a_measured_baseline_that_matches_still_reads_pre_existing(
        tmp_path: Path, capsys) -> None:
    """Both passes measured 7. `pre-existing` is then a true claim."""
    _set_fast(tmp_path, SEVEN_FAILURES)
    row = _row(_one_op_edit(tmp_path, capsys))
    assert "pre-existing" in row, row


def test_a_clean_fast_validator_still_reads_no_new_errors(
        tmp_path: Path, capsys) -> None:
    _set_fast(tmp_path, CLEAN)
    row = _row(_one_op_edit(tmp_path, capsys))
    assert "no new errors" in row, row
