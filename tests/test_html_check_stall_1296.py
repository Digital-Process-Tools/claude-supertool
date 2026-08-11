"""A stalled `node --check` is not a verdict about the HTML (#1296).

Observed twice on 2026-08-11, both times on `pytest (windows-latest, 3.11)`
and nowhere else, both cleared on a re-run:

    FAILED tests/test_html_check.py::test_broken_inline_script_is_a_finding
      AssertionError: expected the error pinned to source line 6, got
      {'line': None, 'col': None, 'severity': 'error', 'code': 'adapter',
       'msg': 'timeout -- node --check did not return within 30s for the
               <script> block starting at line 5'}

The adapter was right. It could not get an answer inside its own budget and
said so, naming the reason and the block -- the three-state contract of
`docs/validators.md` "Declining instead of guessing" working exactly as
written. `tests/test_html_check.py` then asserted a pinned source line
against that refusal, so a correct decline rendered as a failing test, which
reads as a defect in the product. That is this repo's own defect class --
an absence produced by the tool, read as an absence in the world -- living
inside the suite meant to detect it (#1205/#1218 shape).

The repair is #794's, applied to the direction #794 did not cover.
`assert_adapter_ok_or_skip_if_stalled` already declines a stall for tests
asserting a file is *clean*; most tests here assert a file is *broken*, and
the same stall is the same non-verdict. `_run` therefore routes every spawn
through `skip_if_stalled` and the individual assertions are left alone --
the pin on line 6 stays exactly as strong as it was for every run in which
node actually answered.

Not "accept any `severity: error`": that would also accept an absent node
and a timeout reported in 12ms, both of which are defects someone has to
fix. The four clauses of `stalled_at_its_own_wall` are what tell a wall from
those, and they are the reason the gate is that predicate rather than a
looser assertion.

Not a `node` probe either. The failing leg had node; what it did not have
was a spare 30 seconds partway through a suite. A probe that answers in
40ms says nothing about that, and it would buy a skip on every green run
in exchange -- a skip nothing counts (#1274).

Why 3.11 specifically is unexplained. The same job on other Python versions
passed both times and there is no account of that here beyond runner load;
none is asserted, and none should be inferred from this file.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

import _adapter_verdict as verdicts
import test_html_check as html_tests
from _adapter_budget import inner_budget
from _adapter_verdict import skip_if_stalled

TIMEOUT_MSG = (
    "timeout -- node --check did not return within 30s "
    "for the <script> block starting at line 5"
)


def _stall(duration_ms: int) -> dict:
    return {
        "tool": "html-check", "file": "dashboard.html", "ok": False, "count": 1,
        "errors": [{"line": None, "col": None, "severity": "error",
                    "code": "adapter", "msg": TIMEOUT_MSG}],
        "duration_ms": duration_ms,
    }


def _finding() -> dict:
    return {
        "tool": "html-check", "file": "dashboard.html", "ok": False, "count": 1,
        "errors": [{"line": 6, "col": 11, "severity": "error",
                    "code": "parse", "msg": "Unexpected token ';'"}],
        "duration_ms": 88,
    }


def _clean() -> dict:
    return {"tool": "html-check", "file": "dashboard.html", "ok": True,
            "count": 0, "errors": [], "duration_ms": 74}


def _stub_adapter(tmp_path: Path, payload: dict) -> Path:
    """An adapter that emits `payload` and looks at nothing.

    A stub rather than a loaded runner because the condition under test is a
    runner nobody here can produce on demand -- which is the whole reason the
    two occurrences are all the evidence there is.
    """
    p = tmp_path / "stub-adapter.py"
    p.write_text(
        "import sys\nsys.stdout.write(%r)\n" % json.dumps(payload),
        encoding="utf-8",
    )
    return p


@contextlib.contextmanager
def _a_skip_here_is_a_failure():
    """Turn a decline inside this block into a red.

    `pytest.skip` is green, so a guard that stops running because the thing
    it guards went wrong reports exactly what a guard that ran and passed
    reports. Every assertion in this file that says "this payload is a
    verdict and must reach its assertion" is wrapped in this; only the two
    that assert a decline are not.
    """
    try:
        yield
    except pytest.skip.Exception as exc:
        raise AssertionError(
            "skip_if_stalled declined a payload that is a verdict about the "
            "file, so the assertion guarding it never ran -- and a skip is "
            "green. Reason given: %s" % exc) from None


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """Point `_run` at a stub adapter; yield a caller taking the payload."""
    def run_with(payload: dict):
        monkeypatch.setattr(html_tests, "ADAPTER", _stub_adapter(tmp_path, payload))
        return html_tests._run(str(tmp_path / "dashboard.html"))
    return run_with


def test_a_stalled_adapter_declines_rather_than_failing(stubbed) -> None:
    """The reported failure, and the one behaviour change here."""
    with pytest.raises(pytest.skip.Exception) as caught:
        stubbed(_stall(duration_ms=30_004))
    assert TIMEOUT_MSG in str(caught.value), str(caught.value)


def test_a_clean_verdict_on_a_broken_page_still_fails(tmp_path, monkeypatch) -> None:
    """The bar: the weakening must not survive `html-check` reporting clean.

    Runs the reported test itself against an adapter that says the broken
    page is fine. If this ever passes, the gate has stopped being a gate.
    """
    monkeypatch.setattr(html_tests, "ADAPTER", _stub_adapter(tmp_path, _clean()))
    with _a_skip_here_is_a_failure(), pytest.raises(AssertionError):
        html_tests.test_broken_inline_script_is_a_finding(tmp_path)


def test_a_wrong_line_on_a_real_finding_still_fails(tmp_path, monkeypatch) -> None:
    """And the pin itself is untouched when the adapter actually parsed."""
    misplaced = _finding()
    misplaced["errors"][0]["line"] = 2
    monkeypatch.setattr(html_tests, "ADAPTER", _stub_adapter(tmp_path, misplaced))
    with _a_skip_here_is_a_failure(), pytest.raises(AssertionError):
        html_tests.test_broken_inline_script_is_a_finding(tmp_path)


def test_an_overeager_predicate_reddens_the_guards_rather_than_skipping_them(
    stubbed, monkeypatch,
) -> None:
    """The guards below have to fail, not skip, and that is not automatic.

    `skip_if_stalled` declines by raising, and a decline is GREEN. So every
    guard here that says "this payload IS a verdict and must reach the
    assertion written for it" would, if the predicate ever widened to swallow
    it, stop running rather than go red -- the same absence-read-as-presence
    this whole change is about, one layer further in, in the code that exists
    to catch it. `_a_skip_here_is_a_failure` is what converts it back, and
    this is the test that proves it does.
    """
    monkeypatch.setattr(
        verdicts, "stalled_at_its_own_wall",
        lambda payload, *, inner_s: "a predicate that calls everything a wall")
    with pytest.raises(AssertionError, match="never ran"):
        with _a_skip_here_is_a_failure():
            stubbed(_finding())


def test_a_real_finding_comes_back_unchanged(stubbed) -> None:
    """A parse error is a verdict about the file and must reach the assertion."""
    with _a_skip_here_is_a_failure():
        assert stubbed(_finding()) == _finding()


def test_a_clean_verdict_comes_back_unchanged(stubbed) -> None:
    with _a_skip_here_is_a_failure():
        assert stubbed(_clean()) == _clean()


def test_the_third_state_comes_back_unchanged(stubbed) -> None:
    """`skipped` payloads carry no `ok` at all -- eleven tests here assert on them."""
    third = {"tool": "html-check", "file": "x.html", "duration_ms": 12,
             "skipped": "NO inline <script> block in this file was checked"}
    with _a_skip_here_is_a_failure():
        assert stubbed(third) == third


def test_a_timeout_reported_in_12ms_still_fails(stubbed) -> None:
    """An adapter that says `timeout` in 12ms did not time out.

    That is broken error routing in the thing this suite tests, and a
    message-only gate would swallow it.
    """
    with _a_skip_here_is_a_failure():
        assert stubbed(_stall(duration_ms=12))["errors"][0]["msg"] == TIMEOUT_MSG


def test_an_absent_node_still_fails(stubbed) -> None:
    """`node` missing is an `adapter` error and is NOT a wall.

    It is instant, it is actionable, and `test_missing_node_is_skipped_not_ok`
    is the test that owns it. A gate that let it through would hide an
    unconfigured runner behind a skip on every single test in the file.
    """
    absent = _stall(duration_ms=30_004)
    absent["errors"][0]["msg"] = "node not found on PATH; install Node.js"
    with _a_skip_here_is_a_failure():
        assert stubbed(absent) == absent


def test_the_gate_reads_the_budget_off_the_adapter() -> None:
    """No second copy of 30 (#702).

    A literal here would keep passing after someone changed the adapter's own
    `TIMEOUT_S`, and the gate would then classify a real stall as a verdict.
    """
    assert html_tests.INNER_S == inner_budget(html_tests.ADAPTER)
    assert html_tests.INNER_S >= 1


def test_skip_if_stalled_returns_the_payload_when_there_is_one() -> None:
    """The helper's own contract, independent of `_run`."""
    payload = _finding()
    assert skip_if_stalled(payload, inner_s=30) is payload
