#!/usr/bin/env python3
"""The coverage gate: measure the whole Python surface, say what was not measured.

Before #861 the floor was `--cov=supertool --cov-fail-under=86` in `addopts`,
and the sentence it printed was about `supertool.py` and nothing else. Every op
under `presets/` — the entire `gh-*`, `gl-*`, `git-*`, `watch`, `hn-*`, `bsky-*`
surface, ~14k statements, where essentially all the work of the last twelve
issues happened — sat outside it. A preset could ship with zero tests and the
gate would report the same green.

Two further facts found while fixing it, both worse than the one filed:

**The floor was not a CI gate at all.** `.github/workflows/tests.yml` runs the
twelve pytest legs with `--no-cov`, and `.githooks/pre-push` does the same,
explicitly, on the grounds that "CI is the authority on what's mergeable". So
the 86% was enforced only against a bare local `pytest` — and `docs/contributing.md`
told contributors it ran on "all 12 pytest legs". Nobody was wrong on purpose;
the number simply had no owner. This module is that owner, and the `coverage`
job in `tests.yml` is where it runs.

**A single-process measurement understates this repo badly.** 122 test modules
drive a preset by spawning it (`subprocess.run([sys.executable, DIFF, ...])`),
which is the only honest way to test a script whose contract is its argv and its
exit status. Coverage does not follow a child process unless told to, so those
lines read as missing. Measured in-process only, `presets/git/diff.py` reports
**9%**; with child attribution on it reports **83.7%** — the file has a
600-line dedicated test module. `trail.py` moves 12% -> 75.6%, `commit.py`
80% -> 94.2%. A floor set on the unattributed number would have sent somebody to
write tests for code that is already tested, and left the genuinely untested
files (`git/blame.py` at 14%, `git/diverge.py` at 24%, `mcp/daemon.py` at 46%)
looking no different. Hence `parallel = true` plus `COVERAGE_PROCESS_START`
below.

It is not free, though it is cheaper than it first looked: four local runs of
the full suite landed between 3m24s and 6m04s against ~3m36s with no coverage
at all, and the spread is contention on the measuring machine rather than
variance in the work. What the top of that range buys is a 16.8k-line
`supertool.py` traced inside each of ~900 spawned children rather than only in
the parent; attributing `presets/` alone costs ~3% (210s -> 217s). Dropping
`supertool.py` from the child config would take that back at the price of
measuring the parent only, which is the guess this whole file exists to stop
making. One job pays it, once, off the critical path of the twelve legs.

## The three states, and why they are printed every run

`docs/validators.md` §"Declining instead of guessing" is the rule: a checker
that cannot answer must say so, because a silent omission is indistinguishable
from a pass. That is precisely the defect #861 filed — a report that omitted
`presets/` without ever saying it had. So the report below has three sections
and always prints all three:

* **measured, enforced** — a floor that reds the build.
* **measured, not enforced** — the number is real and printed, no floor, and
  the *reason* there is no floor is printed beside it. It differs per
  directory: the validator and formatter adapters wrap external binaries that
  are absent on the runner, so their figure reports the toolchain rather than
  the code; `.github/scripts/` contains this gate itself. One sentence covering
  all four would have been true of two of them, which is this issue's defect at
  a smaller scale.
* **not measured** — named, with the reason, and cross-referenced to whatever
  does check them. Never left to be inferred from a green tick.

`tests/test_coverage_scope_861.py` asserts every tracked `.py` file lands in
exactly one of those buckets, discovered from `git ls-files` rather than listed,
so a new preset directory reds the suite until somebody classifies it. That is
the durable half — the same shape `tests/test_ci_non_python_coverage_557.py`
gave the `.ts` inventory, for the same reason.

## The floors

Set at the honest measured value with ~1 point of stated slack, not at an
aspiration. A gate nobody can pass is deleted or bypassed inside a week, and a
bypassed gate is worse than an absent one — it still prints green. Slack,
because the numbers below were measured on macOS/py3.14 and the job runs on
ubuntu/py3.12, and the delta across platform-conditional branches is real and
not something this machine can measure. Tighten them once one real run of the
`coverage` job has reported the ubuntu figure; the gate prints a `raise the
floor` advisory whenever it is more than `_SLACK` under the measurement, so the
staleness is visible rather than remembered.

Two floors and not one, because a single total hides the imbalance the issue
named: `supertool.py` improving while `presets/` rots averages to something
that looks fine. Two floors and not eleven (one per preset family) for the
reason the `timeout-minutes` comment in `tests.yml` gives about per-OS budgets —
eleven numbers to keep in step is ten more places to drift, and the families
are not independently owned.

Usage:

    python3 .github/scripts/coverage_gate.py            # run suite, report, gate
    python3 .github/scripts/coverage_gate.py --report   # report from existing data
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Path prefixes whose coverage reds the build, and the floor each must clear.
#: Measured 2026-08-05 on macOS/py3.14 over `-m 'not benchmark'`:
#: supertool.py 89.90%, presets/ 84.04%.
ENFORCED: "dict[str, float]" = {
    "supertool.py": 89.0,
    "presets/": 83.0,
}

#: Measured and printed, never a floor — each with the reason it has none, and
#: the reasons are not the same one. Lumping four directories under a single
#: sentence that is true of two of them is a smaller version of the defect this
#: file fixes: a disclosure that reads as complete while being partly wrong.
MEASURED_NOT_ENFORCED: "dict[str, str]" = {
    "validators/": (
        "adapters whose body is `subprocess.run([<external binary>, ...])` — "
        "phpstan, hadolint, cargo, tsc. The binary is absent on the runner, so "
        "the number reports the toolchain rather than the code, and a floor "
        "could only fire for a reason the person who tripped it cannot act on"
    ),
    "formatters/": (
        "same shape as validators/ — prettier, phpcbf, php-cs-fixer are not "
        "installed here, and their absence is not a test failure"
    ),
    "notifiers/": (
        "the Python half is two small files; the part of this directory that "
        "matters is TypeScript and is listed under NOT measured below, which "
        "is where a reader should be looking rather than at this number"
    ),
    ".github/scripts/": (
        "CI plumbing, including this gate itself. Enforcing a floor on the "
        "script that computes the floor is a loop with no useful fixed point — "
        "and its own failure mode is a red CI step, not a silent one"
    ),
}

#: Not measured, with the reason. Coverage of a test file answers "did the suite
#: run itself", which is what the pass/fail count already says.
NOT_MEASURED_PY: "dict[str, str]" = {
    "tests/": (
        "the suite measuring itself — the pass/fail count is the same fact, "
        "and a floor here rewards writing tests for tests"
    ),
}

#: Not Python at all. Listed so the gap is recorded rather than merely
#: unmentioned, each pointing at whatever does check it.
NOT_MEASURED_OTHER: "tuple[tuple[str, str], ...]" = (
    ("notifiers/claude-channel/*.ts",
     "TypeScript. The `notifiers` job type-checks it under the channel's own "
     "strict tsconfig and runs its socket-level integration tests for real "
     "(#557) — but there is no line-coverage number for it, here or there."),
    ("notifiers/cursor-witness/extension/src/*.ts",
     "TypeScript, and uncovered knowingly: a VS Code extension needs the "
     "editor's type packages to compile and an editor host to exercise (#557)."),
    ("*.sh, .githooks/*",
     "Shell. `bash -n` parses every one of them on all twelve pytest legs via "
     "tests/test_ci_non_python_coverage_557.py — syntax only, nothing is "
     "executed."),
)

#: How far under the measurement a floor may sit before the gate says so. Wide
#: enough to absorb the platform delta the floors were set with, narrow enough
#: that a floor left stale for a release is visible.
_SLACK = 3.0


def classify(rel: str) -> str:
    """Which bucket a repo-relative `.py` path falls in.

    Returns `"enforced"`, `"measured"`, `"unmeasured"`, or `""` when the path
    matches nothing — which is the answer `tests/test_coverage_scope_861.py`
    turns into a red, because an unclassified file is #861 happening again.
    """
    rel = rel.replace("\\", "/")
    for prefix in ENFORCED:
        if rel == prefix or rel.startswith(prefix):
            return "enforced"
    for prefix in MEASURED_NOT_ENFORCED:
        if rel.startswith(prefix):
            return "measured"
    for prefix in NOT_MEASURED_PY:
        if rel.startswith(prefix):
            return "unmeasured"
    return ""


def _source_lines() -> "list[str]":
    """`source =` entries for the coverage config, absolute except the module.

    Absolute deliberately: a child process is spawned with `cwd` set to a
    `tmp_path`, so a relative `presets` resolves to nothing there and coverage
    silently measures none of it — which is the failure this whole file exists
    to stop happening quietly. `supertool` goes in as a *module name* rather
    than a path because `source` resolves a bare name through `sys.path` (the
    editable install), and a file path there is rejected with
    `module-not-imported`.
    """
    out = ["supertool"]
    for prefix in list(ENFORCED) + list(MEASURED_NOT_ENFORCED):
        if prefix.endswith("/"):
            out.append(str(REPO / prefix.rstrip("/")))
    return out


def write_config(data_file: Path) -> Path:
    """Write the run config the parent *and* every child process reads."""
    path = data_file.parent / "coverage_gate.ini"
    body = ["[run]", "parallel = true", f"data_file = {data_file}", "source ="]
    body += [f"    {line}" for line in _source_lines()]
    body += ["omit =", "    */tests/*", "    */node_modules/*",
             "    */__pycache__/*", "", "[report]", "skip_empty = true", ""]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def run_suite(config: Path) -> int:
    """Run the suite under coverage, with child processes attributed.

    `COVERAGE_PROCESS_START` is what makes the 122 subprocess-driving test
    modules count. It is read by the `.pth` file coverage installs, in every
    Python child that inherits this environment — including the xdist workers,
    which are themselves children. `--no-cov` turns pytest-cov off so there is
    exactly one collector and one set of data files.
    """
    env = dict(os.environ, COVERAGE_PROCESS_START=str(config))
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "coverage", "run",
         f"--rcfile={config}", "-m", "pytest", "-m", "not benchmark",
         "--no-cov", "--tb=short", "-q"],
        cwd=str(REPO), env=env,
    ).returncode


def measure(config: Path) -> "dict[str, tuple[int, int]]":
    """Combine the per-process data files and total each bucket.

    Returns bucket -> (statements, missing). A bucket with no statements is a
    real answer and is reported as such rather than skipped — "nothing was
    measured" is the sentence #861 is about.
    """
    subprocess.run([sys.executable, "-m", "coverage", "combine",
                    f"--rcfile={config}"], cwd=str(REPO),
                   capture_output=True)
    out = config.parent / "coverage.json"
    # `--include` scoped to the repo, and it is not belt-and-braces. Several
    # tests copy `supertool.py` into a `tmp_path` and run it there (the foreign
    # -install guards), and `source = supertool` resolves by module name, so the
    # child measures *that* copy under its throwaway path. By report time the
    # directory is gone and `coverage json` exits 1 with "No source for code".
    # `ignore_errors = true` would also silence it, and would silence a real
    # missing source file with it; this excludes exactly the out-of-tree paths
    # and leaves every in-repo error still fatal.
    subprocess.run([sys.executable, "-m", "coverage", "json",
                    f"--rcfile={config}", f"--include={REPO}/**",
                    "-o", str(out), "-q"],
                   cwd=str(REPO), check=True)
    files = json.loads(out.read_text(encoding="utf-8"))["files"]
    totals: "dict[str, list[int]]" = {}
    for name, data in files.items():
        rel = _relative(name)
        key = _bucket_key(rel)
        if key is None:
            continue
        acc = totals.setdefault(key, [0, 0])
        acc[0] += data["summary"]["num_statements"]
        acc[1] += data["summary"]["missing_lines"]
    return {k: (v[0], v[1]) for k, v in totals.items()}


def _relative(name: str) -> str:
    name = name.replace("\\", "/")
    root = str(REPO).replace("\\", "/") + "/"
    return name[len(root):] if name.startswith(root) else name


def _bucket_key(rel: str) -> "str | None":
    for prefix in ENFORCED:
        if rel == prefix or rel.startswith(prefix):
            return prefix
    for prefix in MEASURED_NOT_ENFORCED:
        if rel.startswith(prefix):
            return prefix
    return None


def _pct(stmts: int, missing: int) -> float:
    return 100.0 * (stmts - missing) / stmts if stmts else 0.0


def report(totals: "dict[str, tuple[int, int]]") -> int:
    """Print all three states and return the exit status.

    Every section prints on every run, including the ones with no floor. A
    reader must be able to see what this number does *not* cover without
    knowing to ask — that asymmetry is what #861 was.
    """
    failures: "list[str]" = []
    print("\n=== coverage: measured and enforced ===")
    for prefix, floor in ENFORCED.items():
        stmts, missing = totals.get(prefix, (0, 0))
        pct = _pct(stmts, missing)
        if stmts == 0:
            failures.append(
                f"{prefix}: nothing was measured. A scope that matches no file "
                f"reports the same 0 as code with no tests, and #861 is what "
                f"that costs — check `source =` in the generated config.")
            print(f"  {prefix:<22} NOT MEASURED  (floor {floor:.0f}%)")
            continue
        mark = "ok " if pct >= floor else "FAIL"
        print(f"  {prefix:<22} {pct:6.2f}%  ({stmts - missing}/{stmts} stmts)"
              f"  floor {floor:.0f}%  {mark}")
        if pct < floor:
            failures.append(
                f"{prefix}: {pct:.2f}% is below the {floor:.0f}% floor.")
        elif pct - floor > _SLACK:
            print(f"       ^ floor is {pct - floor:.1f} points stale — raise it "
                  f"in .github/scripts/coverage_gate.py so the ratchet holds")

    print("\n=== coverage: measured, not enforced ===")
    for prefix, why in MEASURED_NOT_ENFORCED.items():
        stmts, missing = totals.get(prefix, (0, 0))
        if stmts == 0:
            print(f"  {prefix:<22} no statements measured")
        else:
            print(f"  {prefix:<22} {_pct(stmts, missing):6.2f}%  "
                  f"({stmts - missing}/{stmts} stmts)  no floor")
        print(f"  {'':<22} why: {why}")

    print("\n=== coverage: NOT measured ===")
    for prefix, why in NOT_MEASURED_PY.items():
        print(f"  {prefix:<34} {why}")
    for what, why in NOT_MEASURED_OTHER:
        print(f"  {what:<34} {why}")

    print()
    if failures:
        for line in failures:
            print(f"FAIL {line}")
        return 1
    print("coverage gate: pass")
    return 0


def _work_dir() -> Path:
    """A stable scratch directory, deliberately not a `TemporaryDirectory`.

    `--report` exists to re-print the disclosure without paying six minutes for
    the suite again, and it cannot do that if the data was deleted on the way
    out of the run that produced it. Outside the repo, so nothing here needs a
    `.gitignore` entry; emptied by the next real run rather than accumulated.
    """
    work = Path(tempfile.gettempdir()) / "supertool-coverage-gate"
    work.mkdir(parents=True, exist_ok=True)
    return work


def main(argv: "list[str]") -> int:
    work = _work_dir()
    config = write_config(work / ".coverage")
    if "--report" not in argv:
        for stale in work.glob(".coverage*"):
            stale.unlink()
        suite = run_suite(config)
        if suite != 0:
            print("\ncoverage gate: the suite failed — coverage of a red "
                  "suite is not a number anyone should act on.")
            return suite
    return report(measure(config))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
