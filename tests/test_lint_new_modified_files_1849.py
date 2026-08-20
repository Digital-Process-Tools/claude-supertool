"""A file the PR *modifies* is unchecked by both lint gates at once (#1849).

`F401`, `F841` and `F541` are ignored tree-wide (#797, #1481): the ignore is
about 263 pre-existing findings in files that have a history. `lint-new`
re-enables them, but only for paths a PR adds, copies or renames. So a file the
PR **modifies** falls between the two -- the tree-wide ignore hides the finding
locally, and the `ACR` scope hides it in CI.

Measured on PR #1843, at `c974d493`
-----------------------------------

`lint-new` reported one finding, in the only file that PR adds. Sweeping the
whole diff with the same rule set found **five**; the other four were in
modified files and would have shipped:

    tests/test_guard_refusal_names_no_bypass_1706.py:28:8: F401 `pytest` ...
    tests/test_guard_scope_is_a_route_not_a_file_1671.py:36:8: F401 `pytest` ...
    tests/test_ops_default_fits_the_session_cap_1774.py:34:8: F401 `pytest` ...
    tests/test_repo_op_registry_1240.py:26:8: F401 `pytest` ...

All four have the same cause: **deleting a fixture orphans whatever only it
used.** The conversion removed a hand-rolled `shipped_config` fixture that was
the sole consumer of `pytest` in each file, via `@pytest.fixture` and a
`monkeypatch: pytest.MonkeyPatch` annotation.

Why the scope is a baseline, not touched lines
----------------------------------------------

The obvious route -- intersect ruff line numbers with `git diff --unified=0`
hunk ranges -- **cannot see the case that motivated the issue**, and this was
measured rather than reasoned. At `c974d493` all four diffs are *pure
deletions*: every hunk is `+N,0`, so the set of touched lines in those files is
**empty**, and the orphaned `import pytest` sits at a line the PR never
touched. A touched-lines gate reports 0 of the 4.

So the scope is the file's own **merge-base baseline**: run the same re-enabled
rule set over the base content and over the head content, and report a head
finding only when its `(code, message)` is not already accounted for at base.
That is the mechanism the supertool `ruff` validator uses per edit, applied at
PR granularity -- which is the granularity a whole-file arrival needs, and the
one the validator's own docstring says it cannot reach.

`test_the_orphaning_deletion_touches_no_line_at_the_import` is the positive
control for that decision: it fails if anybody re-scopes this to touched lines.

The pairing this file exists to hold
------------------------------------

Both halves, always, and the second is not optional: a modified file that
*gains* a finding must be reported, and a modified file carrying a
**pre-existing** finding must stay quiet. Without the second, the fix passes by
reporting everything and the tree-wide ignore is gone -- a far larger change
arriving disguised as this one.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / ".github" / "scripts" / "lint_new_files.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("lint_new_files_1849", GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

NL = chr(10)

#: A pre-existing F401, committed on `main` before the topic branch exists.
DEAD_IMPORT = "import json" + NL + NL + "X = 1" + NL

#: `import json` is live here. Deleting `load` orphans it -- and deletes only,
#: so the resulting hunk carries no added line at all.
ORPHAN_BASE = (
    "import json" + NL
    + NL
    + NL
    + "def load(text):" + NL
    + "    return json.loads(text)" + NL
    + NL
    + NL
    + "def keep():" + NL
    + "    return 2" + NL
)
ORPHAN_HEAD = (
    "import json" + NL
    + NL
    + NL
    + "def keep():" + NL
    + "    return 2" + NL
)

CLEAN = "X = 1" + NL


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, args + (r.stdout, r.stderr)
    return r.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """`main` carrying the two shapes, with `topic` checked out on top."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    # The same select/ignore pair this repository carries, because the gate
    # resolves ruff configuration the way ruff does -- by walking up from the
    # file. Without it these tests would measure ruff defaults, under which
    # F401 is not ignored and the whole subject of #1849 does not exist.
    (root / "pyproject.toml").write_text(
        "[tool.ruff.lint]" + NL
        + 'select = ["E9", "F", "B", "PLE"]' + NL
        + 'ignore = ["F401", "F841", "F541"]' + NL,
        encoding="utf-8")
    (root / "orphan.py").write_text(ORPHAN_BASE, encoding="utf-8")
    (root / "old.py").write_text(DEAD_IMPORT, encoding="utf-8")
    (root / "kept.py").write_text(CLEAN, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "topic")
    return root


def _commit(root: Path, message: str = "change") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _gate(root: Path):
    """`(exit_code, output)` for one gate run over `root`.

    Spelled out rather than `**kwargs`-forwarded: `tests/test_encoding_seam.py`
    scans every `run(` call site in this directory for a literal
    `encoding=`/`errors=`, and a forwarded `**kwargs` is a call it declines to
    judge rather than one it passes.
    """
    return gate.run(base="main", head="HEAD", cwd=root)


# --- the half the issue was filed for --------------------------------------


def test_a_finding_this_pr_introduces_into_a_modified_file_is_reported(
    repo: Path,
) -> None:
    """PR #1843's four invisible findings, in miniature.

    `orphan.py` has a history, so the `ACR` scope never opened it; the
    tree-wide ignore switched F401 off everywhere else. `lint-new: ok` on a
    diff carrying this is not a false negative anybody can see -- it renders
    identically to a clean diff, which is this repository's own named defect
    class landing on its own tooling.
    """
    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "orphan.py" in out, out
    assert "F401" in out, out
    assert "json" in out, out


def test_the_orphaning_deletion_touches_no_line_at_the_import(repo: Path) -> None:
    """The positive control for choosing a baseline over touched lines.

    This asserts the *premise* of the design, not the design: the diff that
    orphans the import adds no line at all, so the touched-line set is empty
    and any gate scoped to it reports nothing here. Measured identically on
    PR #1843 at `c974d493`, where all four hunks are `+N,0`.

    If somebody re-scopes the gate to `--unified=0` hunk ranges, the test
    above goes red and this one says why.
    """
    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    _commit(repo)
    diff = _git(repo, "diff", "--unified=0", "-M", "main...HEAD", "--", "orphan.py")
    added = [ln for ln in diff.splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]
    assert added == [], diff
    hunks = [ln for ln in diff.splitlines() if ln.startswith("@@")]
    assert hunks, diff
    # ... and the import ruff will name is line 1, which no hunk covers.
    assert all(",0 @@" in h for h in hunks), diff


# --- the half without which the fix is a much larger change ----------------


def test_a_pre_existing_finding_in_a_modified_file_stays_quiet(repo: Path) -> None:
    """`old.py` carried its dead import before this branch existed.

    Reporting it is the tree-wide gate `tests.yml` deliberately is not, on
    somebody else's line. Without this assertion the fix for #1849 passes by
    reporting everything, and the ignore #797 added is effectively gone.
    """
    (repo / "old.py").write_text(DEAD_IMPORT + "Y = 2" + NL, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    # `"F401" not in out` would be wrong here and would pass for the wrong
    # reason: the receipt names the three re-enabled rules on every run. What
    # must be absent is a *finding line*, and a finding line is the only place
    # the path is followed by `:line:col:`. The scope listing spells it bare.
    assert "old.py:" not in out, out


def test_both_halves_in_one_run(repo: Path) -> None:
    """The pairing, in a single diff, because each half alone is satisfiable
    by a gate that is wrong in the other direction."""
    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    (repo / "old.py").write_text(DEAD_IMPORT + "Y = 2" + NL, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "orphan.py:" in out, out
    assert "old.py:" not in out, out


def test_a_pre_existing_finding_is_matched_by_message_not_by_line(
    repo: Path,
) -> None:
    """Insertions above a pre-existing finding move it; they do not create it.

    Keying the baseline on `path:line:col` would report every finding below an
    added line, which is the "report everything" failure with a plausible
    mechanism attached.
    """
    (repo / "old.py").write_text(
        "Y = 2" + NL + "Z = 3" + NL + DEAD_IMPORT, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert "old.py:" not in out, out


def test_a_second_occurrence_of_an_existing_finding_is_still_new(
    repo: Path,
) -> None:
    """Counted as a multiset, not a set.

    `old.py` already has one unused `json`. Adding an unused `os` is a
    different message and is plainly new; adding a *second* dead import of the
    kind already there is the case a set difference silently swallows.
    """
    (repo / "old.py").write_text(
        "import json" + NL + "import os" + NL + NL + "X = 1" + NL,
        encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "`os`" in out, out
    # The pre-existing one is still not reported, in the same run.
    assert "`json`" not in out, out


def test_introduced_is_a_multiset_difference() -> None:
    """The unit the two tests above exercise through the gate.

    Two `F541`s at base and three at head is one new finding, not zero (a set
    difference) and not three (no baseline at all).
    """
    base = (NL.join([
        "a.py:1:5: F541 [*] f-string without any placeholders",
        "a.py:2:5: F541 [*] f-string without any placeholders",
    ]) + NL)
    head = (NL.join([
        "a.py:1:5: F541 [*] f-string without any placeholders",
        "a.py:2:5: F541 [*] f-string without any placeholders",
        "a.py:9:5: F541 [*] f-string without any placeholders",
    ]) + NL)
    assert gate._introduced(head, base) == [
        "a.py:9:5: F541 [*] f-string without any placeholders"]
    assert gate._introduced(base, base) == []
    assert gate._introduced(base, "") == base.strip().splitlines()


# --- the receipt, which two people misread in one session ------------------


def test_the_receipt_names_both_scopes_when_clean(repo: Path) -> None:
    """`1 of 1 file(s) this PR adds, copies or renames` is accurate and was
    read, by two different people in one session, as `1 of 1 file(s) in this
    PR`. A receipt whose scope has to be inferred is the same class of absence
    as the gap it is describing."""
    (repo / "added.py").write_text(CLEAN, encoding="utf-8")
    (repo / "old.py").write_text(DEAD_IMPORT + "Y = 2" + NL, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert "checked whole" in out, out
    assert "checked only for findings this PR introduces" in out, out
    assert "added.py" in out, out
    assert "old.py" in out, out


def test_the_finding_headline_no_longer_claims_the_acr_scope(repo: Path) -> None:
    """The count is over the whole diff now, so the noun has to be too."""
    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    (repo / "added.py").write_text(CLEAN, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_FINDING, out
    assert "this PR adds, copies or renames carry lint findings" not in out, out
    headline = [ln.strip() for ln in out.splitlines() if "carry lint findings" in ln]
    assert headline == ["1 of 2 .py file(s) in this PR's diff carry lint "
                        "findings:"], out


def test_a_diff_with_no_python_file_at_all_says_so_in_both_scopes(
    repo: Path,
) -> None:
    (repo / "notes.md").write_text("# hi" + NL, encoding="utf-8")
    _commit(repo)
    code, out = _gate(repo)
    assert code == gate.EXIT_OK, out
    assert "no .py file this PR adds, copies or renames" in out, out
    assert "no .py file this PR modifies" in out, out


# --- three states, and the third is still the point ------------------------


def test_a_baseline_that_cannot_be_linted_declines_rather_than_reporting_clean(
    repo: Path, monkeypatch,
) -> None:
    """A head finding with no readable baseline is not `pre-existing`.

    Both silent defaults are wrong and only one of them is loud. Treating it as
    pre-existing reproduces #1849 inside the fix for it; treating it as new
    reports somebody else's line under a `finding` heading. The gate has a
    third state and this is what it is for.
    """
    real = gate._run

    def no_baseline(argv, cwd, stdin=None):
        if stdin is not None:
            return None, "", "could not lint the base revision of this file"
        return real(argv, cwd, stdin)

    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    _commit(repo)
    monkeypatch.setattr(gate, "_run", no_baseline)
    code, out = _gate(repo)
    assert code == gate.EXIT_DECLINED, out
    assert gate.STATE_DECLINED in out, out
    assert "orphan.py" in out, out
    assert "not a clean result" in out.lower(), out


def test_a_base_revision_that_cannot_be_read_declines(
    repo: Path, monkeypatch,
) -> None:
    """`git show <merge-base>:path` failing is the same doubt one layer down."""
    real = gate._run

    def no_show(argv, cwd, stdin=None):
        if argv[1:2] == ["show"]:
            return 128, "", "fatal: invalid object name"
        return real(argv, cwd, stdin)

    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    _commit(repo)
    monkeypatch.setattr(gate, "_run", no_show)
    code, out = _gate(repo)
    assert code == gate.EXIT_DECLINED, out
    assert "orphan.py" in out, out


def test_an_unresolvable_merge_base_declines_rather_than_skipping_the_scope(
    repo: Path, monkeypatch,
) -> None:
    """No merge base means no baseline for any modified file.

    Falling back to "check nothing modified" here would be a green that never
    looked -- the shape #1481 is about, reintroduced by the fix for #1849.
    """
    real = gate._run

    def no_merge_base(argv, cwd, stdin=None):
        if argv[1:2] == ["merge-base"]:
            return 1, "", ""
        return real(argv, cwd, stdin)

    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    _commit(repo)
    monkeypatch.setattr(gate, "_run", no_merge_base)
    code, out = _gate(repo)
    assert code == gate.EXIT_DECLINED, out
    assert "merge base" in out.lower(), out


def test_nothing_this_gate_prints_is_non_ascii() -> None:
    """The report is printed, and a `print` is encoded with the *console's*
    codepage rather than the source file's.

    One em dash survived in the `ruff is not on PATH` arm after #1849 rewrote
    the rest of the reporting path to ASCII. It is safe on cp1252, where U+2014
    exists; it is not on cp437, where the `print` raises `UnicodeEncodeError`
    and kills the process -- turning a `declined` that was about to explain
    itself into a crash with no report at all, which is this repository's own
    defect class landing on the arm built to prevent it.

    Reachable only off the CI matrix (the job is ubuntu-latest under `-X utf8`)
    but squarely on a contributor running the script by hand, which the module
    docstring documents as a supported call. Docstrings are exempt: they reach
    stdout only through `--help`.
    """
    import ast as _ast
    import io as _io

    src = _io.open(str(GATE_PATH), encoding="utf-8").read()
    tree = _ast.parse(src)
    docstrings = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.ClassDef,
                             _ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], _ast.Expr):
                value = node.body[0].value
                if isinstance(value, _ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    offenders = []
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            odd = sorted({c for c in node.value if ord(c) > 127})
            if odd:
                offenders.append("{0}:{1}: {2!r} in {3!r}".format(
                    GATE_PATH.name, node.lineno, "".join(odd), node.value[:60]))
    assert offenders == [], NL.join(
        ["a runtime string literal here is encoded with the console codepage, "
         "not this file's:"] + offenders)


def test_every_run_still_prints_exactly_one_state(repo: Path) -> None:
    (repo / "orphan.py").write_text(ORPHAN_HEAD, encoding="utf-8")
    (repo / "old.py").write_text(DEAD_IMPORT + "Y = 2" + NL, encoding="utf-8")
    _commit(repo)
    _code, out = _gate(repo)
    states = [gate.STATE_OK, gate.STATE_FINDING, gate.STATE_DECLINED,
              gate.STATE_SKIPPED]
    assert sum(out.count(s) for s in states) == 1, out
