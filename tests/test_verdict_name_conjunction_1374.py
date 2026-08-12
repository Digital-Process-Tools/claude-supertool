"""#1374 — a list of workflow names with no conjunction reads as truncated.

Live on master before this:

    Verdict: NOT GREEN — nothing has failed, but `CodeQL`, `changelog`, `tests`
    have not concluded on d6cf7a4 ...

A trailing comma-separated list with no `and` is the shape a *truncated* list
has, in the one sentence whose job is to say whether the commit is cleared. The
reader's question at that moment is "is that all of them", and the punctuation
answers it wrong for free.

`_names()` is the single place every such list is assembled (#841), so every
`verdict()` branch that interpolates one is pinned here at two names and at
three — the one-name control passes against the pre-#1374 code, so neither
assertion stands alone as proof.
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


branch = _load("presets/github/branch.py", "github_branch_1374")

SHA = "d6cf7a4e1f4c0d2f7b9e8a3d5c1b0e2f4a6c8d09"

_DONE = {"status": "completed", "conclusion": "success",
         "databaseId": 1, "createdAt": "2026-01-01T00:00:00Z"}
_MOVING = {"status": "in_progress", "conclusion": "",
           "databaseId": 2, "createdAt": "2026-01-01T00:00:00Z"}


def _say(selected, legs, missing=(), age=0, unreconciled=""):
    return branch.verdict(selected, legs, set(missing), SHA, age,
                          unreconciled=unreconciled, scope="")[1]


# ---------------------------------------------------------------------------
# the joiner itself
# ---------------------------------------------------------------------------

def test_one_name_is_the_name() -> None:
    assert branch._names(["tests"]) == "`tests`"


def test_two_names_are_joined_by_a_conjunction_not_a_comma() -> None:
    assert branch._names(["CodeQL", "tests"]) == "`CodeQL` and `tests`"


def test_three_names_end_on_a_conjunction() -> None:
    assert (branch._names(["CodeQL", "changelog", "tests"])
            == "`CodeQL`, `changelog` and `tests`")


def test_no_rendered_list_ever_ends_on_a_bare_name_after_a_comma() -> None:
    """The property, not the three cases: the last separator is never a comma.

    A list ending `..., `tests`` is indistinguishable from one that was cut, and
    that is the whole defect. Asserted over 1..6 names so a future cap or a
    re-write of the joiner cannot reintroduce the shape at some other length.
    """
    for n in range(1, 7):
        rendered = branch._names([f"w{i}" for i in range(n)])
        assert " and " in rendered or n == 1, rendered
        assert not rendered.endswith("`, `w%d`" % (n - 1)) or n == 1, rendered


def test_the_empty_list_is_not_silently_a_name() -> None:
    """Nothing calls it with zero today. It must not render as a name if one does."""
    assert branch._names([]) == ""


# ---------------------------------------------------------------------------
# every verdict branch that interpolates one
# ---------------------------------------------------------------------------

def test_the_unconcluded_branch_carries_the_conjunction() -> None:
    one = _say({"tests": _MOVING}, {"tests": ["IN_PROGRESS"]})
    three = _say({"tests": _MOVING, "CodeQL": _MOVING, "changelog": _MOVING},
                 {"tests": ["IN_PROGRESS"], "CodeQL": ["QUEUED"],
                  "changelog": ["QUEUED"]})
    assert "`tests` has not concluded" in one
    assert "`CodeQL`, `changelog` and `tests` have not concluded" in three


def test_the_red_branch_carries_the_conjunction() -> None:
    red = {"status": "completed", "conclusion": "failure", "databaseId": 3,
           "createdAt": "2026-01-01T00:00:00Z"}
    one = _say({"tests": red}, {"tests": ["FAILURE"]})
    three = _say({"tests": red, "CodeQL": red, "changelog": red},
                 {"tests": ["FAILURE"], "CodeQL": ["FAILURE"],
                  "changelog": ["FAILURE"]})
    assert "in `tests`." in one
    assert "in `CodeQL`, `changelog` and `tests`." in three


def test_the_unread_job_list_branch_carries_the_conjunction() -> None:
    one = _say({"tests": _DONE}, {"tests": None})
    three = _say({"tests": _DONE, "CodeQL": _DONE, "changelog": _DONE},
                 {"tests": None, "CodeQL": None, "changelog": None})
    assert "for `tests` did" in one
    assert "for `CodeQL`, `changelog` and `tests` did" in three


def test_the_previous_head_branch_carries_the_conjunction() -> None:
    one = _say({"tests": _DONE}, {"tests": ["SUCCESS"]}, missing=["docs"])
    three = _say({"tests": _DONE}, {"tests": ["SUCCESS"]},
                 missing=["docs", "lint", "spell"])
    assert "`docs` ran on the previous head" in one
    assert "`docs`, `lint` and `spell` ran on the previous head" in three
