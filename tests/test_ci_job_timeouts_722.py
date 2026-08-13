"""Every CI job declares a wall-clock budget, and the budget is a hang-guard (#722).

`notifiers (bun + TypeScript) (ubuntu-latest)` on PR #715 ran 26 minutes and was
still running when it was killed by hand. The same job takes 24-37s, its macOS
twin passed in 32s on the same PR, and #715 touched no TypeScript. It was stuck
on step 9, "Run the channel integration tests for real" — a socket wait, most
likely, which is #554's ground.

The defect this file guards is not the hang. It is that **no job and no step in
`.github/workflows/tests.yml` declared `timeout-minutes`**, so GitHub's six-hour
default applied and the board rendered `13 passed, 0 failed, 1 pending` — exactly
what a leg that is about to finish looks like. The states summed to the leg
count, so #454's arithmetic check passed cleanly. Nothing anywhere distinguished
"still working" from "will never finish", and `gh api .../jobs/<id>/logs` returns
`BlobNotFound` while a job is in progress, so the hang was unobservable until it
ended — and a job cancelled by hand never writes a log at all. A job killed by
`timeout-minutes` *does*: it fails, loudly, with its log written. That is the
whole point.

**Sizing, from 70 job observations across five runs on master rather than from
theory** (`gh api .../actions/runs/<id>/jobs`, 2026-08-01):

| job class | observed | budget | ratio |
| --- | --- | --- | --- |
| `pytest`, windows-latest | 483-574s | 30 min | 3.1x the worst |
| `pytest`, macos-latest | 128-197s | 30 min | 9.1x the worst |
| `pytest`, ubuntu-latest | 93-125s | 30 min | 14x the worst |
| `notifiers` | 24-37s | 10 min | 16x the worst |

**Two numbers, not one, and that is the load-bearing decision.** Copying a single
figure across both job classes is what #702 was filed about: a budget that fires
on a *slow* runner instead of a hung one converts a real green into a red and
teaches everyone to re-run, which is worse than the disease. The windows pytest
legs are consistently ~530s and the notifiers legs are consistently ~30s; one
number generous enough for the first would be ~50x the second, i.e. no guard at
all on the job that actually hung.

**Per-OS budgets inside the pytest matrix were considered and declined.** A
`${{ matrix.os == 'windows-latest' && 30 || 15 }}` would bound the ubuntu legs
tighter, but the difference it buys — a hung ubuntu leg noticed at 15 minutes
instead of 30 — is noise against the 360 it replaces, and it is three numbers to
keep in step instead of one. The failure being *bounded* is the deliverable.

**What is asserted here, and what cannot be.** Nothing runnable can make CI hang
on demand, so no test here proves a budget is large enough on a loaded runner or
that it fires on a real hang — such a test would pass on every machine that does
not have the problem, which is the defect and not the fix. What is pinned is
policy, and policy holds identically on a fast laptop and a crawling runner:
that every job declares a budget at all (so job #15 cannot arrive without one),
that the numbers stay inside a stated band at both ends, that the step which hung
also carries an inner per-test guard, and that the job ceiling sits above that
inner guard so the inner one can always fire first.
"""

from __future__ import annotations

import re

import pytest

from _workflow_parse import job_blocks, job_budget, job_steps

#: pytest-timeout's per-test budget. Matched against a *step's* `run:` block
#: and never against the job text: until #731 it was searched across the whole
#: `notifiers` block, and the comment justifying the flag quotes it twelve
#: lines above the flag, so deleting the real `--timeout=30` left this file
#: 14/14 green.
_PER_TEST_RE = re.compile(r"--timeout[= ](\d+)")

#: Floors. Below these a budget stops being a hang-guard and becomes a
#: benchmark on the runner, which is #702's inversion arriving in CI config.
#: Set at roughly 2x the worst duration ever observed for the class, so that
#: tightening one past the point where a slow runner could trip it fails here
#: with the evidence attached rather than in a red leg nobody caused.
#:
#: `lint-new` (#1481) has never run, so its pair is by analogy and says so here
#: rather than pretending to a measurement: checkout + `pip install -e .[dev]`
#: + one ruff invocation over a handful of files is the `notifiers` shape, not
#: the pytest one. Re-derive both from its own durations once there are some.
MIN_BUDGET_MIN = {"pytest": 20, "notifiers": 5, "coverage": 8, "lint-new": 5}

#: Ceilings. A budget large enough to be indistinguishable from GitHub's
#: six-hour default is the defect wearing the fix's clothes: the board still
#: reads `pending` for longer than anyone waits. Roughly 2x the chosen budget,
#: which leaves room to raise one for a real reason and none to disable it.
MAX_BUDGET_MIN = {"pytest": 60, "notifiers": 30, "coverage": 40, "lint-new": 30}


#: `job_blocks` and `job_budget` now live in `tests/_workflow_parse.py`,
#: shared with `test_ci_non_python_coverage_557.py` since #731 — that file
#: needed the same indentation reader one level deeper (steps, not just jobs)
#: and had no business growing a second parser beside this one. The fixture
#: tests below still exercise them, through the import.


# --- the parser, so a discovery bug cannot read as a clean sheet ------------


def test_the_job_discovery_is_not_empty() -> None:
    """The failure this file exists to prevent must not be able to hide in it."""
    assert job_blocks(), (
        "no jobs parsed out of tests.yml — either the workflow moved or the "
        "two-space-indent assumption broke. Either way every assertion below "
        "is now checking nothing and reporting a pass.")


def test_the_job_discovery_finds_the_jobs_that_exist() -> None:
    assert set(job_blocks()) == {"pytest", "notifiers", "coverage", "lint-new"}, (
        "the set of CI jobs changed. Give the new one a timeout-minutes sized "
        "from its own observed duration, then add it to MIN_BUDGET_MIN and "
        "MAX_BUDGET_MIN here — the whole point of #722 is that a job cannot "
        "arrive without a budget.")


def test_the_parser_does_not_read_a_step_budget_as_its_job_budget() -> None:
    """A step ceiling leaves every other step in the job unbounded.

    Counting one as the job's would report a bounded job that is not one.
    """
    fixture = (
        "jobs:\n"
        "  only:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: something\n"
        "        timeout-minutes: 5\n"
    )
    blocks = job_blocks(fixture)
    assert set(blocks) == {"only"}
    assert job_budget(blocks["only"]) is None


def test_the_parser_ignores_two_space_keys_outside_the_jobs_mapping() -> None:
    """`on:` has `push:` at two spaces. It is not a job."""
    fixture = (
        "on:\n"
        "  push:\n"
        "    branches: [master]\n"
        "jobs:\n"
        "  real:\n"
        "    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 7\n"
    )
    blocks = job_blocks(fixture)
    assert set(blocks) == {"real"}
    assert job_budget(blocks["real"]) == 7, (
        "the budget must be found anywhere in its block, not only on the first "
        "line of it. The first version of this regex was missing re.M, and both "
        "fixtures here happened to declare the budget first — so the parser unit "
        "tests were green while the real workflow read as unbudgeted.")


# --- the policy ------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(job_blocks()))
def test_every_job_declares_a_wall_clock_budget(name: str) -> None:
    """The whole of #722, in one assertion.

    Without a `timeout-minutes` a hung leg runs to GitHub's six-hour default
    while every read of the board says `pending`, and the merge gate turns that
    into a six-hour block nobody is told about.
    """
    assert job_budget(job_blocks()[name]) is not None, (
        f"job `{name}` declares no timeout-minutes, so a hang in it runs for "
        "six hours and renders as `pending` the whole time. Size one from the "
        "job's observed duration with headroom — see this file's docstring for "
        "the measurements the existing two were sized from.")


@pytest.mark.parametrize("name", sorted(MIN_BUDGET_MIN))
def test_no_budget_is_tightened_into_a_benchmark(name: str) -> None:
    """A budget that a slow runner can trip is #702, in CI config.

    It converts a genuine green into a red, and the thing everyone learns is to
    press re-run — after which the guard is worse than not having one.
    """
    budget = job_budget(job_blocks()[name])
    assert budget is not None and budget >= MIN_BUDGET_MIN[name], (
        f"job `{name}` is budgeted at {budget} min, under the {MIN_BUDGET_MIN[name]} "
        "min floor. The floor is ~2x the worst duration ever observed for this "
        "job class; below it the budget stops guarding a hang and starts "
        "measuring the runner.")


@pytest.mark.parametrize("name", sorted(MAX_BUDGET_MIN))
def test_no_budget_is_loosened_back_into_the_default(name: str) -> None:
    """360 minutes spelled out is still 360 minutes."""
    budget = job_budget(job_blocks()[name])
    assert budget is not None and budget <= MAX_BUDGET_MIN[name], (
        f"job `{name}` is budgeted at {budget} min, over the {MAX_BUDGET_MIN[name]} "
        "min ceiling. A budget nobody would wait out is the six-hour default "
        "with extra steps.")


def test_the_two_job_classes_do_not_share_one_number() -> None:
    """#702's rule, applied here: sizing is per class, from that class's data.

    The pytest legs run 93-574s and the notifiers legs 24-37s. One number
    generous enough for the first is no guard at all on the second — which is
    the job that actually hung.
    """
    blocks = job_blocks()
    assert job_budget(blocks["pytest"]) != job_budget(blocks["notifiers"]), (
        "both jobs carry the same budget. They differ by more than an order of "
        "magnitude in observed duration; one number cannot be a hang-guard for "
        "both.")


# --- the inner guard on the step that actually hung ------------------------


def _hung_step():
    """The step that hung, found by name among the notifiers job's steps."""
    steps = job_steps(job_blocks()["notifiers"])
    assert steps, (
        "no steps parsed out of the notifiers job — the guards below are now "
        "checking nothing and reporting a pass")
    named = [s for s in steps
             if s.name == "Run the channel integration tests for real"]
    assert named, (
        "the channel integration step was renamed or removed; this guard no "
        f"longer knows which step it is checking. Steps: "
        f"{[s.name or s.uses for s in steps]}")
    return named[0]


def _per_test_budget(step) -> int | None:
    found = _PER_TEST_RE.findall(step.run)
    return int(found[0]) if found else None


def test_the_step_that_hung_arms_a_per_test_timeout() -> None:
    """pytest-timeout was already installed by both jobs and used by neither.

    A job ceiling can only say "this leg did not finish". The per-test budget
    names the test and dumps the stack of the thread stuck in it, which is the
    one artefact #554 needs and the one a wall-clock kill cannot produce.

    #731: this used to search the whole `notifiers` job block, comments and
    all. The comment justifying the flag quotes it — "`--timeout=30` (#722) is
    the inner half of the guard" — twelve lines above the flag itself, so
    deleting the real `--timeout=30` from the run block left this file 14/14
    green. Verified by deleting it. That is #730's shape sitting in the file
    that was held up as the structural alternative to #730, which is the best
    argument there is that the class was worth sweeping for on purpose.
    """
    step = _hung_step()
    assert _per_test_budget(step) is not None, (
        "the channel integration step no longer arms pytest-timeout, so a "
        "hung socket wait is bounded only by the job ceiling — which reports "
        "'the leg did not finish' and names no test. Its run block is:\\n"
        f"{step.run}")


def test_the_job_ceiling_sits_above_the_inner_budget_it_guards() -> None:
    """The #716 rule, read out of the file rather than tabulated beside it.

    `tests/_adapter_budget.py`: an outer budget is a hang-guard on the inner
    one, so it must exceed it. A job ceiling below the per-test budget could
    never let the per-test guard fire — the leg would be killed first, and the
    traceback that names the hung test would never be written.

    The inner number now comes from the step's `run:`. Read off the job text it
    came from the comment, which held the same figure by coincidence — so a
    step raised to `--timeout=3000` against a 10-minute ceiling would have been
    compared against the comment's stale 30 and passed.
    """
    inner = _per_test_budget(_hung_step())
    outer = job_budget(job_blocks()["notifiers"])
    assert inner is not None, "no per-test budget to compare the ceiling to"
    assert outer is not None and outer * 60 > inner, (
        f"the notifiers job ceiling ({outer} min) is at or under its own "
        f"per-test budget ({inner}s). The inner guard can then never fire, so "
        "a hung test is reported as a dead leg instead of as a named failure.")


def test_the_pytest_job_deliberately_has_no_per_test_budget() -> None:
    """Stated, not left to be inferred from its absence.

    The main suite runs ~4000 tests under `-n auto`, excludes the `slow`
    marker in CI as of #891 (a separate scheduled workflow runs it instead),
    and its windows legs are the ones this repo has repeatedly measured
    blowing hand-written budgets under contention (#702, #658, #650). There is
    no per-test timing data for that leg, and a per-test number guessed against
    a contended windows runner is the exact mistake those three issues are
    about, one layer up. The job ceiling bounds it; the finer guard waits for
    evidence. If that changes, delete this test and say so in the changelog.

    Over the steps' `run:` blocks rather than the job text. This assertion is a
    negative, so the whole-job version failed in the safe direction — a comment
    mentioning `--timeout=N` anywhere in the pytest job would have turned it
    red for no reason. A false red is cheaper than a false green and still not
    free: it is a guard nobody trusts, which is how a guard stops being read.
    """
    steps = job_steps(job_blocks()["pytest"])
    assert steps, "no steps parsed out of the pytest job"
    armed = [s.name for s in steps if _PER_TEST_RE.search(s.run)]
    assert not armed, (
        f"steps {armed} now arm a per-test timeout. That may well be right — "
        "but it needs per-test timings from a windows leg behind it, not a "
        "number chosen from a laptop. Record them and remove this test.")
