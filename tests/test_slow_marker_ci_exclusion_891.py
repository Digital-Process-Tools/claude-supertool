"""#891: mark the four heaviest local tests `slow`, and — the owner's scope
change on top of the issue as filed — stop CI from running `slow` too.

`pyproject.toml`'s `addopts` already excludes `slow` from the local default
run. `.github/workflows/tests.yml` overrides `-m` and therefore *included*
`slow` on every one of the twelve legs, at full duration, on every push. That
made the marker cosmetic for CI: adding it changed the local loop and changed
nothing else. This file pins the three assertions that make it not cosmetic:
the named tests actually carry the marker (checked through pytest's own
collection, not by grepping source for the decorator spelling), the CI job's
`-m` expression actually excludes `slow`, and a `--durations=25` leg exists so
the next "which test is slow on Windows" question has an answer in the log
instead of a guess.

Excluding `slow` from CI's twelve legs would otherwise mean these tests run
**nowhere** — and they are timeout/hang/network-failure disclosure tests,
this repo's most-filed defect class. So a fourth thing is pinned here too:
that a scheduled workflow exists whose job actually asks for `-m slow` on at
least one platform. Not asserting *content correctness* of that job (this
file cannot run a GitHub Actions cron), only that the shape exists: a
`schedule:` trigger, and a run step whose `-m` expression contains `slow`
without negating it.

`test_a_network_failure_is_not_reported_as_a_refusal` (test_image_fetch_ssrf_817.py)
is deliberately excluded from the "must be marked" list. #922 (open, not
merged) is reported to bring its cost from ~20s to ~0.5s; marking it here
would need revisiting the moment that lands, so it is left unmarked and this
file pins that it stays that way until someone touches it on purpose.
"""

from __future__ import annotations

import re
import subprocess
import sys

from _workflow_parse import REPO, job_blocks, job_steps, matrix_os, run_blocks

SLOW_WORKFLOW = REPO / ".github" / "workflows" / "slow-tests.yml"

_TARGET_TEST_FILES = [
    "tests/test_git_push_hazards_640_642_647.py",
    "tests/test_edge_cases_batch_security.py",
    "tests/test_image_fetch_ssrf_817.py",
]

_MUST_BE_SLOW = [
    "test_fetch_timeout_gives_a_verdict_not_a_traceback",
    "test_rebase_timeout_fixture_survives_a_slow_helper_spawn",
    "test_batch_at_cap_runs_to_completion",
]

_NOT_YET_SLOW = "test_a_network_failure_is_not_reported_as_a_refusal"

#: Only the *quoted* form. A run block routinely carries other `-m` flags
#: that are module invocations, not marker expressions — `python -m pip`,
#: `python -X utf8 -m pytest`, `python -m coverage` — and every one of them
#: is unquoted. Matching bare `-m\s+(\S+)` too means "does this job exclude
#: slow" can be answered by the word "pip", which is the house defect
#: arriving in this file's own parser: a read that finds *something* and
#: reports it as the answer to the question actually asked.
_DASH_M_RE = re.compile(r"-m\s+(['\"])(.*?)\1")


def _dash_m_exprs(run: str) -> list[str]:
    """Every quoted `-m` marker expression in a shell run block, in order."""
    return [match.group(2) for match in _DASH_M_RE.finditer(run)]


def _collect_under_slow() -> str:
    """Real pytest collection output under `-m slow`, not a grep of source.

    A grep for `@pytest.mark.slow` cannot tell a marker that is present but
    misspelled, mis-indented, or attached to the wrong test from one that
    genuinely applies — pytest's own collector can.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "slow",
         "--no-cov", "-p", "no:cacheprovider", *_TARGET_TEST_FILES],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    return result.stdout + result.stderr


def test_the_three_heavy_tests_collect_under_dash_m_slow() -> None:
    out = _collect_under_slow()
    missing = [name for name in _MUST_BE_SLOW if name not in out]
    assert not missing, (
        f"{missing} did not collect under `pytest -m slow` — the "
        "`@pytest.mark.slow` decorator is missing, misspelled, or attached "
        f"to the wrong test. Collection output:\n{out}")


def test_the_ssrf_network_failure_test_is_deliberately_not_marked_slow() -> None:
    out = _collect_under_slow()
    assert _NOT_YET_SLOW not in out, (
        f"{_NOT_YET_SLOW} now collects under `-m slow`. #891's brief left it "
        "unmarked because #922 (open, not merged) is expected to fix its cost "
        "from ~20s to ~0.5s — if it has been marked on purpose because #922 "
        "stalled or a fresh measurement still shows it heavy, update this "
        "test to say so instead of deleting it.")


def test_ci_pytest_job_excludes_slow_and_benchmark() -> None:
    blocks = job_blocks()
    steps = job_steps(blocks["pytest"])
    runs = run_blocks(steps)
    exprs = [expr for run in runs for expr in _dash_m_exprs(run)]
    assert exprs, "no quoted `-m` expression found in the pytest job's run steps"
    assert any("not slow" in expr and "not benchmark" in expr for expr in exprs), (
        f"pytest job's `-m` expressions are {exprs!r}, none of which exclude "
        "both slow and benchmark. #891's scope change means CI must exclude "
        "`slow` the same way the local default does — leaving it in the CI "
        "`-m` override runs these tests on every one of the twelve legs, on "
        "every push, which is the exact cost the marker was supposed to "
        "remove.")


def test_ci_pytest_job_reports_durations() -> None:
    blocks = job_blocks()
    steps = job_steps(blocks["pytest"])
    runs = run_blocks(steps)
    assert any("--durations=25" in run for run in runs), (
        "no pytest-job leg passes --durations=25. #891 scope item 3: nobody "
        "has identified which tests are actually slow on Windows, and "
        "without this flag a stalled-looking leg has nothing in the log to "
        "say which test is running.")


def test_a_scheduled_slow_workflow_exists() -> None:
    assert SLOW_WORKFLOW.exists(), (
        f"{SLOW_WORKFLOW} does not exist. Excluding `slow` from the twelve "
        "CI legs means those tests — timeout/hang/network-failure disclosure "
        "tests, this repo's most-filed defect class — run nowhere at all "
        "unless a separate scheduled workflow runs `-m slow` on a cron.")


def test_the_scheduled_workflow_runs_on_a_schedule_trigger() -> None:
    text = SLOW_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^\s*schedule:\s*$", text, re.M), (
        f"{SLOW_WORKFLOW} exists but declares no `schedule:` trigger — a "
        "workflow that only runs on manual dispatch is not a standing check, "
        "it is a button nobody presses.")


def test_the_scheduled_workflow_asks_for_slow_tests_on_at_least_ubuntu() -> None:
    text = SLOW_WORKFLOW.read_text(encoding="utf-8")
    blocks = job_blocks(text)
    assert blocks, f"no jobs parsed out of {SLOW_WORKFLOW}"
    found_on_ubuntu = False
    for block in blocks.values():
        oses = matrix_os(block)
        runs_on_ubuntu = any("ubuntu-latest" in os_name for os_name in oses) \
            or "ubuntu-latest" in block
        if not runs_on_ubuntu:
            continue
        for run in run_blocks(job_steps(block)):
            for expr in _dash_m_exprs(run):
                if "slow" in expr and "not slow" not in expr:
                    found_on_ubuntu = True
    assert found_on_ubuntu, (
        f"no job in {SLOW_WORKFLOW} runs on ubuntu-latest with a `-m` "
        "expression that positively selects `slow` — the scheduled job must "
        "run the tests CI's main matrix now skips, on at least one platform.")
