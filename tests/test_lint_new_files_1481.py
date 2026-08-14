"""An error that arrives already-dead is invisible to both lint gates (#1481).

PR #1473 split `presets/github/since_tag.py` into `presets/github/_release_gate.py`
and left three imports unreachable in the new file. It merged 22/22 green, and
every supertool edit to that file on the way reported `ruff : ok (no new
errors)`. Neither gate was wrong on its own terms:

* CI runs no `ruff check` step, deliberately, and `.github/workflows/tests.yml`
  states the reason — a tree-wide gate reds an unrelated contributor's PR the
  day the linter ships a new rule.
* the supertool `ruff` validator reports only what an edit newly *introduces*,
  and a file whose whole content arrives at once introduces nothing relative to
  its own baseline.

The gap is where they meet, and #1481 is a mechanism issue rather than a
cleanup: `ruff check presets/` on master is clean, so sizing this off "how much
dead code is there" would rank it near zero and miss the point entirely.

What this gate owns
-------------------

The **diff**, never the tree — the files a PR adds, copies or renames, where
"pre-existing" is not a meaningful category because the path has no history. A
new rule from a ruff release then only reds a PR that is already touching the
file, which is exactly the cost the workflow comment refuses to socialise.

Two facts re-derived from the tree rather than taken from the issue, both of
which change the design:

**The issue's body is wrong that F401 is enforced.** It says "`F401` is selected
repo-wide (`select = ["E9", "F", "B", "PLE"]`) and there is no per-file ignore.
Nothing is misconfigured." There is no per-file ignore, but `pyproject.toml`
carries a *global* `ignore = ["F401", "F841", "F541"]`, added by #797 with its
reason stated: 263 pre-existing occurrences across ~120 files, and the cleanup
is a `--fix` sweep nobody can review. Measured here on 2026-08-13: a plain
`ruff check` of the offending file at `ffe47ed` returns `All checks passed!` —
so a diff-scoped gate running the repo's own configuration would have reported
the same green the issue is complaining about.

That ignore is a statement about the *existing tree*. A path that has no
history has no share of it, so the gate re-enables exactly those three rules
for the files it checks, and the debt stops growing without anybody paying it
down. With them on, `ffe47ed` reports the three imports the issue names.

**The offending file was a rename, not an addition.** `git diff --name-status
-M` calls it `R065 presets/github/since_tag.py -> presets/github/_release_gate.py`.
A gate scoped to `A` alone would have missed the instance it was filed for,
which is why `C` and `R` are in the filter.

The cost of that, measured rather than waved at: 141 of 902 tracked `.py` files
currently carry at least one F401/F841/F541 (293 findings), so a PR that
*renames* one of those reds until the author deletes the dead line. That lands
on whoever is moving the file rather than on the next person to push, which is
the test the workflow comment sets.

Three states, never two
-----------------------

`docs/validators.md` §"Declining instead of guessing". The arm that matters is
`declined`: a gate that cannot run — no ruff, an unresolvable base — must not
exit 0, because "nothing to report" and "I did not look" are the same output
otherwise, and that equivalence is the whole of #1481.

Ruff is *required* by this module rather than `skipif`-gated, unlike
`tests/test_validators_ruff.py`. It is a declared `dev` dependency,
`docs/contributing.md` tells contributors to install with `pip install -e .[dev]`,
and all twelve CI legs do. A skip would let the tests of a gate whose whole
subject is "a check that did not run must not look green" disappear from a
runner without a word — the defect wearing the fix's clothes. A hard failure
names the missing dependency instead.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / ".github" / "scripts" / "lint_new_files.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("lint_new_files_1481", GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

DEAD_IMPORT = "import json" + chr(10) + chr(10) + "X = 1" + chr(10)
UNDEFINED_NAME = "def f():" + chr(10) + "    return nope" + chr(10)
CLEAN = "X = 1" + chr(10)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, args + (r.stdout, r.stderr)
    return r.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A base commit on `main`, and a `topic` branch checked out on top of it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    # The gate resolves ruff's configuration the way ruff does -- by walking up
    # from the file -- so the fixture repo carries the same select/ignore pair
    # this repository does. Without it these tests would measure ruff defaults.
    (root / "pyproject.toml").write_text(
        "[tool.ruff.lint]" + chr(10)
        + 'select = ["E9", "F", "B", "PLE"]' + chr(10)
        + 'ignore = ["F401", "F841", "F541"]' + chr(10),
        encoding="utf-8")
    (root / "kept.py").write_text(CLEAN, encoding="utf-8")
    (root / "old.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "topic")
    return root


def _commit(root: Path, message: str = "change") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _gate(root: Path):
    """`(exit_code, output)` for one gate run over `root`.

    Not spelled `_run(..., **kwargs)`: `tests/test_encoding_seam.py` scans every
    `run(` call site in this directory for a literal `encoding=`/`errors=`, and
    a forwarded `**kwargs` is a call it declines to judge rather than passes.
    Being unreadable to that scan is the same defect class as the one this file
    is about, one layer over.
    """
    return gate.run(base="main", head="HEAD", cwd=root)


# --- the diff is the scope --------------------------------------------------


def test_a_file_the_diff_adds_is_checked(repo: Path) -> None:
    (repo / "added.py").write_text(UNDEFINED_NAME, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "added.py" in out, out
    assert "F821" in out, out


def test_a_file_the_diff_only_modifies_is_not_checked(repo: Path) -> None:
    """The whole point of scoping to the diff.

    `old.py` carries a dead import from before this branch existed. Reporting it
    is the tree-wide gate the workflow refuses, on somebody else's line.
    """
    (repo / "old.py").write_text(DEAD_IMPORT + "Y = 2" + chr(10), encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert "old.py" not in out, out


def test_a_file_nobody_touched_is_not_checked(repo: Path) -> None:
    (repo / "added.py").write_text(CLEAN, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert "old.py" not in out, out


def test_a_non_python_file_is_not_handed_to_ruff(repo: Path) -> None:
    (repo / "notes.md").write_text("# hi" + chr(10), encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert "notes.md" not in out, out


# --- the two facts the issue got wrong -------------------------------------


def test_a_dead_import_in_an_added_file_is_a_finding(repo: Path) -> None:
    """The reported defect, and the reason the gate does not just run `ruff check`.

    F401 is ignored repo-wide with a reason that is about the existing tree. A
    path with no history has no share of that debt, so the rule is back on for
    the files this gate checks -- and without this the gate would return
    `All checks passed!` for exactly the file #1481 was filed about.
    """
    (repo / "added.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "F401" in out, out


def test_a_renamed_file_is_checked(repo: Path) -> None:
    """`_release_gate.py` was `R065`, not `A`. A gate scoped to additions alone
    would have missed the instance it was filed for."""
    _git(repo, "mv", "old.py", "moved.py")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "moved.py" in out, out
    assert "F401" in out, out


def test_a_ruff_exclude_cannot_silently_drop_a_file_from_the_report(
    repo: Path,
) -> None:
    """The gate's own file list is hand-built from the diff, not discovered.

    `--force-exclude` makes ruff apply `[tool.ruff] exclude` to paths handed to
    it explicitly, so a new file under an excluded pattern is dropped from the
    invocation while ruff still exits 0 — and the report then lists it as
    checked and clean. That is #1481's exact failure mode reproduced inside the
    gate written to close it, and it bypasses the `declined` state built for
    precisely this uncertainty. Without the flag, an explicitly-named path is
    always checked, so `N file(s) ... all clean` is a claim the run earned.
    """
    (repo / "added.py").write_text(DEAD_IMPORT, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.ruff]" + chr(10)
        + 'exclude = ["added.py"]' + chr(10)
        + "[tool.ruff.lint]" + chr(10)
        + 'select = ["E9", "F", "B", "PLE"]' + chr(10)
        + 'ignore = ["F401", "F841", "F541"]' + chr(10),
        encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, (
        "an excluded path was listed as checked and clean without ruff ever "
        "opening it:" + chr(10) + out)
    assert "F401" in out, out


def test_the_gate_names_the_rules_it_re_enables(repo: Path) -> None:
    """A finding on a rule the tree ignores is confusing without this line."""
    (repo / "added.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    _code, out = _gate(repo)
    assert "F401" in out and "F841" in out and "F541" in out, out


# --- the number the finding arm reports (#1629) -----------------------------


def test_the_finding_count_is_the_files_carrying_findings_not_the_files_checked(
    repo: Path,
) -> None:
    """#1629: the arm reported `len(files)`, the set it *checked*.

    Measured on a 46-file diff with one dirty path, it printed `46 file(s) ...
    carry lint findings` above a single listed line. That sentence was read as
    "one of 46 shown" and became a brief telling an agent to go looking for
    findings the gate had truncated. Nothing was truncated; the number was
    answering a different question from the one it was worded as.

    The single-file diff is why it survived: when every checked file is dirty
    the two counts coincide, and that is the shape that ships most often.
    """
    (repo / "dirty.py").write_text(DEAD_IMPORT, encoding="utf-8")
    (repo / "clean.py").write_text(CLEAN, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    # Anchored on the whole headline, not searched for inside it. `"2 file(s)
    # this PR" not in out` is satisfied by the correct output and by the broken
    # one alike, because the fixed sentence contains the broken one as a
    # substring -- which is #1661's defect written into #1629's test.
    headline = [line.strip() for line in out.splitlines()
                if "carry lint findings" in line]
    assert headline == ["1 of 2 file(s) this PR adds, copies or renames carry "
                        "lint findings:"], out


def test_a_finding_line_that_names_no_file_leaves_the_count_unstated(
    repo: Path, monkeypatch,
) -> None:
    """A count the run cannot derive is not rounded down to the ones it can.

    Every finding line ruff has ever emitted in `concise` carries `path:line:
    col:`, so this arm should never fire -- which is exactly why it is pinned.
    An output shape the parser cannot read would otherwise silently lower the
    numerator, and a gate that under-counts its own findings is this repo's
    house defect with a smaller number on it.
    """
    real = gate._run

    def unreadable(argv, cwd):
        if argv[1:2] == ["check"]:
            return 1, "dirty.py:1:8: F401 unused" + chr(10) + "??? mystery" + chr(10), ""
        return real(argv, cwd)

    (repo / "dirty.py").write_text(DEAD_IMPORT, encoding="utf-8")
    (repo / "clean.py").write_text(CLEAN, encoding="utf-8")
    _commit(repo)
    monkeypatch.setattr(gate, "_run", unreadable)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "1 of 2" not in out, out
    assert "name no file" in out, out
    assert "mystery" in out, out


# --- three states, and the third is the point -------------------------------


def test_a_clean_diff_says_what_it_checked(repo: Path) -> None:
    (repo / "added.py").write_text(CLEAN, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert "added.py" in out, out
    assert gate.STATE_OK in out, out


def test_a_diff_that_adds_no_python_file_states_that_rather_than_going_quiet(
    repo: Path,
) -> None:
    """Silence and a pass are the same output, which is #1481 in one sentence."""
    (repo / "notes.md").write_text("# hi" + chr(10), encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert gate.STATE_OK in out, out
    assert "no added" in out.lower() or "0 file" in out.lower(), out


def test_a_base_that_does_not_resolve_declines_rather_than_reporting_clean(
    repo: Path,
) -> None:
    """The house defect, pointed at this gate.

    An unresolvable base yields an empty file list, and an empty file list one
    branch away reads as `nothing to check`. It must exit 2.
    """
    code, out = gate.run(base="no/such/ref", head="HEAD", cwd=repo)
    assert code == gate.EXIT_DECLINED, out
    assert gate.STATE_DECLINED in out, out
    assert gate.STATE_OK not in out, out
    assert "no/such/ref" in out, out


def test_ruff_absent_declines_rather_than_reporting_clean(repo: Path) -> None:
    """`skipped`, never `ok` -- the same rule `validators/ruff/ruff.py` follows.

    Exit 2 rather than 0 because this job installs ruff itself: absent here
    means the job is broken, not that a contributor lacks a tool.
    """
    (repo / "added.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    code, out = gate.run(base="main", head="HEAD", cwd=repo, ruff=None)
    assert code == gate.EXIT_DECLINED, out
    assert gate.STATE_DECLINED in out, out
    assert "ruff" in out.lower(), out


def test_ruff_failing_to_start_declines_rather_than_reporting_clean(
    repo: Path,
) -> None:
    """#997's class: a spawn failure that only happens on one platform.

    `which` said yes and exec said no. Swallowing it would report an absence of
    findings from a checker that never ran.
    """
    (repo / "added.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    code, out = gate.run(base="main", head="HEAD", cwd=repo,
                         ruff=str(repo / "not-a-real-binary"))
    assert code == gate.EXIT_DECLINED, out
    assert gate.STATE_DECLINED in out, out


def test_ruff_exiting_on_its_own_config_declines_rather_than_finding(
    repo: Path,
) -> None:
    """ruff exit 2 is ruff failing, not the file being wrong."""
    (repo / "added.py").write_text(CLEAN, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.ruff.lint]" + chr(10) + 'select = ["NOSUCHRULE"]' + chr(10),
        encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_DECLINED, out
    assert gate.STATE_DECLINED in out, out


def test_git_missing_declines_rather_than_reporting_an_empty_diff(
    repo: Path,
) -> None:
    code, out = gate.run(base="main", head="HEAD", cwd=repo,
                         git=str(repo / "not-a-real-git"))
    assert code == gate.EXIT_DECLINED, out
    assert gate.STATE_DECLINED in out, out


def test_a_diff_that_fails_after_the_base_resolved_declines(
    repo: Path, monkeypatch
) -> None:
    """The second git call is a second chance to return an empty list.

    `--diff-filter` is not the kind of thing that fails often, which is exactly
    why the arm needs pinning: an unexercised branch that returns `[]` is one
    refactor away from being the clean-looking absence this whole file is
    about.
    """
    real = gate._run

    def once(argv, cwd):
        if "diff" in argv:
            return 128, "", "fatal: bad revision"
        return real(argv, cwd)

    monkeypatch.setattr(gate, "_run", once)
    code, out = _gate(repo)
    assert code == gate.EXIT_DECLINED, out
    assert "bad revision" in out, out


def test_a_binary_that_is_not_executable_declines(repo: Path) -> None:
    """#620's class: Windows raises `PermissionError` where POSIX raises
    `IsADirectoryError`, and both are `OSError`. Catching one by name would
    leave the handler dead on the other platform, which is a decline that never
    fires and therefore a green that never looked."""
    (repo / "added.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    code, out = gate.run(base="main", head="HEAD", cwd=repo, ruff=str(repo))
    assert code == gate.EXIT_DECLINED, out
    assert gate.STATE_DECLINED in out, out


def test_a_checker_that_timed_out_declines_and_names_its_budget(
    repo: Path, monkeypatch
) -> None:
    """A reader who sees `timeout` cannot tell a hung linter from a busy
    machine, and the number is the first thing they need (#658)."""
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["ruff"], timeout=gate.TIMEOUT_S)

    (repo / "added.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    monkeypatch.setattr(gate.subprocess, "run", hang)
    code, out = gate.run(base="main", head="HEAD", cwd=repo, ruff="ruff")
    assert code == gate.EXIT_DECLINED, out
    assert str(gate.TIMEOUT_S) in out, out


def test_a_diff_row_with_no_tab_is_skipped_rather_than_indexed() -> None:
    """`--name-status` is tab-separated and a blank trailing line is not a row.

    `fields[-1]` on a line that never split would hand ruff the status letter
    as a path.
    """
    tab, nl = chr(9), chr(10)
    rows = ("A" + tab + "new.py" + nl + nl
            + "R100" + tab + "old.py" + tab + "moved.py" + nl)
    assert gate._changed_paths(rows) == ["new.py", "moved.py"]


# --- how the scope is resolved when nobody passes one ----------------------


def test_a_pull_request_event_resolves_its_base_from_the_environment() -> None:
    assert gate.resolve_base({"GITHUB_BASE_REF": "master"}) == "origin/master"


def test_a_push_is_not_a_pull_request_and_says_so(repo: Path) -> None:
    """Not a decline: a push has no PR scope to be measured against, and
    inventing one is worse than naming the state."""
    code, out = gate.main([], env={}, cwd=repo)
    assert code == gate.EXIT_OK, out
    assert gate.STATE_SKIPPED in out, out
    assert "pull_request" in out, out


def test_an_explicit_base_beats_the_environment(repo: Path) -> None:
    (repo / "added.py").write_text(UNDEFINED_NAME, encoding="utf-8")
    _commit(repo)
    code, out = gate.main(["--base", "main"],
                          env={"GITHUB_BASE_REF": "nonsense"}, cwd=repo)
    assert code == gate.EXIT_FINDING, out


def test_every_run_prints_exactly_one_state(repo: Path) -> None:
    """A report that names no state is one somebody reads as a pass."""
    (repo / "added.py").write_text(CLEAN, encoding="utf-8")
    _commit(repo)
    for code, out in (_gate(repo),
                      gate.run(base="no/such/ref", head="HEAD", cwd=repo)):
        states = [s for s in (gate.STATE_OK, gate.STATE_FINDING,
                              gate.STATE_DECLINED, gate.STATE_SKIPPED)
                  if s in out]
        assert len(states) == 1, (code, states, out)


# --- a path out of the diff is data, never an option ------------------------


def test_a_dash_named_path_is_linted_rather_than_parsed_as_an_option(
        repo: Path) -> None:
    """The file list is picked up, never passed in, so ruff needs a `--`.

    `--stdin-filename=x.py` ends in `.py`, so `_python` keeps it; without the
    separator ruff's own parser eats it as an option, no path is left on the
    command line, ruff exits 0 with an empty stdout, and the report lists the
    file under "all clean" having never opened it. That is #1481's own failure
    mode reproduced inside the gate written to close it -- the same argument
    the `--force-exclude` comment makes two lines above the argv.

    The name is a real ruff option and not a shape chosen to be awkward: a
    filename suffix is the only thing between the diff and the option parser,
    and a suffix is not a security property.
    """
    (repo / "--stdin-filename=x.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "F401" in out, out


def test_the_module_runs_as_a_script(repo: Path) -> None:
    """The workflow calls it by path; nothing else pins that it is runnable."""
    r = subprocess.run([sys.executable, str(GATE_PATH), "--base", "main"],
                       cwd=str(repo), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == gate.EXIT_OK, (r.returncode, r.stdout, r.stderr)
    assert gate.STATE_OK in r.stdout, r.stdout
