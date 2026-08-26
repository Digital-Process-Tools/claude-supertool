"""#1990: `gh-pr-merge` is the one op-mode acting op #1986 could not reach.

`resolve_or_conflict` gave the four payload-mode write ops a guarantee: an
ambient `SUPERTOOL_REPO` with no `repo:` op in the call reads as absence,
never as a caller-named target. `gh-pr-merge` has no payload field to
reconcile a target against -- it names its repository through
`gh_args`/`api_path` the way a read op does -- so #1986's chokepoint never
reached it, and an ambient value still directed the merge.

#1990 gives every `_repo_target` helper an `explicit` flag instead of a
payload `gh-pr-merge` does not have: `explicit=True` reads
`explicit_target()` (the same function `resolve_or_conflict` already
consults) rather than the bare env var, so `gh-pr-merge`'s own call sites
get the identical guarantee without becoming payload-mode.

The auditor reproduced #1990 by driving `gh_args()`/`api_path()` directly
rather than running the acting op -- a real merge is not something a test
should attempt -- and that is the shape kept here: these tests exercise the
`_repo_target` functions `presets/github/pr_merge.py` now calls with
`explicit=True`, never `gh-pr-merge` itself.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_repo_target(name: str) -> Any:
    path = REPO_ROOT / "presets" / "_repo_target.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT / "presets"))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


@pytest.fixture(autouse=True)
def clean_repo_env(monkeypatch):
    """Mirrors `test_repo_target_673.py`'s own fixture -- SUPERTOOL_REPO is
    process-global, and a value main()'s real pre-pass sets is invisible to
    monkeypatch's teardown (#1962)."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.delenv("SUPERTOOL_REPO_FROM_OP", raising=False)
    yield
    import os
    os.environ.pop("SUPERTOOL_REPO", None)
    os.environ.pop("SUPERTOOL_REPO_FROM_OP", None)


# ---------------------------------------------------------------------------
# the symptom: an ambient target with no repo: op in the call
# ---------------------------------------------------------------------------

def test_gh_args_explicit_ignores_an_ambient_target_with_no_op(monkeypatch) -> None:
    """The reproduction in #1990's own body: an ambient SUPERTOOL_REPO with
    no repo: op anywhere in the call must not direct gh-pr-merge. `from_op`
    is never set here, which is what an inherited shell export or a leaked
    value looks like."""
    monkeypatch.setenv("SUPERTOOL_REPO", "attacker/elsewhere")
    rt = _load_repo_target("rt_1990_ambient_gh_args")
    assert rt.gh_args(explicit=True) == []


def test_api_path_explicit_ignores_an_ambient_target_with_no_op(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "attacker/elsewhere")
    rt = _load_repo_target("rt_1990_ambient_api_path")
    assert rt.api_path("pulls/5/merge", explicit=True) == "repos/{owner}/{repo}/pulls/5/merge"


def test_target_explicit_is_none_with_no_op(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "attacker/elsewhere")
    rt = _load_repo_target("rt_1990_ambient_target")
    assert rt.target(explicit=True) is None
    # The bare read is untouched -- every read-only op still wants it.
    assert rt.target() == "attacker/elsewhere"


# ---------------------------------------------------------------------------
# must-fire control, in the same fixture shape: a real `repo:X gh-pr-merge:N`
# call sets both variables together, and that must still work. Pairing this
# with the tests above is what stops a harness that always answers "absent"
# from passing them for the wrong reason.
# ---------------------------------------------------------------------------

def test_gh_args_explicit_honours_a_real_repo_op(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/name")
    monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")
    rt = _load_repo_target("rt_1990_real_gh_args")
    assert rt.gh_args(explicit=True) == ["--repo", "owner/name"]


def test_api_path_explicit_honours_a_real_repo_op(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/name")
    monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")
    rt = _load_repo_target("rt_1990_real_api_path")
    assert rt.api_path("pulls/5/merge", explicit=True) == "repos/owner/name/pulls/5/merge"


def test_target_explicit_returns_value_with_a_real_repo_op(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/name")
    monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")
    rt = _load_repo_target("rt_1990_real_target")
    assert rt.target(explicit=True) == "owner/name"


# ---------------------------------------------------------------------------
# pr_merge.py itself calls every _repo_target site with explicit=True --
# a regression pin for the shape of the fix, not a re-test of the shared
# helper's own logic above.
# ---------------------------------------------------------------------------

def test_pr_merge_never_calls_repo_target_without_explicit() -> None:
    """Every `_repo_target.{target,gh_args,api_path,no_repo_error,
    not_found_scope,not_found_hint,api_path_for_display}` call in
    `pr_merge.py` must carry `explicit=True` -- gh-pr-merge is the one
    op-mode acting op in the tree (#1990), and a call site that regresses to
    the bare read silently reopens exactly the gap this issue closes."""
    import re
    src = (REPO_ROOT / "presets" / "github" / "pr_merge.py").read_text(encoding="utf-8")
    calls = re.findall(
        r"_repo_target\.(target|gh_args|api_path|no_repo_error|"
        r"not_found_scope|not_found_hint|api_path_for_display)\(([^)]*)\)",
        src,
    )
    assert calls, "expected pr_merge.py to call _repo_target at all"
    missing = [name for name, args in calls if "explicit" not in args]
    assert not missing, f"call(s) without explicit=True: {missing}"
