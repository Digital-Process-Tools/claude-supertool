"""#727 — a timeout verdict whose own elapsed figure contradicts it.

`FAIL (timeout 0.0s > 10s)` asserts that an op blew a 10s budget and reports
that it took no time at all. Both halves cannot be true, and a reader cannot
tell which one to believe.

The cause is not a broken clock. `SUPERTOOL_DETERMINISTIC_TIME=1` (#643)
freezes every duration supertool measures so two renders of the same op are
byte-identical, and `tests/conftest.py` sets it for the whole suite — including
every subprocess a test spawns. Everywhere else the freeze removes noise. On
this one path it removes the *evidence*: the elapsed is the only number the
message exists to carry.

These tests pin three things:

* under the freeze, the absence announces itself instead of posing as `0.0s`;
* the frozen line is still constant across runs, so #643's property survives;
* with the switch off, a real elapsed is reported — and it is never below the
  budget, because `subprocess.run(timeout=T)` cannot raise before T has passed.
"""
from __future__ import annotations

import re
import shlex
import time
from pathlib import Path

import supertool


def _timed_out_render(timeout: int = 1) -> str:
    """Run a custom op that cannot finish inside `timeout`, return its output."""
    supertool._CONFIG = {"ops": {"slow": {"cmd": "sleep 30", "timeout": timeout}}}
    result = supertool._resolve_custom_op("slow", ["slow"])
    assert result is not None
    return result


class TestTheFrozenElapsedNoLongerPosesAsAMeasurement:
    """Under the deterministic-time freeze the number is absent, and says so."""

    def test_a_timed_out_op_does_not_report_zero_elapsed_against_its_budget(self) -> None:
        """The exact string from the #727 report must not be renderable.

        `0.0s > 1s` is a verdict contradicted by its own measurement. Whatever
        replaces it, it must not be a smaller-looking number either: no elapsed
        below the budget is a real observation on this path.
        """
        line = _timed_out_render().splitlines()[0]
        assert line.startswith("FAIL (timeout ")
        assert "0.0s > 1s" not in line
        assert not re.search(r"FAIL \(timeout 0\.\ds", line)

    def test_the_suppression_is_named_rather_than_implied(self) -> None:
        """A reader must be able to tell the number was suppressed, not taken."""
        line = _timed_out_render().splitlines()[0]
        assert "frozen" in line
        assert "deterministic-time" in line

    def test_the_budget_is_still_reported(self) -> None:
        """Losing the elapsed must not cost the one figure that is still known."""
        assert "1s" in _timed_out_render(timeout=1).splitlines()[0]

    def test_the_frozen_line_is_identical_across_runs(self) -> None:
        """#643's property, on the path this issue changes.

        Exempting this renderer from the freeze would have been the other fix.
        It would put a varying field back into rendered output, which is the
        hole #643 closed at the renderer so that no call site has to remember.
        """
        assert _timed_out_render().splitlines()[0] == _timed_out_render().splitlines()[0]

    def test_partial_output_still_survives_the_new_verdict_line(self, tmp_path: Path) -> None:
        """#399's evidence must not be lost while rewording the line above it."""
        script = tmp_path / "slow.py"
        script.write_text(
            "import sys, time" + chr(10) +
            "print('Status: pushed')" + chr(10) +
            "sys.stdout.flush()" + chr(10) +
            "time.sleep(30)" + chr(10)
        )
        supertool._CONFIG = {"ops": {"slow": {
            "cmd": "{python} " + shlex.quote(script.as_posix()), "timeout": 2}}}
        result = supertool._resolve_custom_op("slow", ["slow"])
        assert result is not None
        lines = result.splitlines()
        assert lines[0].startswith("FAIL (timeout ")
        assert lines[1] == "--- partial output before timeout ---"
        assert lines[2] == "Status: pushed"


class TestTheRealPathStillReportsARealNumber:
    """With the switch off — i.e. every non-test run — nothing changes."""

    def test_a_real_elapsed_is_rendered_and_is_not_below_the_budget(
            self, monkeypatch) -> None:
        monkeypatch.delenv("SUPERTOOL_DETERMINISTIC_TIME", raising=False)
        line = _timed_out_render(timeout=1).splitlines()[0]
        m = re.match(r"FAIL \(timeout (\d+\.\d+)s > (\d+)s\)$", line)
        assert m is not None, line
        assert float(m.group(1)) >= float(m.group(2))


class TestAnElapsedUnderItsBudgetIsAReportingBug:
    """The sanity guard: `subprocess.run(timeout=T)` cannot raise before T.

    So a rendered elapsed below the budget is not a slow machine, a fast
    machine, or a result at all — it is a defect in this reporting path, and
    printing it as a measurement is what made #727 cost a debugging session.
    """

    def test_an_elapsed_below_the_budget_is_named_as_a_defect(self, monkeypatch) -> None:
        monkeypatch.delenv("SUPERTOOL_DETERMINISTIC_TIME", raising=False)
        line = supertool._timeout_verdict_line(time.monotonic(), 10)
        assert line.startswith("FAIL (timeout ")
        assert "reporting path" in line
        assert "#727" in line

    def test_a_genuine_overrun_renders_plainly(self, monkeypatch) -> None:
        """The guard must not fire on the case it exists to let through."""
        monkeypatch.delenv("SUPERTOOL_DETERMINISTIC_TIME", raising=False)
        line = supertool._timeout_verdict_line(time.monotonic() - 12.0, 10)
        assert line == "FAIL (timeout 12.0s > 10s)"

    def test_the_frozen_line_does_not_trip_the_guard(self, monkeypatch) -> None:
        """A suppressed number is an absence, not a contradiction."""
        monkeypatch.setenv("SUPERTOOL_DETERMINISTIC_TIME", "1")
        line = supertool._timeout_verdict_line(time.monotonic(), 10)
        assert "reporting path" not in line
        assert "frozen" in line
