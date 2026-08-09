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
  the code; `notifiers/` is mostly TypeScript and listed below instead. One
  sentence covering all of them would have been true of two, which is this
  issue's defect at a smaller scale. `.github/scripts/` used to sit here too,
  on the grounds that flooring the script that computes the floor is a loop —
  see #991 below for why that was wrong and what it cost.
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

Several floors and not one, because a single total hides the imbalance the
issue named: `_supertool.py` improving while `presets/` rots averages to
something that looks fine. Not eleven either (one per preset family), for the
reason the `timeout-minutes` comment in `tests.yml` gives about per-OS budgets —
eleven numbers to keep in step is ten more places to drift, and the families
are not independently owned.

## The gate is not exempt from the gate (#991)

`.github/scripts/` was measured, printed, and floored by nothing, under the
reason that "enforcing a floor on the script that computes the floor is a loop
with no useful fixed point". There is no loop: this file\'s coverage is produced
by the test suite, not by this file, and two of the three scripts in the
directory are not this one. The second half of that reason — "its own failure
mode is a red CI step, not a silent one" — is true of a crash and false of the
thing that actually happens, which is its tests quietly going away.

The measurement settled it. The bucket read 82.65%: `assemble_changelog.py`
93.95%, `junit_summary.py` 94.12%, **this file 46.43%** — the least covered
thing the gate measured was the gate. Uncovered were `report()` in its
entirety, `measure()`, and `main()`\'s reporting path: the function that decides
pass from fail, the branch that turns "nothing was measured" into a failure,
and the stale-floor advisory. `report()` could have been changed to `return 0`
and all twelve pytest legs would have stayed green. #877\'s refusal branches, by
contrast, were the covered part — the issue had that the wrong way round.

`tests/test_coverage_gate_floor_991.py` covers the verdict function, and the
floors are set from the measurement that followed it. Two entries and not one
so this file cannot be subsidised by the other two; the floors are per-bucket,
and a bucket is not always a directory.

## Where the scratch data lives (#877)

Under the repository, at `.coverage-gate/`, mode 0700 — not at a fixed name in
`tempfile.gettempdir()`, which is what it was. Three reasons, in the order they
bite:

* The rcfile written there is handed to `coverage run --rcfile=` *and* exported
  as `COVERAGE_PROCESS_START` to every child. A coverage rcfile may declare
  `plugins =`, which coverage imports. Whoever controls that file executes code
  inside the process that decides whether the release passes.
* On Linux `gettempdir()` is `/tmp`. A fixed name there can be pre-created, or
  symlinked, by any local process before `mkdir(exist_ok=True)` accepts it
  without comment — and `main()` then unlinks `.coverage*` inside whatever that
  turned out to be.
* One fixed name is shared by every checkout on the machine. Two worktrees
  running the gate at once interleave their `parallel = true` data files under
  one path and `--report` reads the mixture, which is a wrong number out of the
  release gate with nothing printed anywhere. That one arrives far more often
  than the attacker does.

`--report` still works, which is why this is not a `TemporaryDirectory`: the
path is stable per checkout, just not guessable by another user.

**And it refuses rather than degrading.** If the directory cannot be created, or
is a symlink, or the config cannot be written, `main()` prints `REFUSED` and
exits 2 — it never reaches `report()`. With no config there is no data, every
bucket totals zero statements, and "nothing was measured" sits one branch away
from printing as a pass. Three states: 0 pass, 1 a floor failed, 2 refused.

Usage:

    python3 .github/scripts/coverage_gate.py            # run suite, report, gate
    python3 .github/scripts/coverage_gate.py --report   # report from existing data
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Scratch directory, repo-relative. Kept in sync with `.gitignore` by
#: `tests/test_coverage_gate_workdir_877.py`.
WORK_DIRNAME = ".coverage-gate"


class GateRefusal(Exception):
    """The gate cannot measure, and says so instead of reporting a number.

    Distinct from a floor failure on purpose. A floor failure means the code was
    measured and came up short; this means nothing was measured at all, and the
    two must not share an exit status or a reader will act on the wrong one.
    """

#: Path prefixes whose coverage reds the build, and the floor each must clear.
#: The most specific match wins, so a file entry overrides the directory that
#: holds it — see `_longest_prefix`.
#:
#: Measured 2026-08-05 on macOS/py3.14 over `-m 'not benchmark'`:
#: _supertool.py 89.90%, presets/ 84.04%.
#:
#: `_supertool.py` is the file that was `supertool.py` until #931 split the
#: entry point off it. The floor moved with the code, deliberately: leaving the
#: key as `supertool.py` would have kept a 89% floor that now bounds a 53-line
#: shim and reports green while measuring none of the 17.4k lines it was
#: written for.
#:
#: The `.github/scripts` pair is #991, measured 2026-08-07 on the same machine:
#: this file 94.20%, and 93.97% over `assemble_changelog.py` (93.95%) and
#: `junit_summary.py` (94.12%) together. Two entries and not one because the
#: bucket read 82.65% at 46.43% for this file against 94% for the other two,
#: and a single floor there writes down an average in which the two tested
#: files pay for the untested one — the same argument that gives `_supertool.py`
#: and `presets/` separate floors, one level down. That they now sit at nearly
#: the same number is today's coincidence, not a reason to merge them.
#:
#: The two pairs overlap in opposite directions and `_longest_prefix` is what
#: keeps them apart: `.github/scripts/coverage_gate.py` must beat the directory
#: that contains it, while `supertool.py` must *not* be reached by the
#: `_supertool.py` entry above or the shim would inherit a floor written for
#: 17.4k lines it no longer holds. Pinned by
#: tests/test_coverage_gate_floor_991.py.
ENFORCED: "dict[str, float]" = {
    "_supertool.py": 89.0,
    "presets/": 83.0,
    ".github/scripts/coverage_gate.py": 92.0,
    ".github/scripts/": 92.0,
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
    "scripts/": (
        "the maintainer ops (`oss_train`, #1216). Their reachable half is "
        "covered — argument parsing, the refusals, the read-only `dry` path "
        "and the BUSY guard — and the rest is `git rebase` / `git push "
        "--force-with-lease` against real branches. A floor here would be a "
        "standing invitation to raise the number by writing a fixture that "
        "force-pushes, so the number is printed and left alone"
    ),
    "notifiers/": (
        "the Python half is two small files; the part of this directory that "
        "matters is TypeScript and is listed under NOT measured below, which "
        "is where a reader should be looking rather than at this number"
    ),
}

#: Not measured, with the reason. Coverage of a test file answers "did the suite
#: run itself", which is what the pass/fail count already says.
NOT_MEASURED_PY: "dict[str, str]" = {
    "supertool.py": (
        "the #931 entry-point shim. `source` resolves `supertool` by module "
        "name, and the shim rebinds that entry in sys.modules to the module it "
        "delegates to, so coverage attributes the import to `_supertool.py` and "
        "there is no separate number here to floor. Both of its branches are "
        "executed as subprocesses instead, by tests/test_entry_point_shim_931.py"
    ),
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


def _longest_prefix(rel: str, prefixes: "object") -> "str | None":
    """The most specific declared prefix matching `rel`, or `None`.

    Longest match, not first match. `.github/scripts/coverage_gate.py` and
    `.github/scripts/` are both declared (#991), and under a first-match loop
    which of them owns the file is dict insertion order — an invisible
    dependency in the one module whose whole job is to have none. Reordering
    the literal for readability would silently move the gate onto the
    directory floor and print a number that looks just as convincing.
    """
    best: "str | None" = None
    for prefix in prefixes:  # type: ignore[union-attr]
        if rel == prefix or rel.startswith(prefix):
            if best is None or len(prefix) > len(best):
                best = prefix
    return best


def classify(rel: str) -> str:
    """Which bucket a repo-relative `.py` path falls in.

    Returns `"enforced"`, `"measured"`, `"unmeasured"`, or `""` when the path
    matches nothing — which is the answer `tests/test_coverage_scope_861.py`
    turns into a red, because an unclassified file is #861 happening again.
    """
    rel = rel.replace("\\", "/")
    if _longest_prefix(rel, ENFORCED):
        return "enforced"
    if _longest_prefix(rel, MEASURED_NOT_ENFORCED):
        return "measured"
    if _longest_prefix(rel, NOT_MEASURED_PY):
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
    out = ["supertool", "_supertool"]
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
    try:
        path.write_text("\n".join(body), encoding="utf-8")
        # 0600: coverage imports any `plugins =` this file declares, in the
        # parent and in every child that reads COVERAGE_PROCESS_START. Nobody
        # else needs to be able to write it.
        os.chmod(path, 0o600)
    except OSError as exc:
        raise GateRefusal(
            f"could not write the coverage config at {path}: {exc}") from None
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
    return (_longest_prefix(rel, ENFORCED)
            or _longest_prefix(rel, MEASURED_NOT_ENFORCED))


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
            print(f"  {prefix:<34} NOT MEASURED  (floor {floor:.0f}%)")
            continue
        mark = "ok " if pct >= floor else "FAIL"
        print(f"  {prefix:<34} {pct:6.2f}%  ({stmts - missing}/{stmts} stmts)"
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
            print(f"  {prefix:<34} no statements measured")
        else:
            print(f"  {prefix:<34} {_pct(stmts, missing):6.2f}%  "
                  f"({stmts - missing}/{stmts} stmts)  no floor")
        print(f"  {'':<34} why: {why}")

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
    """A stable *and private* scratch directory (#877).

    Stable, deliberately not a `TemporaryDirectory`: `--report` exists to
    re-print the disclosure without paying six minutes for the suite again, and
    it cannot do that if the data was deleted on the way out of the run that
    produced it.

    Private, and this is the half that was missing: under the repository at mode
    0700 rather than at a fixed, world-guessable name in the shared temp root.
    The file written here is fed to `coverage run --rcfile=` and exported as
    `COVERAGE_PROCESS_START`, and a coverage rcfile can declare `plugins =`,
    which coverage imports — so control of this directory is code execution
    inside the release gate. Under the repository it is also per-checkout, which
    is what stops two worktrees combining each other's data files into one
    number.

    Raises `GateRefusal` rather than pressing on. `mkdir(exist_ok=True)` accepts
    an existing directory of any ownership and follows a symlink to one without
    a word, and that silence is the whole vulnerability.
    """
    work = REPO / WORK_DIRNAME
    if work.is_symlink():
        raise GateRefusal(
            f"{work} is a symlink. `mkdir(exist_ok=True)` would follow it and "
            f"the gate would go on to execute the rcfile at the far end — "
            f"remove the link rather than repointing it")
    if work.exists() and not work.is_dir():
        raise GateRefusal(
            f"{work} exists and is not a directory, so the gate has nowhere to "
            f"put its coverage config")
    try:
        work.mkdir(parents=True, exist_ok=True)
        os.chmod(work, 0o700)
    except OSError as exc:
        raise GateRefusal(
            f"could not create the scratch directory {work}: {exc}") from None
    return work


def main(argv: "list[str]") -> int:
    try:
        work = _work_dir()
        config = write_config(work / ".coverage")
    except GateRefusal as exc:
        print(f"\ncoverage gate: REFUSED — {exc}")
        print("Nothing was measured, and that is reported as a refusal rather "
              "than as a result: with no config there is no coverage data, "
              "every bucket totals zero statements, and a zero prints the same "
              "as a directory that genuinely holds no code. Exit 2 — distinct "
              "from 1, which means a floor was measured and missed.")
        return 2
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
