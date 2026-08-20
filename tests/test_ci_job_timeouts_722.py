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

**Sizing, from job observations on master rather than from theory**
(`gh api .../actions/runs/<id>/jobs`). Re-measured 2026-08-20 over the 14 most
recent successful `tests` runs, which is where the machine-readable copy in
`OBSERVED_WORST_SECONDS` comes from; the 2026-08-01 column is kept beside it
because the drift between them is the reason that table exists at all.

| job class | 2026-08-01 | 2026-08-20 | budget | ratio to 2026-08-20 worst |
| --- | --- | --- | --- | --- |
| `pytest`, windows-latest | 483-574s | 387-585s | 30 min | 3.1x |
| `pytest`, macos-latest | 128-197s | 238-389s | 30 min | 4.6x |
| `pytest`, ubuntu-latest | 93-125s | 150-301s | 30 min | 6.0x |
| `coverage` | not measured in CI | 353-528s | 20 min | 2.3x |
| `notifiers` | 24-37s | 47-70s | 10 min | 8.6x |
| `lint-new` | did not exist | 13-33s | 10 min | 18x |

**Every class got slower, and two of them by more than 2x.** `notifiers` went
24-37s to 47-70s and the ubuntu pytest legs 93-125s to 150-301s, over nineteen
days in which nobody was watching for it, because no assertion here compared a
budget to a duration -- only to a hand-written floor. `coverage`'s ratio is the
one that matters: the workflow comment beside its budget still claimed ~3.3x
from four *local* runs, and against CI it is 2.3x. That is #1862's finding.

**Two numbers, not one, and that is the load-bearing decision.** Copying a single
figure across both job classes is what #702 was filed about: a budget that fires
on a *slow* runner instead of a hung one converts a real green into a red and
teaches everyone to re-run, which is worse than the disease. The windows pytest
legs are consistently ~450s and the notifiers legs ~55s (2026-08-20; ~530s and
~30s on 2026-08-01); one number generous enough for the first would be ~20x the
second, i.e. no guard at all on the job that actually hung. `notifiers` has
roughly doubled over those nineteen days, which narrows that gap without coming
close to closing it -- the conclusion is unchanged and the arithmetic is not.

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

#: The worst duration ever observed for each job class, in seconds, with the
#: date and sample size that produced it. `OBSERVED` is what makes the floors
#: below checkable: before #1862 they were prose, and one of them said its job
#: "has never run" for eleven weeks after it started running every push.
#:
#: The 2026-08-20 figures are `gh api .../actions/runs/<id>/jobs` over the 14
#: most recent successful `tests` runs, worst of each class:
#:
#: | class | observed | n | worst |
#: | --- | --- | --- | --- |
#: | `pytest` | 150-585s | 14 legs x 12 | 585s (windows, 3.9) |
#: | `coverage` | 353-528s | 14 | 528s |
#: | `notifiers` | 47-70s | 28 (2 OS) | 70s |
#: | `lint-new` | 13-33s | 14 | 33s |
#:
#: n=14 here where the `lint-new` comment in the workflow says 12: #1849
#: measured that job over 12 runs and #1862 re-measured every class over 14 a
#: day later. Same date, same 13-33s range, different denominators, and two
#: undated-looking sample sizes for one quantity read as a contradiction -- so
#: it is said here rather than left to be noticed.
#:
#: **`pytest` is the tight one, and it is tight on purpose rather than by
#: luck.** Its floor of 20 min is 1200s against 2x585 = 1170s: 30 seconds of
#: slack, where every other class has minutes. That is not a landmine waiting
#: on a slow runner -- these are two static numbers, so nothing here reds
#: because CI had a bad afternoon. It reds when somebody *re-measures* pytest
#: and the worst leg has grown past 600s. When that happens the answer is to
#: raise the pytest budget, not to shave the observation: the assertion exists
#: to make that decision arrive as a red rather than as a slow drift nobody
#: computed. It is flagged here so the next person to re-measure is not
#: surprised into taking the cheap option.
#:
#: **What this cannot check, stated rather than implied.** Nothing here knows
#: whether the date is current -- a table left untouched for a year reads
#: exactly like one taken this morning, and no offline test can tell them
#: apart. What it does buy is that the numbers now exist for every budgeted
#: job, that a *new* job cannot be sized by analogy in silence (see
#: `test_every_budgeted_job_has_a_measurement`), and that a floor which drifts
#: under its own evidence is a red rather than a sentence nobody re-reads.
OBSERVED_WORST_SECONDS = {
    "pytest": 585,
    "coverage": 528,
    "notifiers": 70,
    "lint-new": 33,
}

#: When each row above was taken. Carried beside the numbers rather than only
#: in prose, so that a reader who wants to know whether to trust one does not
#: have to date it by `git blame`.
OBSERVED_ON = "2026-08-20"

#: Floors. Below these a budget stops being a hang-guard and becomes a
#: benchmark on the runner, which is #702's inversion arriving in CI config.
#: Set at roughly 2x the worst duration ever observed for the class, so that
#: tightening one past the point where a slow runner could trip it fails here
#: with the evidence attached rather than in a red leg nobody caused.
#:
#: `coverage` was 8 until #1862, and 8 minutes is 480s against a worst observed
#: CI run of 528s. The floor was **below the duration it exists to protect**:
#: tightening the coverage budget to its own floor would have fired on a green
#: run, which is precisely the #702 inversion this table is written to prevent,
#: sitting inside the guard. It read as safe because it was derived from four
#: *local* runs (3m24s-6m04s, quoted in the workflow beside the budget) and
#: local was faster than CI. Found by doing what #1862 asked -- deriving a
#: budget from real durations and comparing it to the analogy-derived one.
#:
#: `lint-new` (#1481) keeps its 5, and that is now a measurement rather than an
#: analogy: 2x its worst observed run is ~66s, so 5 minutes is ~9x and clears
#: the floor rule comfortably. The number does not change; only its warrant
#: does. It was previously justified as "the `notifiers` shape, not the pytest
#: one" by a comment that also said the job had never run -- a guess that
#: happened to land somewhere defensible, which is not the same as being right.
MIN_BUDGET_MIN = {"pytest": 20, "notifiers": 5, "coverage": 18, "lint-new": 5}

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


# --- the sizing evidence, so "no data yet" cannot survive a job's first run --


def test_every_budgeted_job_has_a_measurement() -> None:
    """#1862: a budget justified *by analogy, because no data exists* must stop
    being justifiable the moment the job runs.

    Nothing offline can watch a job run, so this does not assert that the
    numbers are fresh -- it asserts that numbers exist at all for every job
    that carries a budget. That is the half a test can hold, and it is the half
    that failed: `lint-new` carried "has never run, so its pair is by analogy"
    from #1481 until #1862, through every push in between, because the sentence
    was not load-bearing for any assertion and no run could contradict it.

    The remaining gap is stated in `OBSERVED_WORST_SECONDS`' own comment rather
    than left for a reader to discover: a stale date and a current one are
    indistinguishable here.
    """
    unmeasured = sorted(set(job_blocks()) - set(OBSERVED_WORST_SECONDS))
    assert not unmeasured, (
        f"jobs {unmeasured} carry a timeout-minutes but no observed duration. "
        "Size the budget from that job's own runs and record the worst one in "
        "OBSERVED_WORST_SECONDS with the date -- a budget sized by analogy to "
        "another job class is #702, and a comment saying so is a claim nothing "
        "can contradict.")


def test_no_measurement_is_kept_for_a_job_that_no_longer_exists() -> None:
    """The reverse: evidence outliving the thing it was evidence about."""
    stale = sorted(set(OBSERVED_WORST_SECONDS) - set(job_blocks()))
    assert not stale, (
        f"OBSERVED_WORST_SECONDS holds durations for {stale}, which are not "
        "jobs in tests.yml any more. Drop them; a measurement of a job that "
        "does not run is not evidence about anything.")


@pytest.mark.parametrize("name", sorted(OBSERVED_WORST_SECONDS))
def test_the_floor_clears_the_duration_it_exists_to_protect(name: str) -> None:
    """A floor under its own worst observed run is the defect it guards against.

    The floor's whole job is to make *tightening* a budget fail here rather
    than in a red leg nobody caused. A floor below the worst duration the job
    has actually taken permits exactly the tightening it was written to refuse:
    a budget set to that floor would fire on a run that was going to pass.

    This caught `coverage` at 8 minutes against a 528s worst run (#1862). It
    read as safe for as long as it did because it was sized from local runs,
    which were faster than CI -- the comparison this test performs is the one
    nobody was in a position to perform by eye.
    """
    worst = OBSERVED_WORST_SECONDS[name]
    floor_seconds = MIN_BUDGET_MIN[name] * 60
    assert floor_seconds >= 2 * worst, (
        f"job `{name}`'s floor is {MIN_BUDGET_MIN[name]} min ({floor_seconds}s) "
        f"against a worst observed run of {worst}s (measured {OBSERVED_ON}). "
        "The floor is meant to sit at roughly 2x the worst observation, so "
        "that a budget tightened to it still could not fire on a slow-but-fine "
        f"run. At {floor_seconds}s it permits a budget that would.")


@pytest.mark.parametrize("name", sorted(OBSERVED_WORST_SECONDS))
def test_the_real_budget_still_clears_the_worst_observed_run(name: str) -> None:
    """The floor is a rule about the rule. This is the direct question.

    Asserted separately because the two can disagree: a budget can satisfy its
    floor while sitting close enough to the observed spread that ordinary
    contention reds it. Named here so that arrives as a finding about the
    budget rather than as an intermittent leg.
    """
    budget = job_budget(job_blocks()[name])
    worst = OBSERVED_WORST_SECONDS[name]
    assert budget is not None and budget * 60 >= 2 * worst, (
        f"job `{name}` is budgeted at {budget} min against a worst observed "
        f"run of {worst}s (measured {OBSERVED_ON}) -- under 2x, so a slow "
        "runner can trip it and the red will look like a hang. Raise the "
        "budget, or re-measure if the job genuinely got faster.")


def test_the_two_job_classes_do_not_share_one_number() -> None:
    """#702's rule, applied here: sizing is per class, from that class's data.

    The pytest legs run 150-585s and the notifiers legs 47-70s (2026-08-20; the
    module docstring keeps the earlier column beside it). One number generous
    enough for the first is no guard at all on the second — which is the job
    that actually hung.
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
