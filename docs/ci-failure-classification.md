# CI failure classification (#521) — what is measured, what is built

## Scope of this document

[#521](https://github.com/Digital-Process-Tools/claude-supertool/issues/521)
asks for a classifier: given a red CI leg, decide autofixable / needs-a-human
/ retry. The issue itself says this needs a corpus before it is built, and
that the table it sketches ("rector/prettier diff hunks", "PHPStan missing
type") is a hypothesis drawn from memory of a different repository's CI
shape — the issue's own comment (2026-09-02) already found that table
"written against a GitLab-plus-PHP CI shape this repository does not have".

This lane did not attempt the full classifier. It did two smaller things,
both honestly scoped to what a corpus of six actually supports:

1. **Measured** what `gh-job:ID:fail` surfaces today, against six real
   failed CI runs pulled live from this repository's own history.
2. **Built** the one row the issue calls a rule rather than a heuristic —
   "unit test failure → MANUAL, always" — as a small, independently tested
   pure function, `classify_unit_test_failure` in `presets/_job_argv.py`.

## The measurement

Pulled live on 2026-09-09 via `gh-run` and `gh-job:ID:fail` against this
repository's actual CI history (`Digital-Process-Tools/claude-supertool`,
workflow `tests`), the six most recent failed runs at that time:

| Run | Branch | Failed legs | What actually broke |
| --- | --- | --- | --- |
| 34276615685 | fix/1712 | 2 (`pytest ubuntu 3.12`, `coverage`) | A stale docs-size assertion (`test_render_size_claims_1877.py`) — `ops:full`'s rendered size drifted past the doc's stated figure. |
| 34251408240 | master | 1 (`pytest windows 3.10`) | A concurrency-serialization test (`test_go_warmup_lock_2331.py`) — "more than one caller was inside fn() at the same real moment", a timing assertion that looks flaky but is exactly the shape #521's rule exists to protect: a bot cannot tell "flaky test" from "real race" from the log text alone. |
| 34212429585 | fix/2419 | 13 (nearly every leg) | A cross-cutting guard, `test_handrolled_path_env_guard_1151.py`, tripped by a **new** file the PR added (`test_worktree_teardown_worktreeconfig_2419.py`) containing an `env=` expression the guard's scanner cannot statically evaluate — this repository's own #432 class: a guard test outside the diff's own files, with no filename relationship to the change that broke it. |
| 34195914535 | fix/2412 | 9 | Same underlying shape as the run above — one assertion failing across every OS/version matrix cell plus the coverage leg, which reports the identical failure a second time because coverage only runs after the suite (`coverage gate: the suite failed`). |
| 34167935433 | fix/1110 | 6 | Same pattern again: one real test failure, replicated across every matrix cell it runs on plus `coverage`. |
| 34167236734 | fix/1165 | 4 | Windows-only: a failure that occurred on every Windows Python version and no other OS, the shape a genuine platform-specific bug produces (not sampled in enough depth here to name the root cause; recorded as a data point for the *distribution*, not a claim about the fix). |

**What this sample shows, plainly:**

- **Every single failure in this sample was a pytest unit-test failure.**
  Zero instances of rector, PHPStan, prettier-diff, or lint-tool failures —
  because this repository's CI has no such legs. The issue's table was
  built from a different project's CI shape (per its own 2026-09-02
  comment); on this repository, that half of the table may not apply at
  all, which is itself worth knowing before code is written against it.
- **`gh-job:ID:fail` already gives a clean, high-fidelity signal for the one
  category observed**: the job log carries `.github/scripts/junit_summary.py`'s
  own summary block, `N failing test(s), full messages from junit.xml:`,
  followed by `FAILED <dotted.node.id>` and the assertion text — this is
  parseable without any additional log-scraping, and it survives `gh-job`'s
  own truncation (the "All error blocks" elision skips only lines that
  matched no configured error pattern; the JUnit summary block always
  matches `FAILED`/`Error:` and is never elided).
- **A `coverage` job's failure, when it follows a red suite, is a DERIVED
  failure of the same root cause, not an independent one.** Three of the six
  runs show the identical test-failure text duplicated in both the `pytest`
  leg and the `coverage` leg. A classifier that treats every red leg as its
  own signal would double-count; the first fix a corpus this size actually
  supports is "collapse `coverage: the suite failed` into whatever verdict
  the pytest leg it depends on already got", not a new row of its own.
- **One real failure regularly turns thirteen matrix legs red at once**
  (34212429585). A per-leg classifier without this collapse would report
  "13 unit test failures" for what is one bug — a distortion worth fixing
  before volume, not after.
- `gl-job:ID:fail` was not exercised by this measurement: this repository
  runs no GitLab CI of its own, so there is no live corpus of GitLab job
  logs to sample here. `gl-job.py`'s log-slicing structure mirrors
  `gh-job.py`'s closely enough (`presets/_job_argv.py` is the module they
  already share) that `classify_unit_test_failure` below works unmodified
  against either forge's `:fail` output, but that claim is REASONED from
  reading the code, not OBSERVED against a real GitLab failure — a future
  lane with an actual GitLab-CI repository in this loop's care is where
  that gets checked for real.

## What was built

`classify_unit_test_failure(text: str) -> str | None` in
`presets/_job_argv.py`, shared by both `gh-job` and `gl-job` (the module
both presets already import for their argv parsing). It looks for a line
matching pytest's own `FAILED <nodeid>` marker — either separator observed
in the sample, `path::test_name` or the dotted `module.test_name` junit
summary re-emits — and returns `"MANUAL"` when found, `None` otherwise.

`None` is deliberately not "not a unit test failure": it is "this one rule
did not fire", following this codebase's own standing rule (`CLAUDE.md`,
"The defect this codebase keeps having") that an absence produced by the
tool must never render like an absence in the world. A future classifier
combining several rules needs to keep that distinction at each rule's own
boundary, not only at the combined verdict.

This is deliberately **not wired into `gh-job`/`gl-job`'s own CLI dispatch**
in this lane. `presets/github/job.py`'s `main()` is a ~1600-line function
coupled to live `gh api`/`gh run` calls and its own mocked-subprocess test
fixtures; wiring a `:classify` mode in requires touching that dispatch and
its test harness, which is a second, separable piece of work from the pure
function this lane could test in isolation with real corpus text and no
network mocking at all. Tests: `tests/test_classify_unit_test_failure_521.py`.

## What remains a project for a future lane

- Wire `classify_unit_test_failure` (or its successor) into an actual
  `gh-job:ID:classify` / `gl-job:ID:classify` mode.
  Wire in the `coverage`-follows-`pytest` collapse this sample already
  shows is needed, so one root cause is not reported as N verdicts.
- The rest of the issue's table (autofix rows, infra-retry row) needs its
  own corpus before it is built — this sample had zero instances of any of
  them, so building code against them now would be exactly the
  "hypothesis drawn from memory" the issue itself warns against.
- If this loop starts managing a GitLab-CI or PHP-CI repository, the
  original table's other rows become checkable against a real corpus for
  the first time; until then they stay a hypothesis, not a rule.

## Hypothesis, stated as one, not as a foregone conclusion

The scoping call this lane made — measure + one rule, not the whole table —
is a judgment call, not a fact. It is offered here for pushback: if there is
already a private cross-repo corpus of PHP/GitLab CI failures somewhere this
loop's maintainer has access to that this lane did not know to look for,
the "no autofix-row instances observed" finding above would not hold, and
the right next step would look different.
