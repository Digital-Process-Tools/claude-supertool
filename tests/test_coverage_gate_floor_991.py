"""The gate is not exempt from the gate (#991).

`.github/scripts/` was measured, printed, and floored by nothing. The stated
reason — "enforcing a floor on the script that computes the floor is a loop with
no useful fixed point" — is not true of a loop and is not true of the directory:
two of the three files in it are not the gate at all, and the gate's own
coverage is computed by the *test suite*, not by itself, so there is no fixed
point to find. What there was instead was the exemption this repository files
most often: the thing that reports on correctness, exempted from being correct.

The measurement that decided the shape (2026-08-07, macOS/py3.14, over the
eleven test modules that touch these three files):

| file                    | stmts | covered |
| ----------------------- | ----- | ------- |
| `assemble_changelog.py` |   397 |  93.95% |
| `junit_summary.py`      |    51 |  94.12% |
| `coverage_gate.py`      |   140 |  46.43% |

82.65% for the bucket — and the issue's proposal to floor the bucket at that
number would have written down an average in which a 94% file pays for a 46%
one. That is the imbalance `coverage_gate.py`'s own docstring gives as the
reason there are two floors and not one; the same argument applies one level
down. Hence a floor on the gate *by name* and a second on the rest.

The 46% was the other surprise, and it inverts the issue's reading: the
refusal branches #877 added are the covered part. What had no test at all was
`report()` — the function that decides pass from fail, the `nothing was
measured` branch, and the stale-floor advisory. A gate whose verdict function
is untested can start returning 0 for everything and no leg goes red. Those
tests are below, and they are why the gate's floor can be a number worth
setting rather than an apology.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / ".github" / "scripts" / "coverage_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("coverage_gate_991", GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def ci_scripts() -> "list[str]":
    """Tracked `.py` under `.github/scripts/`, discovered rather than listed."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", ".github/scripts/*.py"],
        cwd=str(REPO), capture_output=True, timeout=60)
    assert proc.returncode == 0, "git ls-files failed; the population is unknown"
    return [p.replace("\\", "/")
            for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0")
            if p]


def test_the_population_is_not_empty() -> None:
    """A discovery bug must not read as a clean sheet.

    Same guard as `test_coverage_scope_861.py` opens with, for the same reason:
    every assertion below is a list comprehension over this, and over an empty
    list they all pass while checking nothing.
    """
    found = ci_scripts()
    assert len(found) >= 3, (
        f"only {found} discovered under .github/scripts/ — either git could "
        f"not answer or the pathspec broke, and this module is now asserting "
        f"nothing at all")


def test_every_ci_script_carries_a_floor() -> None:
    """The bucket that was measured and watched by nothing."""
    unenforced = [p for p in ci_scripts() if gate.classify(p) != "enforced"]
    assert not unenforced, (
        f"{unenforced} are measured, printed, and floored by nothing — they "
        f"can go to zero without reddening a build, which is #991")


def test_the_gate_is_not_exempt_from_the_gate() -> None:
    """`coverage_gate.py` by name, because it is the one that hurts.

    Every release decision about coverage is this file's output. It was the
    least covered thing it measured.
    """
    assert gate.classify(".github/scripts/coverage_gate.py") == "enforced"


def test_the_gate_has_its_own_floor_and_not_the_bucket_average() -> None:
    """A 46% file must not be able to hide behind two 94% ones.

    Floors on averages are how a directory rots one file at a time while the
    total stays put. The gate's docstring makes this argument for splitting
    `supertool.py` from `presets/`; it did not apply it to its own bucket.
    """
    own = gate._bucket_key(".github/scripts/coverage_gate.py")
    other = gate._bucket_key(".github/scripts/junit_summary.py")
    assert own is not None and other is not None
    assert own != other, (
        "the gate shares a bucket with the rest of .github/scripts/, so its "
        "own number is averaged away")
    assert gate.ENFORCED[own] > gate.ENFORCED[other] - 100, "sanity"


def test_the_entry_point_shim_does_not_inherit_the_bodys_floor() -> None:
    """`supertool.py` and `_supertool.py` are the other overlapping-ish pair.

    #931/#942 moved the 17.4k-line body into `_supertool.py` and left
    `supertool.py` an 84-line shim, so the floor moved with the code and the
    shim went into NOT_MEASURED_PY with its reason. The two names differ by a
    leading underscore, and prefix matching is what keeps them apart in both
    directions: the shim must not be reached by the entry written for the body
    — an 89% floor bounding 84 lines reports green while measuring none of
    what it was written for — and the body must not fall through to the shim's
    "not measured" reason, which would drop the largest enforced bucket in the
    repository out of the gate while every section still printed.
    """
    assert gate.classify("_supertool.py") == "enforced"
    assert gate._bucket_key("_supertool.py") == "_supertool.py"
    assert gate.classify("supertool.py") == "unmeasured"
    assert gate._bucket_key("supertool.py") is None, (
        "the shim was totalled into a bucket; its statements are attributed "
        "to _supertool.py by the sys.modules rebind, so counting it here "
        "would count them twice")


def test_the_most_specific_prefix_wins_regardless_of_declaration_order() -> None:
    """The file floor beats the directory floor because it is longer, not first.

    Both `_bucket_key` and `classify` walk `ENFORCED`. With a file entry and
    the directory that contains it both present, first-match makes the verdict
    depend on dict insertion order — an invisible dependency in the one file
    whose job is to have none. Asserted against a reversed copy.
    """
    reversed_floors = dict(reversed(list(gate.ENFORCED.items())))
    original = gate.ENFORCED
    try:
        gate.ENFORCED = reversed_floors
        assert (gate._bucket_key(".github/scripts/coverage_gate.py")
                == ".github/scripts/coverage_gate.py")
        assert gate.classify(".github/scripts/coverage_gate.py") == "enforced"
    finally:
        gate.ENFORCED = original


def test_no_floorless_bucket_claims_to_be_ci_plumbing_any_more() -> None:
    """The old exemption text must go with the exemption.

    A reason left behind after the thing it excused is removed is worse than
    no reason: it reads as a live decision.
    """
    assert ".github/scripts/" not in gate.MEASURED_NOT_ENFORCED, (
        "the directory is enforced now; a leftover 'no floor because' entry "
        "for it would print a sentence that is no longer true")


# --- report(): the verdict function, which had no test at all ----------------

def _totals(pct: float) -> "dict[str, tuple[int, int]]":
    """Synthetic per-bucket totals at a given percentage, for every bucket.

    Built from the live `ENFORCED` keys rather than a literal, so adding a
    floor does not quietly drop it out of these assertions.
    """
    missing = int(round(1000 * (100.0 - pct) / 100.0))
    return {prefix: (1000, missing) for prefix in gate.ENFORCED}


def test_a_below_floor_bucket_fails_the_build(capsys) -> None:
    """The whole point of a floor, and nothing asserted it.

    `report()` returning 1 here is the only thing that turns a coverage drop
    into a red leg. Without this test the function could `return 0`
    unconditionally and all twelve pytest legs would still be green.
    """
    rc = gate.report(_totals(10.0))
    out = capsys.readouterr().out
    assert rc == 1, "10% cleared every floor — the gate cannot fail any more"
    for prefix, floor in gate.ENFORCED.items():
        assert f"{prefix}: 10.00% is below the {floor:.0f}% floor." in out


def test_a_bucket_above_its_floor_passes(capsys) -> None:
    """The other half — a gate that only ever fails is deleted in a week."""
    rc = gate.report(_totals(100.0))
    assert rc == 0
    assert "coverage gate: pass" in capsys.readouterr().out


def test_each_floor_is_enforced_on_its_own_bucket(capsys) -> None:
    """One bucket below its floor reds the run even with the others at 100%.

    The failure a single averaged floor cannot catch, asserted per bucket.
    """
    for target in gate.ENFORCED:
        totals = _totals(100.0)
        totals[target] = (1000, 1000)
        rc = gate.report(totals)
        out = capsys.readouterr().out
        assert rc == 1, f"{target} at 0% did not fail the gate"
        assert f"{target}: 0.00% is below" in out


def test_a_bucket_that_measured_nothing_fails_rather_than_passing(capsys) -> None:
    """Zero statements is a refusal, not a 0% that happens to be a pass.

    An empty `source =` and a directory with no tests produce the same totals.
    The one that must never happen is either of them printing green, and
    `_pct` returns 0.0 for an empty bucket — one `>=` away from clearing a
    floor of 0.
    """
    rc = gate.report({})
    out = capsys.readouterr().out
    assert rc == 1
    for prefix in gate.ENFORCED:
        assert f"{prefix}: nothing was measured." in out
        assert "NOT MEASURED" in out


def test_a_stale_floor_prints_the_advisory(capsys) -> None:
    """The ratchet. A floor more than `_SLACK` under the truth says so.

    This is what stops the floors below from being the last measurement anyone
    ever took, which is the state `supertool.py` and `presets/` are in.
    """
    highest = max(gate.ENFORCED.values())
    rc = gate.report(_totals(min(100.0, highest + gate._SLACK + 2.0)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "points stale — raise it" in out


def test_a_floor_within_slack_stays_quiet(capsys) -> None:
    """No advisory just over the line, or the advisory means nothing."""
    lowest = min(gate.ENFORCED.values())
    gate.report(_totals(lowest + gate._SLACK - 0.5))
    assert "points stale" not in capsys.readouterr().out


def test_the_report_prints_all_three_states_even_when_it_passes(capsys) -> None:
    """The #861 contract: the disclosure is unconditional, not a failure path."""
    gate.report(_totals(100.0))
    out = capsys.readouterr().out
    for heading in ("=== coverage: measured and enforced ===",
                    "=== coverage: measured, not enforced ===",
                    "=== coverage: NOT measured ==="):
        assert heading in out
    for prefix in gate.MEASURED_NOT_ENFORCED:
        assert prefix in out
    for prefix in gate.NOT_MEASURED_PY:
        assert prefix in out


def test_the_enforced_column_is_wide_enough_for_every_bucket_name(capsys) -> None:
    """A file-length key must not shove its own percentage out of alignment.

    Cosmetic until it is not: this report is read in a CI log by somebody
    deciding whether a release ships, and a column that wraps at exactly the
    row that failed is the row they misread.
    """
    gate.report(_totals(100.0))
    rows = [ln for ln in capsys.readouterr().out.splitlines()
            if any(ln.strip().startswith(p) for p in gate.ENFORCED)]
    assert len(rows) == len(gate.ENFORCED)
    starts = {ln.index("%") for ln in rows}
    assert len(starts) == 1, (
        f"the percentage column is not aligned across buckets: {rows}")


# --- measure(): bucket totalling, which also had no test ---------------------

def test_measure_totals_each_bucket_and_ignores_out_of_tree_files(
        tmp_path: Path, monkeypatch) -> None:
    """Per-file JSON folded into per-bucket sums, foreign paths dropped.

    The foreign path is not hypothetical: several tests copy `supertool.py`
    into a `tmp_path` and run it there, and `source = supertool` resolves by
    module name, so the child measures that copy. Counted into `supertool.py`
    it would inflate the very number the gate enforces. `tmp_path` is the
    foreign path here for the same reason it is the real one — outside the
    repository, and native on whichever platform is running.

    Keys are built with `/` rather than written with `/`: `coverage json`
    emits `C:\\...\\supertool.py` on Windows, and a fixture that only ever
    spells them the POSIX way would pass there while testing a shape the
    runner never produces.
    """
    config = tmp_path / "coverage_gate.ini"
    config.write_text("[run]\n", encoding="utf-8")
    (tmp_path / "coverage.json").write_text(json.dumps({"files": {
        str(gate.REPO / "_supertool.py"):
            {"summary": {"num_statements": 100, "missing_lines": 5}},
        str(gate.REPO / "supertool.py"):
            {"summary": {"num_statements": 84, "missing_lines": 84}},
        str(gate.REPO / "presets" / "git" / "diff.py"):
            {"summary": {"num_statements": 200, "missing_lines": 20}},
        str(gate.REPO / "presets" / "git" / "trail.py"):
            {"summary": {"num_statements": 100, "missing_lines": 10}},
        str(gate.REPO / ".github" / "scripts" / "coverage_gate.py"):
            {"summary": {"num_statements": 140, "missing_lines": 7}},
        str(gate.REPO / ".github" / "scripts" / "junit_summary.py"):
            {"summary": {"num_statements": 51, "missing_lines": 3}},
        str(tmp_path / "foreign" / "supertool.py"):
            {"summary": {"num_statements": 999, "missing_lines": 999}},
    }}), encoding="utf-8")
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))

    totals = gate.measure(config)

    assert totals["_supertool.py"] == (100, 5), (
        "the out-of-tree copy leaked into the enforced bucket")
    assert "supertool.py" not in totals, (
        "the #931 shim is declared not-measured; totalling it as a bucket of "
        "its own prints a percentage the report says does not exist")
    assert totals["presets/"] == (300, 30), "the two preset files did not sum"
    assert totals[".github/scripts/coverage_gate.py"] == (140, 7)
    assert totals[".github/scripts/"] == (51, 3), (
        "the gate's own file was counted into the directory bucket as well")


def test_run_suite_exports_the_child_attribution_variable(
        tmp_path: Path, monkeypatch) -> None:
    """`COVERAGE_PROCESS_START`, or the subprocess-driven presets read untested.

    Dropping it does not fail anything: the run still completes, the numbers
    just fall by tens of points in the files with the most tests, and the fix
    somebody reaches for is lowering a floor.

    The expected value is derived from the `Path` handed in rather than
    written out, because `run_suite` passes it through `str()` and that is
    `\\tmp\\x\\...` on Windows and `/tmp/x/...` everywhere else. Both are the
    same file, and the native one is the correct one: coverage's `.pth` opens
    this value as a filename in every child, so a POSIX spelling on Windows
    would be the defect rather than the fix. Asserted as a `Path` equality —
    the question is which file was named, not how it was spelled.
    """
    seen: "dict[str, object]" = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env", {})
        return subprocess.CompletedProcess(cmd, 3)

    config = tmp_path / "coverage_gate.ini"
    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    rc = gate.run_suite(config)

    assert rc == 3, "the suite's exit status is not passed through"
    exported = seen["env"].get("COVERAGE_PROCESS_START")  # type: ignore[union-attr]
    assert exported is not None, (
        "COVERAGE_PROCESS_START was not exported at all — the ~900 spawned "
        "children measure nothing and the report reads as a coverage drop")
    assert Path(exported) == config, (
        f"the children were pointed at {exported}, not at {config}")
    assert "--no-cov" in seen["cmd"], (
        "pytest-cov left on gives a second collector and a mixed number")
    assert f"--rcfile={config}" in seen["cmd"], (
        "the parent process is not reading the same config as its children")


def test_a_red_suite_is_not_reported_as_a_coverage_number(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """`main()` stops at a failing suite instead of reporting on partial data.

    Coverage of a run that died halfway is a smaller number over fewer tests,
    and reporting it invites somebody to act on the drop rather than on the
    failure that caused it.
    """
    monkeypatch.setattr(gate, "REPO", tmp_path)
    monkeypatch.setattr(gate, "run_suite", lambda config: 2)
    monkeypatch.setattr(gate, "measure", lambda config: (_ for _ in ()).throw(
        AssertionError("measure() ran after a red suite")))

    rc = gate.main([])

    assert rc == 2
    assert "the suite failed" in capsys.readouterr().out
