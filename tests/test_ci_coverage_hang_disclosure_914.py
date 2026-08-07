"""When the `coverage` job hangs, the log must name the test (#914).

Three runs of `coverage (floors + disclosure)` were killed at the
`timeout-minutes: 20` ceiling on 2026-08-07, on three unrelated branches —
`21a7969` (fix/883), `8b14b9b` (fix/850) and `0c08cd1` (master) — against a
3.0-3.6 minute baseline across the other sixteen runs that day. All three logs
end the same way: a progress bar, a multi-minute silence, `##[error]The
operation was canceled.`, and then six `Terminate orphan process: pid (N)
(python)` lines. No assertion, no test name, nothing.

**A cancelled leg is neither a pass nor a fail.** The merge gate here is
arithmetic (#454), so each occurrence forces a hand-check of a board that
otherwise reads clean, and `master` sat NOT GREEN on a commit whose code was
fine. Re-running clears the symptom and destroys the evidence.

**This file does not fix the hang. It makes the next one legible.** The cause
is still open, and it is currently undiagnosable, which is the thing being
removed here rather than the thing being fixed.

**Why a per-test budget and not `faulthandler.dump_traceback_later`.** The
constraint that decides it is that the output has to survive to the job log,
and the runner cancels rather than tearing down gracefully. A faulthandler dump
leaves the hang in place: the job still runs to the ceiling, still gets killed,
and whether a dump written minutes earlier reaches the log stream before the
kill is exactly the property that cannot be established. Worse, this job runs
under `-n auto`, so a dump written to a *worker's* stderr is relayed by execnet
rather than written to the job's own fd 2, and there is no reason to think it
arrives. A per-test budget has no such race: it turns the hang into an ordinary
pytest failure, the run finishes in its usual three minutes, and the job writes
its log the normal way because it was never cancelled. That is not a better
chance of surviving — it is the removal of the thing it had to survive.

**pytest-timeout is not a new dependency.** It is in the dev extras, it is
already `pip install`ed by this very job, and the `notifiers` job has armed it
on the step that hung since #722 — where its own comment says the job ceiling
"can only ever say 'this leg did not finish'" while the per-test budget "names
the test and dumps the stack". The abstraction existed; one of three job
classes had not adopted it. The `pytest` job's non-adoption is deliberate and
stated (`test_ci_job_timeouts_722.py`), and its stated reason — no per-test
timing data for a contended windows leg — does not apply here: this job is one
leg, ubuntu, py3.12, and its per-test timings are measured below.

**The number, from measurement rather than theory.** The full suite run through
`coverage_gate.suite_argv` (`-m 'not benchmark'`, so slow tests included, under
`parallel = true` and `COVERAGE_PROCESS_START`), `--durations`, 7214 passed in
199s. The slowest three tests:

| test | seconds |
| --- | --- |
| `test_edge_cases_batch_security.py::TestHugeBatch::test_batch_at_cap_runs_to_completion` | 50.2 |
| `test_git_push_hazards_640_642_647.py::test_fetch_timeout_gives_a_verdict_not_a_traceback` | 33.9 |
| `test_image_fetch_ssrf_817.py::test_a_network_failure_is_not_reported_as_a_refusal` | 20.5 |

300s is ~6x the worst of those, which is the #702 trade taken in the same
direction the two existing budgets take it: sized so it can only fire for a
hang and not for a slow runner. It is also a quarter of the 20-minute job
ceiling, so the inner guard always fires first and the run still completes
inside the outer budget even with several tests hung — the ordering
`tests/_adapter_budget.py` insists on, here between a script and its job. For
scale, the three real stalls were 12, 17 and 12 minutes of silence; 300s names
all three.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from _workflow_parse import job_blocks, job_budget

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / ".github" / "scripts" / "coverage_gate.py"

#: The worst per-test duration measured under this job's own instrumentation,
#: rounded up. Recorded so tightening the budget past the point where a slow
#: runner could trip it fails here with the evidence attached, rather than in a
#: red leg nobody caused. Re-measure with `--durations` before changing it.
SLOWEST_TEST_OBSERVED_S = 51

#: Below this multiple the budget stops guarding a hang and starts measuring
#: the runner, which is #702's inversion arriving one layer up.
MIN_HEADROOM_FACTOR = 4


def _gate():
    spec = importlib.util.spec_from_file_location("coverage_gate", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the post-condition ----------------------------------------------------


_HANGING_TEST = '''
import subprocess, sys


def test_a_test_that_never_returns():
    """Blocks reading a pipe a *grandchild* still holds open.

    Deliberately this shape and not `time.sleep`: the job under guard is the
    only one running under coverage instrumentation, which puts a `coverage`
    hook in every one of ~900 spawned children, and the three cancelled logs
    ended with six orphan python processes still alive. A sleep would be
    interrupted by anything; a parent blocked in `read(2)` on a descriptor an
    unrelated grandchild inherited is the shape that could plausibly defeat a
    signal-based guard, so it is the one worth asserting against.
    """
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys;"
         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'],"
         " stdout=sys.stdout);"
         "sys.exit(0)"],
        stdout=subprocess.PIPE)
    child.communicate()


def test_a_test_that_returns():
    assert True
'''

#: Stands in for `PER_TEST_TIMEOUT_S` in the behavioural test only. The real
#: number is 300s and a test may not take five minutes to prove a point; what
#: is exercised is the argv the job really builds, through the same parameter,
#: with one value swapped.
#:
#: Passed as `suite_argv(..., timeout_s=)` and deliberately *not* as an extra
#: `--timeout=5` appended to the argv. The first draft did the latter, and it
#: passed against a `suite_argv` that armed nothing at all — the test supplied
#: the guard it was meant to be checking for. Routing it through the parameter
#: means there is no budget here unless the implementation has one.
_FIXTURE_TIMEOUT_S = 5

#: How long the harness itself waits before calling the guard absent. Well over
#: the fixture budget plus two interpreter starts under coverage, and well
#: under anything a contended runner turns into a false red.
_HARNESS_BUDGET_S = 180


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the coverage job is `runs-on: ubuntu-latest`, so how pytest-timeout "
           "falls back to its thread method on Windows is not a property of the "
           "job this file guards")
def test_a_test_that_never_returns_is_named_instead_of_killing_the_leg(
        tmp_path: Path) -> None:
    """The whole of #914's item 1, as a post-condition rather than a proxy.

    Runs the *real* argv from `coverage_gate.suite_argv` over a fixture that
    genuinely does not return, and asserts the run ends by itself with the
    hung test named. Delete the `--timeout` from `suite_argv` and this goes
    red by hanging until `_HARNESS_BUDGET_S`, which is the failure it is
    describing, reproduced.
    """
    gate = _gate()
    target = tmp_path / "test_ci_914_fixture.py"
    target.write_text(_HANGING_TEST, encoding="utf-8")

    argv = gate.suite_argv(
        gate.write_config(tmp_path / "cov"),
        "-p", "no:cacheprovider",
        "-n", "2",
        str(target),
        timeout_s=_FIXTURE_TIMEOUT_S,
    )
    try:
        done = subprocess.run(argv, cwd=str(tmp_path), capture_output=True,
                              encoding="utf-8", errors="replace",
                              timeout=_HARNESS_BUDGET_S)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the suite argv did not terminate within {_HARNESS_BUDGET_S}s on a "
            "test that never returns — which is the #914 cancellation, "
            f"reproduced. argv: {argv}")

    out = done.stdout + done.stderr
    assert "gw0" in out or "gw1" in out, (
        "the fixture did not run under xdist, so this proves nothing about the "
        "job — which runs `-n auto`, and whether a guard that fires inside a "
        "*worker* reaches the controller's stdout is the whole question. The "
        "`-n 2` above is explicit because the run happens in a tmp_path with no "
        f"pyproject to inherit `addopts` from. Output:\\n{out[-3000:]}")
    assert "test_a_test_that_never_returns" in out, (
        "the run ended but the hung test is not named in its output, so a real "
        f"occurrence would still read as a shrug. Output:\n{out[-3000:]}")
    assert done.returncode != 0, (
        "a test that never returned was reported as a passing run")
    assert "1 failed, 1 passed" in out, (
        "the guard did not fail exactly the hung test and pass the other one. "
        "Taking the whole session down would be a worse trade than the "
        "cancellation: the point is one named failure in a run that otherwise "
        f"completes and reports. Output:\\n{out[-3000:]}")


# --- the wiring, so the post-condition is about the job that runs ----------


def test_the_job_arms_a_per_test_budget_at_all() -> None:
    """Read off `suite_argv`, not off a comment quoting it.

    #731's lesson in `test_ci_job_timeouts_722.py`: that file searched a whole
    job block including the comment that justified the flag, so deleting the
    real `--timeout=30` left it green.
    """
    argv = _gate().suite_argv(Path("cfg.ini"))
    armed = [a for a in argv if a.startswith("--timeout")]
    assert armed, (
        "the coverage job's pytest invocation arms no per-test budget, so a "
        "hang in it is bounded only by `timeout-minutes: 20` — which cancels "
        f"the leg and names nothing. argv: {argv}")


def test_the_budget_is_the_one_the_module_documents() -> None:
    """One number, in one place, so it cannot drift from its justification."""
    gate = _gate()
    argv = gate.suite_argv(Path("cfg.ini"))
    assert f"--timeout={gate.PER_TEST_TIMEOUT_S}" in argv, (
        "`suite_argv` passes a per-test budget that is not "
        f"`PER_TEST_TIMEOUT_S` ({gate.PER_TEST_TIMEOUT_S}). The constant is "
        "where the measurement justifying the number is written down; a "
        "literal beside it is a second place for it to be wrong.")


def test_the_budget_is_not_tightened_into_a_benchmark() -> None:
    """#702, one layer up: a budget a slow runner can trip is worse than none.

    It converts a genuine green into a red, and what everyone learns is to
    press re-run — after which the guard is not read at all.
    """
    budget = _gate().PER_TEST_TIMEOUT_S
    floor = SLOWEST_TEST_OBSERVED_S * MIN_HEADROOM_FACTOR
    assert budget >= floor, (
        f"the per-test budget is {budget}s, under the {floor}s floor — "
        f"{MIN_HEADROOM_FACTOR}x the slowest test measured under this job's "
        f"own instrumentation ({SLOWEST_TEST_OBSERVED_S}s, "
        "`test_batch_at_cap_runs_to_completion`). Below that it stops guarding "
        "a hang and starts measuring the runner.")


def test_the_job_ceiling_sits_above_the_budget_it_guards() -> None:
    """The #716 rule, read out of both files rather than tabulated beside them.

    A job ceiling at or under the per-test budget could never let the inner
    guard fire: the leg would be cancelled first and the named failure would
    never be written. That is the state this job was in before #914 — the
    inner budget was infinite.
    """
    inner = _gate().PER_TEST_TIMEOUT_S
    outer = job_budget(job_blocks()["coverage"])
    assert outer is not None, "the coverage job declares no timeout-minutes"
    assert outer * 60 > inner, (
        f"the coverage job ceiling ({outer} min) is at or under its own "
        f"per-test budget ({inner}s), so the inner guard can never fire and a "
        "hung test is reported as a dead leg instead of a named failure.")


def test_the_budget_leaves_room_for_the_run_it_bounds() -> None:
    """A guard that fires and still blows the ceiling has changed nothing.

    The baseline run is ~3.5 min; the ceiling is 20. A per-test budget large
    enough that one hung test plus the baseline exceeds the ceiling would
    still end in a cancellation with no log, which is the defect.
    """
    inner = _gate().PER_TEST_TIMEOUT_S
    outer = job_budget(job_blocks()["coverage"])
    assert outer is not None and inner * 3 < outer * 60, (
        f"a {inner}s per-test budget against a {outer} min ceiling leaves room "
        "for fewer than three hung tests before the leg is cancelled anyway. "
        "Either tighten the budget or state why one occurrence is enough.")
