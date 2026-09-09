"""#2447 -- one git invocation chokepoint, reachable from every preset.

`presets/git/_git_common.py` grew the hardening (`_with_lock_retry`, the
`_stop()` grace, `git_timeout()`'s `SUPERTOOL_GIT_TIMEOUT` override) and two
files never got any of it, because each carried its own `_git()` built on a
bare `subprocess` call: `presets/dashboard/dashboard.py` and
`presets/github/pr_merge.py`. #1945 had to propagate `--no-optional-locks` to
all three by hand, and `tests/test_git_no_optional_locks_other_sites_1945.py`
says so in its own first paragraph -- "the fix has to land at each wrapper
separately".

The chokepoint now lives at `presets/_git_run.py`, a shared helper at the
`presets/` root rather than inside one preset's directory, which is what lets
`presets/dashboard/` and `presets/github/` reach it without either of them
reaching into `presets/git/`. `_git_common` re-exports it, so the git presets
are unchanged.

What this file pins is that a FOURTH private wrapper reds the suite instead of
being found by the next flag somebody has to sweep by hand.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: The chokepoint itself. The one file under `presets/` allowed to build a
#: `["git", ...]` argv and spawn it.
CHOKEPOINT = "presets/_git_run.py"

#: The two files #2447 is about. Scoped to these rather than swept over all of
#: `presets/`, because the other 24 `["git", ...]` sites in this tree are
#: one-shot reads nobody has judged against this chokepoint's contract, and a
#: register asserting a reason for each of them would be 24 reasons invented in
#: one sitting. These two were read, and they are the two the issue names.
ADOPTERS = (
    "presets/dashboard/dashboard.py",
    "presets/github/pr_merge.py",
)


def _load(rel: str, name: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_argv_sites(rel: str) -> list:
    """Every `["git", ...]` list literal in `rel`, as `(lineno, function)`.

    AST, not grep: a docstring naming `["git", ...]` as prose is not a call
    site, and this file's own docstring would match a grep for it.
    """
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    enclosing = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(id(child), node.name)
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value == "git":
                sites.append((node.lineno, enclosing.get(id(node), "<module>")))
    return sites


def test_the_chokepoint_exists_and_carries_the_hardening() -> None:
    """MUST FIRE. The extraction is the fix; without it there is nothing to adopt."""
    module = _load(CHOKEPOINT, "git_run_2447")
    for name in ("_git", "_git_attempt", "_git_verbatim", "_git_verbatim_attempt",
                 "_with_lock_retry", "_stop", "_settled", "git_timeout",
                 "TIMEOUT_RC", "LOCK_WAIT_DEFAULT"):
        assert hasattr(module, name), f"{CHOKEPOINT} does not export {name}"


def test_the_detector_finds_the_chokepoints_own_argv_sites() -> None:
    """MUST NOT FIRE -- positive control for the two assertions below.

    `_git_argv_sites` returning `[]` for a file is the whole evidence those
    assertions rest on, and an empty list is also what a broken detector
    returns for every file in the tree. So the detector is pointed at the one
    file that certainly does build a git argv, and has to find it there.
    """
    sites = _git_argv_sites(CHOKEPOINT)
    assert sites, f"the detector found no git argv in {CHOKEPOINT}, so its silence elsewhere means nothing"
    functions = {fn for _, fn in sites}
    assert "_git_attempt" in functions, sites
    assert "_git_verbatim_attempt" in functions, sites


@pytest.mark.parametrize("rel", ADOPTERS)
def test_an_adopter_builds_no_git_argv_of_its_own(rel: str) -> None:
    """MUST FIRE. A fourth private wrapper reds here."""
    sites = _git_argv_sites(rel)
    assert sites == [], (
        f"{rel} builds its own git argv at {sites} -- route it through "
        f"{CHOKEPOINT} instead, or this is the fourth wrapper the next "
        f"git-invocation fix has to be swept into by hand (#2447)"
    )


@pytest.mark.parametrize("rel", ADOPTERS)
def test_an_adopter_imports_the_chokepoint(rel: str) -> None:
    """MUST FIRE. Absence of a git argv is not presence of the shared one."""
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "_git_run" in imported, f"{rel} does not import _git_run; it imports {sorted(imported)}"


@pytest.mark.parametrize("rel,name", [
    ("presets/dashboard/dashboard.py", "dashboard_2447_env"),
    ("presets/github/pr_merge.py", "pr_merge_2447_env"),
])
def test_supertool_git_timeout_reaches_both_adopters(rel, name, monkeypatch) -> None:
    """MUST FIRE. #1886/#1903 joined the prose to the code at three sites; these
    two were a fourth and fifth number that read the variable not at all."""
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "77")
    module = _load(rel, name)
    budgets = []

    def _fake_attempt(args, timeout=None):
        budgets.append(timeout)
        return subprocess.CompletedProcess(args=["git"] + list(args), returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(module._git_run, "_git_attempt", _fake_attempt)
    module._git(["rev-parse", "--short", "HEAD"])
    assert budgets == [77], (
        f"{rel} ran its git call on {budgets}, not the 77 SUPERTOOL_GIT_TIMEOUT asked for"
    )


def test_the_env_control_would_notice_a_budget_that_ignored_it(monkeypatch) -> None:
    """MUST NOT FIRE -- positive control for the parametrised test above.

    That test passes if the budget is 77. It would also pass against a
    chokepoint that hardcoded 77 and read no environment at all, so the same
    call is made a second time under a different value.
    """
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "12")
    module = _load("presets/dashboard/dashboard.py", "dashboard_2447_env_control")
    budgets = []

    def _fake_attempt(args, timeout=None):
        budgets.append(timeout)
        return subprocess.CompletedProcess(args=["git"] + list(args), returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(module._git_run, "_git_attempt", _fake_attempt)
    module._git(["rev-parse", "--short", "HEAD"])
    assert budgets == [12], budgets


def test_git_common_still_re_exports_the_chokepoint() -> None:
    """MUST FIRE. Eight git presets import these names from `_git_common`; the
    extraction must not have moved them out from under any of them.

    Compared against `sys.modules["_git_run"]` -- the module `_git_common`'s own
    `from _git_run import ...` resolved -- and NOT against a second
    `_load(CHOKEPOINT, ...)`. `importlib.util.module_from_spec` builds a fresh
    module object every call and registers nothing, so loading the file twice
    produces two sets of functions that are equal in behaviour and identical in
    nothing. That is what this assertion caught on its first run, and it was the
    test being wrong rather than the code: an identity check against a
    hand-loaded copy can only ever fail.
    """
    common = _load("presets/git/_git_common.py", "git_common_2447")
    run = sys.modules["_git_run"]
    for name in ("_git", "_git_attempt", "_git_verbatim", "_git_verbatim_attempt",
                 "_with_lock_retry", "_stop", "_settled", "git_timeout",
                 "TIMEOUT_RC", "LOCK_WAIT_DEFAULT"):
        assert hasattr(common, name), f"_git_common no longer carries {name}"
        assert getattr(common, name) is getattr(run, name), (
            f"_git_common.{name} is a second copy, not the chokepoint's own"
        )


def test_the_identity_check_would_notice_a_second_copy() -> None:
    """MUST NOT FIRE -- positive control for the assertion above.

    `is` against a re-export passes trivially; what it has to be able to do is
    FAIL against a genuine second copy. So one is built and compared the same
    way.
    """
    run = sys.modules["_git_run"]
    second = _load(CHOKEPOINT, "git_run_2447_second_copy")
    assert second._git is not run._git, (
        "two independent loads of the chokepoint produced the same function "
        "object, so the identity check above cannot distinguish a copy"
    )
