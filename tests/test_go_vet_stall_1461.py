"""A `go vet` that ran out of wall is not a verdict about the package (#1461).

`pytest (windows-latest, 3.10)` job #94089832110, on PR #1457 — a PR touching
`presets/_checks.py` and nothing else:

    FAILED tests/test_validators_go_vet_669.py::test_a_clean_package_is_clean
      AssertionError: expected a clean verdict — go-vet reported ok=False count=1
      after 60138ms: [adapter] go vet timed out after 60s

**The adapter is right and the suite was wrong.** `validators/SCHEMA.md`
reserves `code: "adapter"` for exactly this — "a binary that is absent, a
timeout, output that would not parse" — and says it "stays a real error ...
`ok: false`, `count: 1`", because `ok` and `count` are the channel this schema
gives an absence. The core reads such a payload as `NOT CHECKED`, never
subtracts it from a baseline and never rolls an edit back (`docs/validators.md`,
"Declining instead of guessing"). Routing it to `skipped()` instead would make
a hung validator quiet and exit 0 — the mute button
`validators/common/refusal.py` declines by name.

What read it as a verdict was `test_a_clean_package_is_clean`, asserting a lint
result about a package no tool had finished analysing. That is #794 and #1296
again in a fourth adapter suite: `_adapter_verdict.skip_if_stalled` already
exists for it and `tests/test_validators_go_vet_669.py` never adopted it.

Two things had to move, and only the first is obvious:

* the suite routes every adapter spawn through `skip_if_stalled`;
* `stalled_at_its_own_wall` matched the substring `timeout` only, and this
  adapter says **"timed out"**. The predicate could not see the one payload
  that produced the report at all.

`validators/pyright/pyright.py` is invisible to it for the same reason
("pyright timed out after 60s") *and* a second one — it codes that error
`timeout` rather than `adapter`, so the core never renders it `NOT CHECKED`
either. Filed, not fixed here.

The cold-cache cost is measured rather than argued (macOS, go1.22.3, `GOCACHE`
pointed at an empty directory): **cold 3.54s, warm 0.24s, ~15x**. `go vet`
compiles the standard library before analysing anything, once per build cache;
that is a property of the machine, not of the package. The suite now pays it
once in a throwaway module outside the adapter's wall — a second, never-seen
module vets in 0.22s after that, because `GOCACHE` is content-keyed and not
module-scoped (measured the same way).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import _adapter_verdict as verdicts
import test_validators_go_vet_669 as go_vet_tests


def _adapter_module():
    """The go-vet adapter imported as a module, so its arms can be provoked."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "go_vet_adapter_1461", go_vet_tests.ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Clock:
    """`time.time()` readings, in order. The adapter takes exactly two."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)

    def time(self) -> float:
        return self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]


def _timeout_payload(monkeypatch, tmp_path: Path, capsys) -> dict:
    """The adapter's own timeout payload, provoked rather than hand-written.

    Hand-writing it would pin this file to a message the adapter is free to
    reword, which is the failure one layer in: a test asserting a shape nothing
    produces. The clock is faked because `duration_ms` is load-bearing —
    `stalled_at_its_own_wall`'s fourth clause requires the adapter to have
    spent its whole budget, and an adapter reporting a timeout in 12ms has a
    broken error route that must stay red.
    """
    mod = _adapter_module()
    root = go_vet_tests._module(tmp_path, {"pkg/a.go": go_vet_tests.CLEAN})

    class _Wall:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="go vet .", timeout=mod.TIMEOUT)

    monkeypatch.setattr(mod, "subprocess", _Wall)
    monkeypatch.setattr(mod, "time", _Clock(1000.0, 1000.0 + mod.TIMEOUT + 0.138))
    monkeypatch.setattr(sys, "argv", ["go-vet.py", str(root / "pkg" / "a.go")])
    mod.main()
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# The payload the runner produced
# ---------------------------------------------------------------------------

def test_the_adapter_publishes_a_timeout_as_no_verdict_not_as_a_finding(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """What the adapter does is already correct, and is pinned so it stays so.

    `ok: false, count: 1` here is not a claim about the package; it is the only
    channel `SCHEMA.md` gives an absence, and `code: "adapter"` is what makes
    the core render it `NOT CHECKED`, keep it out of the delta, refuse to cache
    it and refuse to roll an edit back over it. A well-meant change to
    `skipped()` would take all four away and exit 0.
    """
    out = _timeout_payload(monkeypatch, tmp_path, capsys)
    assert "skipped" not in out, out
    assert out["ok"] is False, out
    assert [e["code"] for e in out["errors"]] == ["adapter"], out
    assert out["duration_ms"] >= go_vet_tests.INNER_S * 1000, out


def test_that_payload_is_classified_as_a_wall_and_not_as_a_verdict(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """The predicate has to *see* it, and it did not.

    `stalled_at_its_own_wall` tested for the substring `timeout`; this adapter
    writes "timed out". Every other clause matched, so the one payload that
    produced #1461 was the one shape the guard could not classify.
    """
    out = _timeout_payload(monkeypatch, tmp_path, capsys)
    reason = verdicts.stalled_at_its_own_wall(out, inner_s=go_vet_tests.INNER_S)
    assert reason is not None, (
        "the guard could not recognise the adapter's own timeout payload: %r"
        % (out,))
    assert str(go_vet_tests.INNER_S) in reason, reason


# ---------------------------------------------------------------------------
# What the suite does with it
# ---------------------------------------------------------------------------

@pytest.fixture
def stubbed(monkeypatch):
    """Point the suite's `_run` at a fixed payload; return a caller."""
    def run_with(payload: dict):
        completed = subprocess.CompletedProcess(
            args=["go-vet.py"], returncode=0, stdout=json.dumps(payload), stderr="")
        monkeypatch.setattr(go_vet_tests, "_spawn",
                            lambda *a, **k: completed)
        return go_vet_tests._run(Path("pkg/a.go"))
    return run_with


@contextlib.contextmanager
def _a_skip_here_is_a_failure():
    """Turn a decline inside this block into a red.

    `pytest.skip` is green, so a guard that stops running because the thing it
    guards went wrong reports what a guard that ran and passed reports.
    """
    try:
        yield
    except pytest.skip.Exception as exc:
        raise AssertionError(
            "the suite declined a payload that is a verdict about the package, "
            "so the assertion guarding it never ran — and a skip is green. "
            "Reason given: %s" % exc) from None


def test_the_suite_declines_a_stall_rather_than_asserting_on_it(
        tmp_path: Path, monkeypatch, capsys, stubbed) -> None:
    """The reported failure, and the one behaviour change in the suite."""
    payload = _timeout_payload(monkeypatch, tmp_path, capsys)
    with pytest.raises(pytest.skip.Exception) as caught:
        stubbed(payload)
    assert "budget" in str(caught.value), str(caught.value)


def test_a_real_vet_finding_still_reaches_its_assertion(stubbed) -> None:
    """The bar: nothing that IS a verdict may start declining.

    A printf diagnostic is `ok: false, count: 1` too. Only the code, the
    message and the elapsed time tell it from a wall.
    """
    finding = {"tool": "go-vet", "file": "pkg/a.go", "ok": False, "count": 1,
               "errors": [{"line": 6, "col": 5, "severity": "warning",
                           "code": None, "msg": "Printf format %d has arg of "
                                                "wrong type string"}],
               "duration_ms": 240}
    with _a_skip_here_is_a_failure():
        assert stubbed(finding) == finding


def test_a_wrong_clean_verdict_still_fails_the_test_that_pins_it(
        tmp_path: Path, monkeypatch, stubbed) -> None:
    """And the weakening must not survive go-vet reporting a clean package dirty.

    Runs the reported test itself against an adapter that reports a finding on
    a clean package. If this ever stops raising, the gate has stopped gating.
    """
    completed = subprocess.CompletedProcess(
        args=["go-vet.py"], returncode=0, stderr="",
        stdout=json.dumps({"tool": "go-vet", "file": "pkg/a.go", "ok": False,
                           "count": 1,
                           "errors": [{"line": 3, "col": 1, "severity": "warning",
                                       "code": None, "msg": "invented"}],
                           "duration_ms": 240}))
    monkeypatch.setattr(go_vet_tests, "_spawn", lambda *a, **k: completed)
    with _a_skip_here_is_a_failure(), pytest.raises(AssertionError):
        go_vet_tests.test_a_clean_package_is_clean(tmp_path)


# ---------------------------------------------------------------------------
# The cold build cache, paid once and outside the adapter's wall
# ---------------------------------------------------------------------------

def test_the_warmup_names_its_own_failure_instead_of_raising(
        tmp_path: Path, monkeypatch) -> None:
    """A warm-up that blows its budget must report, not kill the collection.

    It is a convenience, not a gate: every real assertion below it still has
    `skip_if_stalled` under it. So the honest outcome is a named reason a
    reader can act on, and the budget is in it — a reason that does not say
    what was exceeded cannot be acted on.
    """
    class _Wall:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="go vet .", timeout=1)

    monkeypatch.setattr(go_vet_tests, "subprocess", _Wall)
    reason = go_vet_tests._warm_the_go_build_cache(tmp_path)
    assert reason is not None
    assert str(go_vet_tests.GO_WARMUP_S) in reason, reason


def test_the_warmup_reports_a_spawn_the_os_refused(
        tmp_path: Path, monkeypatch) -> None:
    """Windows raises `FileNotFoundError [WinError 2]` where POSIX may not fail
    at all (#997). The warm-up is a `subprocess.run` like any other."""
    class _Boom:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            raise OSError()  # no errno, no strerror — both None

    monkeypatch.setattr(go_vet_tests, "subprocess", _Boom)
    reason = go_vet_tests._warm_the_go_build_cache(tmp_path)
    assert reason is not None
    assert "OSError" in reason, reason
    assert "None" not in reason, reason


def test_the_warmup_actually_runs_before_the_spawns_it_exists_for() -> None:
    """Structural, because the effect is a duration and a duration is a benchmark.

    What the warm-up buys is that no adapter spawn is the first `go vet` on the
    machine. Asserting that by timing would put a benchmark in a correctness
    test, which `tests/_adapter_budget.py` argues against at length and for the
    same reason: the number becomes the assertion and it fails on a busy runner
    rather than on a defect. So the two properties that make it work are pinned
    directly — dropping either leaves every test in the suite passing here and
    slower where it matters, which is precisely the change nothing else catches.
    """
    fixture = go_vet_tests._go_build_cache_is_warm
    # `_pytestfixturefunction` on the plain function was the older spelling and
    # is gone: pytest 8.4 wraps a fixture in `FixtureFunctionDefinition` and
    # keeps the marker on `_fixture_function_marker`. Read off the object
    # rather than guessed, because a `getattr(..., None)` here would turn a
    # renamed attribute into a green — this assertion going quiet is the same
    # defect as the one it guards.
    marker = fixture._fixture_function_marker
    assert marker.autouse is True, marker
    assert marker.scope == "module", marker


def test_the_warmup_returns_nothing_to_report_when_go_answers(
        tmp_path: Path) -> None:
    """A non-zero exit is not a failure of the warm-up: `go vet` on the probe
    module is allowed to have opinions about it. Only not answering is."""
    if not go_vet_tests.shutil.which("go"):
        pytest.skip("go not on PATH")
    reason = go_vet_tests._warm_the_go_build_cache(tmp_path)
    # This file has no warm-up fixture of its own, so on a cold runner this is
    # the call that pays the standard-library compile. A budget it did not fit
    # in is a statement about the machine and declines — the same distinction
    # the whole change is about, applied to the change's own test. Any *other*
    # reason is the helper being wrong and stays red.
    if reason is not None and str(go_vet_tests.GO_WARMUP_S) in reason:
        pytest.skip(reason)
    assert reason is None, reason
