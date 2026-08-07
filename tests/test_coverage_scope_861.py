"""Every source `.py` file must be classified by the coverage gate (#861).

The floor was `--cov=supertool --cov-fail-under=86`, and it measured one file.
`presets/` — ~14k statements, the entire op surface, where essentially all the
work of the last twelve issues landed — was outside it, so a preset could ship
with zero tests against a green gate. The near-miss worth naming is
`presets/_untrusted.py`: a security boundary sixteen files import, whose tests
are good because their authors chose to write them and for no other reason.

The regression this file exists to catch is not "the number went down" —
`coverage_gate.py`'s floors do that. It is **the scope silently narrowing
again**, which is a different failure and the one that produced the issue: a
gate that stops looking at a directory reports the same green as one that looks
and finds nothing wrong. So the assertions below are about *what is measured*,
never about how much of it is covered.

Discovered from `git ls-files`, never listed. A list here would need the same
edit the config did, at the same moment nobody made it — #730's lesson, and the
shape `tests/test_ci_non_python_coverage_557.py` already uses for the `.ts`
inventory. A new `presets/foo/bar.py` reds this suite until somebody puts it in
a bucket.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from _workflow_parse import job_blocks, job_steps, run_blocks

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / ".github" / "scripts" / "coverage_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("coverage_gate", GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def tracked_python() -> "list[str]":
    """Every tracked `.py` path, repo-relative, forward slashes.

    Tracked rather than walked, because the question is "what does this
    repository ship" — the same question `shell_files()` in
    `test_ci_non_python_coverage_557.py` asks, and a different one from
    `_repo_walk`'s deliberately wider "is this file source", which has to see a
    file being written right now.
    """
    proc = subprocess.run(["git", "ls-files", "-z", "--", "*.py"],
                          cwd=str(REPO), capture_output=True, timeout=60)
    if proc.returncode != 0:
        return []
    return [p.replace("\\", "/")
            for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0")
            if p]


def test_the_population_is_not_empty() -> None:
    """A discovery bug must not read as a clean sheet.

    This is the failure mode that let #861 live for twelve issues: a check
    running over nothing renders exactly the same green as one running over
    everything. Asserted first so a broken `git ls-files` names itself instead
    of quietly passing every assertion below on an empty list.
    """
    found = tracked_python()
    assert len(found) > 100, (
        f"only {len(found)} tracked .py files discovered — either git could not "
        f"answer `ls-files` or the glob broke. Either way this module is now "
        f"checking nothing and reporting a pass, which is the defect it exists "
        f"to remove.")


def test_every_source_file_is_classified() -> None:
    """No `.py` in this repository may be neither measured nor declared unmeasured.

    Three states, per `docs/validators.md` §"Declining instead of guessing":
    measured-and-enforced, measured-without-a-floor, or not-measured-with-a-
    stated-reason. What there is no bucket for is the fourth state #861 was —
    absent from the report and absent from the list of things the report does
    not cover, which is indistinguishable from covered.

    One test over the whole population rather than 519 parametrised ones. The
    parametrised shape is right where the ids are few and each names a file
    worth naming (`test_the_known_shell_files_are_all_discovered` has six); at
    this size it triples the suite's test count to say one thing, and a reader
    of a red run wants the whole list of offenders in one message anyway.
    """
    unclassified = [rel for rel in tracked_python() if not gate.classify(rel)]
    assert not unclassified, (
        f"{len(unclassified)} tracked .py files are in no coverage bucket:\n  "
        + "\n  ".join(unclassified[:40])
        + "\nAdd each to ENFORCED, MEASURED_NOT_ENFORCED or NOT_MEASURED_PY in "
        ".github/scripts/coverage_gate.py — with the reason, if it is the last "
        "one. An unclassified file is #861 happening again.")


def test_the_preset_surface_is_enforced() -> None:
    """The specific narrowing that produced the issue, pinned by name.

    `classify()` returning *something* for a preset is not enough — before
    #861 an equivalent classifier would have called `presets/` unmeasured and
    been internally consistent while measuring nothing. The bucket has to be
    the one with a floor in it.
    """
    presets = [p for p in tracked_python() if p.startswith("presets/")]
    assert len(presets) > 50, "preset discovery broke; see the population guard"
    unenforced = [p for p in presets if gate.classify(p) != "enforced"]
    assert not unenforced, (
        f"{len(unenforced)} preset files carry no coverage floor, e.g. "
        f"{unenforced[:5]}. This is #861: the op surface shipping unmeasured "
        f"behind a gate that reports on one other file.")


def test_the_security_boundary_is_enforced() -> None:
    """`presets/_untrusted.py` by name, because it is the one that hurts.

    Sixteen callers depend on it to fence untrusted remote text. Its tests are
    good today; nothing but this line requires them to stay good tomorrow.
    """
    assert gate.classify("presets/_untrusted.py") == "enforced"


def test_supertool_itself_is_still_enforced() -> None:
    """Widening the scope must not have dropped what the old gate did cover.

    The floor follows the code, not the filename: since #931 `supertool.py` is a
    ~50-line entry-point shim and the 17k lines live in `_supertool.py`. A floor
    left on the shim would have kept printing 89%-and-green over nothing.
    """
    assert gate.classify("_supertool.py") == "enforced"
    assert gate.classify("supertool.py") == "unmeasured"


def test_every_bucket_without_a_floor_states_why() -> None:
    """A number with no floor, or no number at all, has to say why.

    Both of the floorless states, not just the not-measured one. "Measured,
    no floor" is the easier place to park something quietly — it still prints
    a percentage, so it looks accounted for — and the reason is the only thing
    distinguishing a considered exemption from an oversight.
    """
    buckets = (list(gate.MEASURED_NOT_ENFORCED.items())
               + list(gate.NOT_MEASURED_PY.items())
               + list(gate.NOT_MEASURED_OTHER))
    assert len(buckets) >= 6, "the disclosure lost entries"
    for what, why in buckets:
        assert len(why) > 30, (
            f"{what} carries no floor and no real reason — an exemption "
            f"nobody wrote down is indistinguishable from an oversight, which "
            f"is #861 in miniature")


def test_the_gate_measures_children_not_just_the_parent(tmp_path: Path) -> None:
    """`parallel = true` in the generated config, or the number is a fiction.

    122 test modules drive a preset by spawning it, because a script whose
    contract is its argv and its exit status cannot honestly be tested any
    other way. Coverage does not follow a child unless told to: measured
    in-process only, `presets/git/diff.py` reports 9% while its 600-line
    dedicated test module sits right there, and the true figure is 83.7%. A
    floor set on the 9% sends somebody to write tests that already exist.
    """
    config = gate.write_config(tmp_path / ".coverage")
    body = config.read_text(encoding="utf-8")
    assert "parallel = true" in body, (
        "without parallel mode the child processes overwrite one data file and "
        "the subprocess-driven presets read as untested")
    assert str(tmp_path) in body, (
        "data_file must be absolute — children run with cwd set to a tmp_path, "
        "so a relative path scatters the data into throwaway directories")


def test_the_measured_paths_are_absolute() -> None:
    """The child-process trap, pinned.

    A relative `presets` in `source =` resolves against the child's cwd, which
    is a `tmp_path`, where it matches nothing — and coverage reports that as
    zero measured statements rather than as an error. Same class of silence as
    the issue itself.
    """
    lines = gate._source_lines()
    assert "supertool" in lines and "_supertool" in lines, (
        "both top-level modules go in by module name; `source` rejects a file "
        "path there. `_supertool` is where the code lives since #931")
    paths = [line for line in lines if line not in ("supertool", "_supertool")]
    assert paths, "no directory sources at all — the scope collapsed"
    for line in paths:
        assert Path(line).is_absolute(), (
            f"{line} is relative; in a child process spawned with cwd=tmp_path "
            f"it resolves to nothing and is silently measured as empty")
        assert Path(line).exists(), f"{line} does not exist"


def test_ci_runs_the_gate() -> None:
    """A floor enforced nowhere is the state this issue found, not the fix.

    `tests.yml`'s twelve pytest legs pass `--no-cov` and `.githooks/pre-push`
    does too, deliberately — so before #861 the 86% ran against a bare local
    `pytest` and nothing else, while `docs/contributing.md` told contributors
    it ran on all twelve legs. This asserts the job that actually owns the
    number exists and invokes this script.
    """
    workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8")
    jobs = job_blocks(workflow)
    assert "coverage" in jobs, (
        "no `coverage` job in tests.yml — the floor is back to being enforced "
        "only on whoever happens to run a bare `pytest`")
    runs = "\n".join(run_blocks(job_steps(jobs["coverage"])))
    assert "coverage_gate.py" in runs, (
        "the coverage job does not invoke .github/scripts/coverage_gate.py")


def test_the_floors_are_numbers_a_run_produced() -> None:
    """Floors are a measurement, not an aspiration.

    A gate nobody can pass gets deleted or bypassed inside a week, and a
    bypassed gate still prints green — strictly worse than no gate. The bound
    here is loose on purpose: it is not asserting a coverage level, it is
    asserting that nobody set the floor to 0 to make a red go away, or to 100
    to make a point.
    """
    assert gate.ENFORCED, "no enforced floors at all"
    for prefix, floor in gate.ENFORCED.items():
        assert 50.0 <= floor <= 99.0, (
            f"{prefix}'s floor of {floor} is not a number any run produced")
