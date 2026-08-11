"""`gh-branch`'s verdict sentences agree with the number of workflows (#841).

Live on master before this:

    Verdict: NOT GREEN — nothing has failed, but `CodeQL`, `tests` has not
    concluded on d6cf7a4, so they are neither a pass nor a fail.

`has` is singular, `they are` is plural, and both refer to the same list. The
pronoun half was already count-aware (`'it is' if len(moving) == 1 else 'they
are'`); the verb half was written for the singular case and left fixed when
`_names()` generalised the subject to N workflows.

This is the sentence a maintainer reads at every merge gate, so every branch of
`verdict()` that interpolates a `_names()` list is pinned here, at one workflow
and at two — not only the branch the issue reported. Each test pairs the
singular control with the plural case in the same body: the singular half passes
against the pre-#841 code, so neither assertion stands alone as proof.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_841")

SHA = "d6cf7a4e1f4c0d2f7b9e8a3d5c1b0e2f4a6c8d09"

_DONE = {"status": "completed", "conclusion": "success",
         "databaseId": 1, "createdAt": "2026-01-01T00:00:00Z"}


def _say(selected, legs, missing=(), age=0, unreconciled=""):
    return branch.verdict(selected, legs, set(missing), SHA, age,
                          unreconciled=unreconciled, scope="")[1]


# ---------------------------------------------------------------------------
# the reported branch: workflows that have not concluded
# ---------------------------------------------------------------------------

def test_unconcluded_workflows_take_a_plural_verb_and_a_plural_pronoun() -> None:
    one = _say({"tests": _DONE}, {"tests": ["IN_PROGRESS"]})
    two = _say({"tests": _DONE, "CodeQL": _DONE},
               {"tests": ["IN_PROGRESS"], "CodeQL": ["QUEUED"]})
    assert "`tests` has not concluded" in one
    assert "so it is neither a pass nor a fail" in one
    assert "`CodeQL`, `tests` have not concluded" in two
    assert "so they are neither a pass nor a fail" in two


def test_the_verb_and_the_pronoun_never_disagree_with_each_other() -> None:
    """The defect was not `has` being wrong in isolation — it was one half of a
    sentence agreeing with the count while the other half did not."""
    for names in (["tests"], ["tests", "CodeQL"], ["tests", "CodeQL", "lint"]):
        said = _say({n: _DONE for n in names},
                    {n: ["IN_PROGRESS"] for n in names})
        verb_singular = "has not concluded" in said
        pronoun_singular = "it is neither" in said
        assert verb_singular == pronoun_singular, (
            "verb and pronoun disagree for %d workflow(s): %s"
            % (len(names), said))


# ---------------------------------------------------------------------------
# the siblings the issue asked to be checked rather than only the reported one
# ---------------------------------------------------------------------------

def test_workflows_with_no_run_on_this_head_take_a_plural_verb() -> None:
    one = _say({"tests": _DONE}, {"tests": ["SUCCESS"]}, missing=["CodeQL"])
    two = _say({"tests": _DONE}, {"tests": ["SUCCESS"]},
               missing=["CodeQL", "lint"])
    assert "`CodeQL` ran on the previous head and has no run" in one
    assert "`CodeQL`, `lint` ran on the previous head and have no run" in two


def test_an_unread_job_list_pluralises_the_thing_that_did_not_come_back() -> None:
    one = _say({"tests": _DONE}, {"tests": None})
    two = _say({"tests": _DONE, "CodeQL": _DONE},
               {"tests": None, "CodeQL": None})
    assert "the job list for `tests` did not come back" in one
    assert "the job lists for `CodeQL`, `tests` did not come back" in two


def test_the_red_branch_that_already_agreed_still_does() -> None:
    """`leg`/`legs` off `bad` was the one sibling written correctly, and is the
    pattern the rest were brought onto. Regression guard, not a fix."""
    one = _say({"tests": _DONE}, {"tests": ["FAILURE"]})
    two = _say({"tests": _DONE}, {"tests": ["FAILURE", "FAILURE"]})
    assert "1 leg on" in one
    assert "2 legs on" in two
